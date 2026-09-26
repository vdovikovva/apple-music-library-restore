#!/usr/bin/env python3
"""
Step 1 — diff the local snapshot against the library of the new account.

The old account is gone, so its tracks carry no catalog IDs: all we have is
text and duration from step 0. The new account is alive, so its library comes
straight from the API, catalog IDs included.

Matching therefore runs text-to-text, in three passes of falling confidence:

  A. artist + title, duration within 3s   — same recording, no doubt
  B. artist + title, duration ignored     — same song, different master
  C. title only, duration within 2s       — the artist string disagrees
                                            ('A, B & C' vs 'A'), duration and
                                            title together still pin it down
  D. fuzzy title >=93%, duration within 2s — same recording spelled apart:
                                            'RMX' vs 'Remix', '4 AM' vs
                                            '4 A.M.', and Apple's own typos

Whatever survives all three is the delta: present on the old account, absent
on the new one.

Usage:
    python3 01_diff_ru.py --snapshot work/us-export
"""

import argparse
import json
import os
import sys

from common import Client, load_tokens, norm, similar


def fetch_ru_library(client, path):
    """Whole library of the current account, cached on disk after first run."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            songs = json.load(f)
        print(f"  cached: {len(songs)} tracks from {path}")
        return songs

    songs, dead = [], 0
    for d in client.library_songs():
        a = d.get("attributes", {})
        pp = a.get("playParams") or {}
        if not pp:
            # Metadata left behind after the cloud copy was lost: greyed out
            # in the app, unplayable, and not something to count as present.
            dead += 1
            continue
        songs.append({
            "catalog_id": str(pp.get("catalogId") or ""),
            "library_id": d.get("id"),
            "title": a.get("name"),
            "artist": a.get("artistName"),
            "album": a.get("albumName"),
            "duration": a.get("durationInMillis"),
        })
        if len(songs) % 500 == 0:
            print(f"    {len(songs)}...")
    print(f"  fetched: {len(songs)} tracks, {dead} dead entries skipped")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(songs, f, ensure_ascii=False, indent=1)
    return songs


def index(songs):
    """Lookup tables: exact pair, exact title, and duration-bucketed for fuzzy."""
    by_pair, by_title, by_sec = {}, {}, {}
    for s in songs:
        pair = (norm(s["artist"]), norm(s["title"]))
        by_pair.setdefault(pair, []).append(s)
        by_title.setdefault(norm(s["title"]), []).append(s)
        if s.get("duration"):
            by_sec.setdefault(s["duration"] // 1000, []).append(s)
    return by_pair, by_title, by_sec


def fuzzy(title, dur, by_sec, threshold=0.93):
    """
    Nearest title among tracks of near-identical length.

    Bucketing by second first is what makes this affordable: comparing every
    missing track against every track in the library would be millions of
    string ratios, against a few dozen here.
    """
    if not dur or not title:
        return None
    best, best_r = None, threshold
    for sec in range(dur // 1000 - 2, dur // 1000 + 3):
        for s in by_sec.get(sec, []):
            if abs(s["duration"] - dur) > 2000:
                continue
            r = similar(title, norm(s["title"]))
            if r >= best_r:
                best, best_r = s, r
    return best


def close(a, b, limit):
    return a and b and abs(a - b) <= limit


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snapshot", default="work/us-export",
                   help="folder written by 00_dump_local.py")
    p.add_argument("--work", default="work", help="folder for intermediate files")
    p.add_argument("--pause", type=float, default=0.35)
    args = p.parse_args()

    tpath = os.path.join(args.snapshot, "Apple Music Library Tracks.json")
    if not os.path.exists(tpath):
        sys.exit(f"{tpath} not found — run 00_dump_local.py first")
    with open(tpath, encoding="utf-8") as f:
        old = json.load(f)

    print("=" * 66)
    print("STEP 1 — DIFF AGAINST THE NEW ACCOUNT")
    print("=" * 66)

    client = Client(load_tokens(), pause=args.pause)
    print(f"\nStorefront: {client.storefront()}")
    print("\nNew library...")
    new = fetch_ru_library(client, os.path.join(args.work, "ru-library.json"))

    by_pair, by_title, by_sec = index(new)

    matched, delta = [], []
    stats = {"A": 0, "B": 0, "C": 0, "D": 0}
    for t in old:
        artist, title, dur = t.get("Artist"), t.get("Title"), t.get("Track Duration")
        if not title:
            delta.append({**t, "reason": "no title"})
            continue

        pair = (norm(artist), norm(title))
        hits = by_pair.get(pair, [])

        hit = next((s for s in hits if close(dur, s["duration"], 3000)), None)
        how = "A"
        if not hit and hits:
            hit, how = hits[0], "B"
        if not hit:
            hit = next((s for s in by_title.get(norm(title), [])
                        if close(dur, s["duration"], 2000)), None)
            how = "C"
        if not hit:
            hit, how = fuzzy(norm(title), dur, by_sec), "D"

        if hit:
            stats[how] += 1
            matched.append({"old": t, "new": hit, "pass": how})
        else:
            delta.append({**t, "reason": "not in new library"})

    os.makedirs(args.work, exist_ok=True)
    dpath = os.path.join(args.work, "delta.json")
    with open(dpath, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.work, "matched.json"), "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=1)

    catalog = sum(1 for t in delta if t.get("Cloud Status") == "subscription")

    print("\n" + "-" * 66)
    print(f"  Old snapshot:    {len(old)}")
    print(f"  New library:     {len(new)}")
    print(f"  Already there:   {len(matched)}"
          f"   (A {stats['A']} / B {stats['B']} / C {stats['C']} / D {stats['D']})")
    print(f"  Missing:         {len(delta)}  -> {dpath}")
    print(f"    of them from the catalog: {catalog}"
          f"   (the rest are uploads or dead entries and cannot be restored)")


if __name__ == "__main__":
    main()
