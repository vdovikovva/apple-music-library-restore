#!/usr/bin/env python3
"""
Step 0 — snapshot the local Music.app library into export-shaped JSON.

For when the source Apple ID is gone: blocked, deleted, or simply not yours
any more. No token and no privacy.apple.com download are possible, but the
Music app on this Mac still holds the metadata, and AppleScript reads it.

Writes the two files the rest of the pipeline expects:

    Apple Music Library Tracks.json
    Apple Music Library Playlists.json

with one difference from a real Apple export: no catalog IDs. The Music app
never exposes them. Step 1b rebuilds them from the catalog by text + duration.

Usage:
    python3 00_dump_local.py --out "work/local-export"
"""

import argparse
import json
import os
import subprocess
import sys

SEP = "\x1e"          # ASCII record separator: will not occur in a track title

# AppleScript property -> our key. Everything here is a scalar the Music app
# can hand back for 'every track' in one batched call, which is the only way
# this finishes in seconds instead of an hour.
FIELDS = [
    ("database ID",   "db_id"),
    ("name",          "title"),
    ("artist",        "artist"),
    ("album artist",  "album_artist"),
    ("album",         "album"),
    ("duration",      "duration"),
    ("year",          "year"),
    ("track number",  "track_number"),
    ("disc number",   "disc_number"),
    ("genre",         "genre"),
    ("cloud status",  "cloud_status"),
    ("played count",  "played_count"),
    ("skipped count", "skipped_count"),
    ("composer",      "composer"),
    ("kind",          "kind"),
    ("size",          "size"),
    ("bit rate",      "bit_rate"),
    ("comment",       "comment"),
    ("grouping",      "grouping"),
    ("sort name",     "sort_name"),
    ("sort artist",   "sort_artist"),
    # Ratings and loves cannot be read back from anywhere once the account is
    # gone: they are not in the file, not in the XML export, and the API that
    # served them needs the very login we are about to lose.
    ("favorited",       "favorited"),
    ("disliked",        "disliked"),
    ("album favorited", "album_favorited"),
    ("rating",          "rating"),
]


def osa(script):
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("AppleScript failed:\n" + r.stderr.strip())
    return r.stdout.rstrip("\n")


