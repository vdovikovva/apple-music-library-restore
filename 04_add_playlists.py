#!/usr/bin/env python3
"""
Step 4 — rebuild playlists, preserving track order.

If a playlist with the same name already exists and is editable, it is filled
rather than duplicated. Apple's own curated playlists you were subscribed to
are re-added by their public pl.* identifier.

Run without --apply first to preview what will be created.

    python3 04_add_playlists.py --export "..."           # preview
    python3 04_add_playlists.py --export "..." --apply   # create
"""

import csv
import json
import os

from common import (ACCEPTED, ApiError, Client, base_parser, export_file,
                    load_tokens, require_export, work_dir)

CHUNK = 100          # tracks per request


def build_maps(args, work):
    """library track id -> source catalog id -> target catalog id."""
    from common import ID_FIELDS
    with open(export_file(args, "Apple Music Library Tracks.json"), encoding="utf-8") as f:
        lt = json.load(f)
    src_of = {}
    for t in lt:
        for field in ID_FIELDS:
            if t.get(field):
                src_of[t["Track Identifier"]] = str(t[field])
                break

    path = os.path.join(work, "tracks.csv")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found — run 02_resolve.py first")
    dst_of = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["Verdict"] in ACCEPTED and r["Target ID"]:
                dst_of[r["Source ID"]] = r["Target ID"]
    return src_of, dst_of


def resolve_items(items, src_of, dst_of):
    """Keep original order, drop what we could not restore, collapse repeats."""
    out, seen = [], set()
    for lib_id in items:
        dst = dst_of.get(src_of.get(lib_id, ""), None)
        if dst and dst not in seen:
            seen.add(dst)
            out.append(dst)
    return out


def main():
    p = base_parser(__doc__)
    p.add_argument("--apply", action="store_true", help="actually create playlists")
    args = p.parse_args()
    require_export(args)

    work = work_dir(args)
    jpath = os.path.join(work, "playlists_journal.json")
    client = Client(load_tokens(), pause=args.pause)
    src_of, dst_of = build_maps(args, work)

    with open(export_file(args, "Apple Music Library Playlists.json"), encoding="utf-8") as f:
        raw = json.load(f)

    # Existing playlists, so we fill rather than duplicate.
    existing = {}
    for d in client.get("/v1/me/library/playlists", {"limit": 100}).get("data", []):
        a = d["attributes"]
        existing.setdefault(a.get("name"), []).append(
            {"id": d["id"], "editable": a.get("canEdit", False)})

    journal = ({"done": []} if not os.path.exists(jpath)
               else json.load(open(jpath, encoding="utf-8")))

    plan = []
    used = {}
    for pl in raw:
        if pl.get("Container Type") != "Playlist" or not pl.get("Playlist Item Identifiers"):
            continue
        tracks = resolve_items(pl["Playlist Item Identifiers"], src_of, dst_of)
        if not tracks:
            continue
        name = pl.get("Title") or "Untitled"
        # Playlists can share a name; take the n-th existing one for the n-th copy.
        n = used.get(name, 0)
        used[name] = n + 1
        slot = existing.get(name, [])
        target = slot[n] if n < len(slot) and slot[n]["editable"] else None
        plan.append({
            "key": f"{name}#{n}",           # unique even for same-named playlists
            "name": name, "tracks": tracks,
            "desc": pl.get("Description") or "",
            "existing_id": target["id"] if target else None,
        })

    subscribed = [pl.get("Public Playlist Identifier") for pl in raw
                  if pl.get("Container Type") == "Subscribed Playlist"
                  and pl.get("Public Playlist Identifier")]

    print("=" * 66)
    print("STEP 4 — PLAYLISTS" + ("" if args.apply else "   (preview)"))
    print("=" * 66)
    total = sum(len(x["tracks"]) for x in plan)
    print(f"  Playlists to restore:   {len(plan)}  ({total} tracks)")
    print(f"  Apple playlists to add: {len(subscribed)}\n")
    for x in plan:
        mode = "fill" if x["existing_id"] else "create"
        print(f"    {mode:<7} {x['name'][:36]:<38} {len(x['tracks'])}")

    if not args.apply:
        print("\n  Preview only. Re-run with --apply to create them.")
        return

    print("\n" + "-" * 66)
    ok = fail = 0
    for x in plan:
        if x["key"] in journal["done"]:
            continue
        head, tail = x["tracks"][:CHUNK], x["tracks"][CHUNK:]
        try:
            if x["existing_id"]:
                pid = x["existing_id"]
                client.mutate("POST", f"/v1/me/library/playlists/{pid}/tracks",
                              {"data": [{"id": i, "type": "songs"} for i in head]})
            else:
                _, resp = client.mutate("POST", "/v1/me/library/playlists", {
                    "attributes": {"name": x["name"], "description": x["desc"]},
                    "relationships": {"tracks": {
                        "data": [{"id": i, "type": "songs"} for i in head]}},
                })
                pid = resp.get("data", [{}])[0].get("id")
            while tail and pid:
                part, tail = tail[:CHUNK], tail[CHUNK:]
                client.mutate("POST", f"/v1/me/library/playlists/{pid}/tracks",
                              {"data": [{"id": i, "type": "songs"} for i in part]})
            journal["done"].append(x["key"])
            ok += 1
            print(f"  ok  {x['name'][:38]:<40} {len(x['tracks'])}")
        except ApiError as e:
            fail += 1
            print(f"  !!  {x['name'][:38]:<40} {e}")
        json.dump(journal, open(jpath, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"\n  Playlists done: {ok}   failed: {fail}")

    if subscribed:
        print("\n  Re-adding Apple curated playlists…")
        added = 0
        todo = [s for s in subscribed if s not in journal["done"]]
        for i in range(0, len(todo), 10):
            batch = todo[i:i + 10]
            try:
                client.mutate("POST", f"/v1/me/library?ids[playlists]={','.join(batch)}")
                journal["done"] += batch
                added += len(batch)
            except ApiError as e:
                print(f"    ! {e}")
        json.dump(journal, open(jpath, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"    added: {added}  (some may no longer exist on Apple's side)")

    final = client.get("/v1/me/library/playlists", {"limit": 100}).get("data", [])
    print(f"\n  Playlists in library now: {len(final)}")


if __name__ == "__main__":
    main()
