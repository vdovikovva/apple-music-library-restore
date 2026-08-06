#!/usr/bin/env python3
"""
Step 3 — add the resolved tracks to your library.

This is the first step that writes. It reads tracks.csv from step 2, takes
only 'exact' and 'close' matches, skips what is already in your library,
and adds the rest in batches. Every added ID is journalled, so an interrupted
run resumes cleanly and --undo can roll the whole thing back.

    python3 03_add_tracks.py --export "..."          # add
    python3 03_add_tracks.py --export "..." --undo   # roll back
"""

import csv
import json
import os
import time

from common import (ACCEPTED, ApiError, Client, base_parser, live_library_ids,
                    load_tokens, require_export, work_dir)

BATCH = 25


def load_journal(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"added": [], "failed": []}


def save_journal(j, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)
    os.replace(tmp, path)


def undo(client, journal, path):
    print(f"UNDO — {len(journal['added'])} tracks in journal")
    lib = {}
    for d in client.library_songs():
        cat = (d.get("attributes", {}).get("playParams") or {}).get("catalogId")
        if cat:
            lib[str(cat)] = d["id"]
    removed = 0
    for cid in list(journal["added"]):
        lib_id = lib.get(cid)
        if not lib_id:
            journal["added"].remove(cid)   # not there anyway
            continue
        try:
            client.mutate("DELETE", f"/v1/me/library/songs/{lib_id}")
            journal["added"].remove(cid)
            removed += 1
            if removed % 100 == 0:
                save_journal(journal, path)
                print(f"    removed {removed}…")
        except ApiError as e:
            print(f"    ! {cid}: {e}")
    save_journal(journal, path)
    print(f"  Removed {removed}")


def main():
    p = base_parser(__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually add the tracks (without it you only get a preview)")
    p.add_argument("--undo", action="store_true", help="remove everything this tool added")
    p.add_argument("--include-manual", metavar="CSV",
                   help="also add rows from an edited manual_review.csv")
    args = p.parse_args()

    work = work_dir(args)
    jpath = os.path.join(work, "add_tracks_journal.json")
    client = Client(load_tokens(), pause=args.pause)
    journal = load_journal(jpath)

    if args.undo:
        undo(client, journal, jpath)
        return

    src = os.path.join(work, "tracks.csv")
    if not os.path.exists(src):
        raise SystemExit(f"{src} not found — run 02_resolve.py first")

    queue = {}
    with open(src, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["Verdict"] in ACCEPTED and r["Target ID"]:
                queue.setdefault(r["Target ID"], r)     # collapse duplicates
    if args.include_manual and os.path.exists(args.include_manual):
        with open(args.include_manual, encoding="utf-8-sig") as f:
            extra = [r for r in csv.DictReader(f) if r["Target ID"]]
        for r in extra:
            queue.setdefault(r["Target ID"], r)
        print(f"  Added {len(extra)} rows from {args.include_manual}")

    print("=" * 66)
    print("STEP 3 — ADD TRACKS" + ("" if args.apply else "   (preview)"))
    print("=" * 66)
    print(f"  Unique tracks resolved:  {len(queue)}")

    existing, dead = live_library_ids(client)
    print(f"  Already in library:      {len(existing)} live ({dead} dead entries ignored)")

    done = set(journal["added"])
    todo = [cid for cid in queue if cid not in existing and cid not in done]
    print(f"  Added in earlier runs:   {len(done)}")
    print(f"  -> to add now:           {len(todo)}\n")
    if not todo:
        print("  Nothing to do.")
        return

    if not args.apply:
        print("  Sample of what would be added:")
        for cid in todo[:15]:
            r = queue[cid]
            print(f"    {r['Found artist'] or r['Artist']} — {r['Found title'] or r['Title']}"
                  f"   [{r['Method']}, {r['Verdict']}]")
        if len(todo) > 15:
            print(f"    … and {len(todo) - 15} more")
        est = len(todo) / BATCH * (args.pause + 0.3) / 60
        print(f"\n  Preview only — nothing was written. Estimated run time: {est:.0f} min.")
        print("  Re-run with --apply to add them for real.")
        return

    t0, ok, fail = time.time(), 0, 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            client.mutate("POST", f"/v1/me/library?ids[songs]={','.join(chunk)}")
            journal["added"] += chunk
            ok += len(chunk)
        except ApiError as e:
            journal["failed"] += chunk
            fail += len(chunk)
            print(f"    ! batch {i // BATCH}: {e}")
        if (i // BATCH) % 20 == 0:
            save_journal(journal, jpath)
            print(f"    {min(i + BATCH, len(todo))}/{len(todo)}  ok={ok} failed={fail}")
    save_journal(journal, jpath)

    print(f"\n  Sent: {ok}   Failed: {fail}   ({(time.time() - t0) / 60:.1f} min)")
    print("\n  Waiting 30s, then verifying…")
    time.sleep(30)
    live, dead = live_library_ids(client)
    confirmed = sum(1 for c in journal["added"] if c in live)
    print(f"  Live tracks in library now: {len(live)}")
    print(f"  Confirmed from journal:     {confirmed}/{len(journal['added'])}")
    gap = len(journal["added"]) - confirmed
    if gap:
        print(f"\n  {gap} tracks were accepted (HTTP 202) but did not appear.")
        print("  This is Apple silently refusing: the track streams fine but its")
        print("  licence does not allow adding it to a library. Nothing to fix.")
    print(f"\n  Journal: {jpath}")
    print("  Roll back with: python3 03_add_tracks.py --export ... --undo")
    print("  Next: 04_add_playlists.py")


if __name__ == "__main__":
    main()