def batch(prop, source, count=None):
    """
    One property for every track of `source`, as a list of strings.

    Not every property exists on every track class — a shared track from the
    catalog answers a different set than a local file. A property the Music
    app refuses is filled with blanks rather than killing the whole dump.
    """
    r = subprocess.run(["osascript", "-e",
        'set text item delimiters to (ASCII character 30)\n'
        'tell application "Music"\n'
        f'  set r to (get {prop} of every track of {source})\n'
        'end tell\n'
        'return r as text'], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    ! '{prop}' unavailable, left blank ({r.stderr.strip()[:60]})")
        return [""] * (count or 0)
    out = r.stdout.rstrip("\n")
    return out.split(SEP) if out else []


def _unused_batch(prop, source):
    out = osa(
        'set text item delimiters to (ASCII character 30)\n'
        'tell application "Music"\n'
        f'  set r to (get {prop} of every track of {source})\n'
        'end tell\n'
        'return r as text'
    )
    return out.split(SEP) if out else []


def to_ms(text):
    """
    Music reports seconds as a real; the Apple export uses integer ms.

    AppleScript formats reals in the system locale, so on a Russian Mac this
    arrives as '171,337005615234'. There is no thousands separator to confuse
    it with, so a plain swap is safe.
    """
    try:
        return int(round(float(text.replace(",", ".")) * 1000))
    except ValueError:
        return None


def to_int(text):
    try:
        return int(text)
    except ValueError:
        return None


def dump_tracks(sources):
    """
    Every track of every source, de-duplicated by database ID.

    The library playlist is not enough on its own: Apple Music lets a track
    sit in a playlist without ever being added to the library, and on this
    machine 214 playlist entries were exactly that. Missing them would have
    silently shortened four playlists on restore.
    """
    tracks, seen = [], set()
    for source in sources:
        cols = {"db_id": batch("database ID", source)}
        n = len(cols["db_id"])
        for prop, key in FIELDS:
            if key != "db_id":
                cols[key] = batch(prop, source, count=n)

        for key, values in cols.items():
            if len(values) != n:
                sys.exit(f"column '{key}' has {len(values)} rows, expected {n} "
                         "— the library changed mid-dump, run it again")

        for i in range(n):
            row = {key: cols[key][i] for key in cols}
            db_id = to_int(row["db_id"])
            if db_id is None or db_id in seen:
                continue
            seen.add(db_id)
            tracks.append(build(row, db_id))
    return tracks


def build(row, db_id):
    return {
        # Apple export field names, so the later steps need no changes.
        "Track Identifier": db_id,
        "Title":            row["title"],
        "Artist":           row["artist"],
        "Album":            row["album"],
        "Track Duration":   to_ms(row["duration"]),
        # Ours. Extra keys are ignored by the pipeline, and every one of them
        # is another way to tell two same-titled songs apart.
        "Album Artist":     row["album_artist"],
        "Year":             to_int(row["year"]),
        "Track Number":     to_int(row["track_number"]),
        "Disc Number":      to_int(row["disc_number"]),
        "Genre":            row["genre"],
        "Cloud Status":     row["cloud_status"],
        "Play Count":       to_int(row["played_count"]),
        "Skip Count":       to_int(row["skipped_count"]),
        "Composer":         row["composer"],
        "Kind":             row["kind"],
        "Size":             to_int(row["size"]),
        "Bit Rate":         to_int(row["bit_rate"]),
        "Comment":          row["comment"],
        "Grouping":         row["grouping"],
        "Sort Name":        row["sort_name"],
        "Sort Artist":      row["sort_artist"],
        "Favorited":        row["favorited"] == "true",
        "Disliked":         row["disliked"] == "true",
        "Album Favorited":  row["album_favorited"] == "true",
        "Rating":           to_int(row["rating"]),
    }


def dump_playlists():
    names = osa(
        'set text item delimiters to (ASCII character 30)\n'
        'tell application "Music" to set r to '
        '(get name of every user playlist)\n'
        'return r as text'
    ).split(SEP)
    kinds = osa(
        'set text item delimiters to (ASCII character 30)\n'
        'tell application "Music" to set r to '
        '(get special kind of every user playlist)\n'
        'return r as text'
    ).split(SEP)

    playlists, indexes = [], []
    for i, (name, kind) in enumerate(zip(names, kinds), start=1):
        # 'none' is a playlist the user made. Everything else is a built-in
        # container — Library, Music, Purchased — that would duplicate the
        # whole library or cannot be recreated on the other account anyway.
        if kind.strip().lower() != "none":
            print(f"  skip  {name}  (special kind: {kind})")
            continue
        ids = [to_int(x) for x in batch("database ID", f"user playlist {i}")]
        ids = [x for x in ids if x is not None]
        desc = osa(f'tell application "Music" to return description of user playlist {i}')
        indexes.append(i)
        playlists.append({
            "Container Type": "Playlist",
            "Title": name,
            "Description": desc,
            "Playlist Item Identifiers": ids,
        })
        print(f"  {name}: {len(ids)} tracks")
    return playlists, indexes


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="work/local-export",
                   help="folder to write the export-shaped JSON into")
    p.add_argument("--source", default="library playlist 1",
                   help="AppleScript source for the full library")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)

    print("=" * 66)
    print("STEP 0 — LOCAL SNAPSHOT")
    print("=" * 66)

    print("\nPlaylists...")
    playlists, indexes = dump_playlists()
    ppath = os.path.join(args.out, "Apple Music Library Playlists.json")
    with open(ppath, "w", encoding="utf-8") as f:
        json.dump(playlists, f, ensure_ascii=False, indent=1)

    print("\nTracks...")
    sources = [args.source] + [f"user playlist {i}" for i in indexes]
    tracks = dump_tracks(sources)
    tpath = os.path.join(args.out, "Apple Music Library Tracks.json")
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump(tracks, f, ensure_ascii=False, indent=1)

    known = {t["Track Identifier"] for t in tracks}
    orphan = sum(1 for pl in playlists
                 for i in pl["Playlist Item Identifiers"] if i not in known)
    if orphan:
        print(f"\n  WARNING: {orphan} playlist entries have no track record")

    # What can actually come back. 'subscription' tracks live in the catalog,
    # so they are restorable in principle; anything else is a local file or a
    # dead cloud entry and no amount of API calls will bring it back.
    by_status = {}
    for t in tracks:
        by_status[t["Cloud Status"]] = by_status.get(t["Cloud Status"], 0) + 1
    no_meta = sum(1 for t in tracks if not t["Title"] or not t["Artist"])

    print("\n" + "-" * 66)
    print(f"  Tracks written:     {len(tracks)}  -> {tpath}")
    print(f"  Playlists written:  {len(playlists)}  -> {ppath}")
    print("\n  Cloud status:")
    for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"    {status or '(empty)'}: {count}")
    if no_meta:
        print(f"\n  {no_meta} tracks miss a title or artist — unmatchable, they "
              "will be reported as failures in step 1b")


if __name__ == "__main__":
    main()
