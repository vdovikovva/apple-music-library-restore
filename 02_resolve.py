#!/usr/bin/env python3
"""
Step 2 — resolve every track from the source storefront to the target one.
Read-only: writes CSV reports, touches nothing in your library.

Cascade, most reliable first:
  1. ISRC          — 100% accurate in testing, but finds fewer tracks
  2. direct ID     — some IDs are identical across storefronts
  3. equivalents   — widest reach, but sometimes returns a different version

Every match is checked against the original duration, which is what catches
the converter swapping a radio edit for an extended mix.

Interrupted runs resume from a checkpoint.

    python3 02_resolve.py --export "/path/to/Apple Music Activity"
"""

import csv
import json
import os
import time

from common import (ACCEPTED, ApiError, Client, base_parser, duration_delta,
                    keep_awake, live_library_ids, load_library_tracks,
                    load_tokens, require_export, verdict, work_dir)

BATCH = 100
CHECKPOINT_EVERY = 200


def save(state, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def record(track, item, method):
    a = item["attributes"]
    delta = duration_delta(track, a)
    return {
        "method": method, "dst_id": item["id"],
        "found_artist": a.get("artistName"), "found_name": a.get("name"),
        "delta": delta, "verdict": verdict(delta),
    }


def stage_direct(client, args, tracks, resolved):
    """Cheapest method — one batched request per 100 IDs — but 96% accurate."""
    todo = [t for t in tracks if t["src_id"] not in resolved]
    print(f"\n[B] Direct ID lookup — {len(todo)} tracks")
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        by_id, _ = client.catalog_batch(args.dst, "songs", "ids",
                                        [t["src_id"] for t in chunk])
        for t in chunk:
            if t["src_id"] in by_id:
                resolved[t["src_id"]] = record(t, by_id[t["src_id"]], "direct")
        if (i // BATCH) % 10 == 0:
            print(f"    {min(i + BATCH, len(todo))}/{len(todo)}  resolved: {len(resolved)}")
    print(f"    -> {len(resolved)} resolved")


def stage_isrc(client, args, tracks, resolved):
    """
    Two batched passes: source catalog -> ISRC, then ISRC -> target catalog.
    Runs first because it is the only method that never returned a wrong
    version in testing.
    """
    todo = [t for t in tracks if t["src_id"] not in resolved]
    print(f"\n[A] ISRC route — {len(todo)} tracks")
    if not todo:
        return

    isrc_of = {}
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        by_id, _ = client.catalog_batch(args.src, "songs", "ids",
                                        [t["src_id"] for t in chunk])
        for t in chunk:
            d = by_id.get(t["src_id"])
            if d and d["attributes"].get("isrc"):
                isrc_of[t["src_id"]] = d["attributes"]["isrc"]
    print(f"    ISRC known for {len(isrc_of)} tracks")

    track_of = {t["src_id"]: t for t in todo}
    owner = {v: k for k, v in isrc_of.items()}
    hits = 0
    isrcs = list(owner)
    for i in range(0, len(isrcs), BATCH):
        _, items = client.catalog_batch(args.dst, "songs", "filter[isrc]",
                                        isrcs[i:i + BATCH])
        for d in items:
            src_id = owner.get(d["attributes"].get("isrc"))
            if src_id and src_id not in resolved:
                resolved[src_id] = record(track_of[src_id], d, "isrc")
                hits += 1
    print(f"    -> {hits} resolved")


def stage_equivalents(client, args, tracks, resolved, state, ckpt):
    todo = [t for t in tracks if t["src_id"] not in resolved]
    print(f"\n[C] Equivalents converter — {len(todo)} tracks (one request each)")
    hits = 0
    for n, t in enumerate(todo, 1):
        data = client.get(f"/v1/catalog/{args.dst}/songs",
                          {"filter[equivalents]": t["src_id"]},
                          strict=True).get("data", [])
        if data:
            resolved[t["src_id"]] = record(t, data[0], "equivalents")
            hits += 1
        if n % CHECKPOINT_EVERY == 0:
            state["resolved"] = resolved
            save(state, ckpt)
            print(f"    {n}/{len(todo)}  resolved: {hits}  [checkpoint]")
    print(f"    -> {hits} resolved")


def stage_albums(client, args, albums_out, state, ckpt):
    path = os.path.join(args.export, "Apple Music Library Albums.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    albums = [{"src_id": str(a["Catalog Identifiers - Album"]), "title": a.get("Title")}
              for a in raw if a.get("Catalog Identifiers - Album")]
    todo = [a for a in albums if a["src_id"] not in albums_out]
    print(f"\n[D] Albums — {len(todo)} with a catalog ID")

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        by_id, _ = client.catalog_batch(args.dst, "albums", "ids",
                                        [a["src_id"] for a in chunk])
        for a in chunk:
            if a["src_id"] in by_id:
                d = by_id[a["src_id"]]
                albums_out[a["src_id"]] = {
                    "method": "direct", "dst_id": d["id"],
                    "found_name": d["attributes"].get("name"),
                    "found_artist": d["attributes"].get("artistName"),
                }
    rest = [a for a in albums if a["src_id"] not in albums_out]
    for n, a in enumerate(rest, 1):
        data = client.get(f"/v1/catalog/{args.dst}/albums",
                          {"filter[equivalents]": a["src_id"]},
                          strict=True).get("data", [])
        if data:
            albums_out[a["src_id"]] = {
                "method": "equivalents", "dst_id": data[0]["id"],
                "found_name": data[0]["attributes"].get("name"),
                "found_artist": data[0]["attributes"].get("artistName"),
            }
        if n % CHECKPOINT_EVERY == 0:
            state["albums"] = albums_out
            save(state, ckpt)
    print(f"    -> {len(albums_out)}/{len(albums)} resolved")


def resolve_all(client, args, tracks, resolved, albums_out, state, ckpt):
    """All network work of this step. Returns catalog IDs already in the library."""
    existing, dead = live_library_ids(client)
    print(f"  Already in library:      {len(existing)} live, {dead} dead entries ignored")

    # Order matters and costs about a minute of extra requests.
    #
    # ISRC runs first even though it needs two batched passes (source catalog
    # -> ISRC, then ISRC -> target) while a direct lookup needs one. Measured
    # on this library: ISRC was duration-exact on 100% of its matches, direct
    # lookups on 96%. Where the two disagreed, ISRC was right 7 times out of 7.
    # Running direct first would lock in ~190 wrong versions that ISRC would
    # have resolved correctly. Accuracy wins over ~60 seconds.
    for fn in (stage_isrc, stage_direct):
        fn(client, args, tracks, resolved)
        state["resolved"] = resolved
        save(state, ckpt)
    stage_equivalents(client, args, tracks, resolved, state, ckpt)
    state["resolved"] = resolved
    save(state, ckpt)
    stage_albums(client, args, albums_out, state, ckpt)
    state["albums"] = albums_out
    save(state, ckpt)
    return existing


def main():
    args = base_parser(__doc__).parse_args()
    require_export(args)
    work = work_dir(args)
    # Checkpoint is per storefront pair: resuming a ru->us run inside a
    # ru->de run would silently mix results from two different catalogs.
    ckpt = os.path.join(work, f"resolve_checkpoint_{args.src}_{args.dst}.json")
    client = Client(load_tokens(), pause=args.pause)

    state = {"resolved": {}, "albums": {}}
    if os.path.exists(ckpt):
        with open(ckpt, encoding="utf-8") as f:
            state = json.load(f)
        print(f"  Resuming: {len(state['resolved'])} tracks already resolved")
    resolved, albums_out = state["resolved"], state["albums"]

    print("=" * 66)
    print(f"STEP 2 — RESOLVE  {args.src.upper()} -> {args.dst.upper()}   (read-only)")
    print("=" * 66)

    raw, tracks, untagged = load_library_tracks(args)
    print(f"  Tracks in export:        {len(raw)}")
    print(f"    with a catalog ID:     {len(tracks)}")
    print(f"    personal uploads:      {len(untagged)}  (not recoverable)")

    keep_awake()
    t0 = time.time()
    try:
        existing = resolve_all(client, args, tracks, resolved, albums_out, state, ckpt)
    except ApiError as e:
        if e.status != 0:
            raise
        # Network gone for good. Every hit so far is in the checkpoint; the
        # misses are not recorded anywhere, so a re-run simply asks again.
        state["resolved"], state["albums"] = resolved, albums_out
        save(state, ckpt)
        raise SystemExit(f"\n  Network down ({e}). Progress is saved — run the "
                         "same command again to resume.")

    # ── reports ──
    out_all = os.path.join(work, "tracks.csv")
    out_manual = os.path.join(work, "manual_review.csv")
    out_albums = os.path.join(work, "albums.csv")
    header = ["Artist", "Title", "Album", "Source ID", "ID field", "Method",
              "Target ID", "Found artist", "Found title", "Delta sec",
              "Verdict", "Already in library"]

    stats, manual, to_add = {}, [], set()
    with open(out_all, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for t in tracks:
            r = resolved.get(t["src_id"])
            dup = bool(r and r["dst_id"] in existing)
            v = r["verdict"] if r else "not-found"
            stats[v + (" (dup)" if dup else "")] = \
                stats.get(v + (" (dup)" if dup else ""), 0) + 1
            row = [t["artist"], t["title"], t["album"], t["src_id"], t["id_source"],
                   r["method"] if r else "", r["dst_id"] if r else "",
                   r["found_artist"] if r else "", r["found_name"] if r else "",
                   f"{r['delta'] / 1000:.1f}" if r and r["delta"] is not None else "",
                   v, "yes" if dup else ""]
            w.writerow(row)
            if r and v in ACCEPTED and not dup:
                to_add.add(r["dst_id"])
            elif r and v in ("suspect", "different") and not dup:
                manual.append(row)

    for path, rows in ((out_manual, manual),):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    with open(out_albums, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Source ID", "Method", "Target ID", "Found artist", "Found album"])
        for sid, r in albums_out.items():
            w.writerow([sid, r["method"], r["dst_id"], r["found_artist"], r["found_name"]])

    print("\n" + "=" * 66)
    print("RESULT")
    print("=" * 66)
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k:24s} {v:6d}  ({100 * v / len(tracks):.1f}%)")
    print(f"\n  Unique tracks ready to add: {len(to_add)}")
    print(f"  Needing your review:        {len(manual)}")
    print(f"  Albums resolved:            {len(albums_out)}")
    by_method = {}
    for r in resolved.values():
        by_method[r["method"]] = by_method.get(r["method"], 0) + 1
    print(f"  By method: " + ", ".join(f"{k}={v}" for k, v in by_method.items()))
    print(f"\n  Took {(time.time() - t0) / 60:.1f} min")
    print(f"\n  Reports:\n    {out_all}\n    {out_manual}\n    {out_albums}")
    print("\n  Next: review manual_review.csv, then run 03_add_tracks.py")


if __name__ == "__main__":
    main()
