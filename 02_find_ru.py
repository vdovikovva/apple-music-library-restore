#!/usr/bin/env python3
"""
Step 2 — find the missing tracks in the target catalog, by text.

The normal pipeline resolves a source catalog ID into a target one. Here there
is no source ID to resolve: the account that held it is gone. All that came
across from step 0 is what the Music app knew — artist, title, album and a
duration accurate to the millisecond.

Two things shape this script, both learned the hard way on a live account.

Search is rate-limited by volume, not by pace. Catalog lookups run happily at
three per second; /search dies after a few hundred whatever the gap between
them, and then answers 429 for a long while. So the pause here is adaptive:
it backs off when Apple complains and creeps back down when it does not, and
the run is arranged to need as few searches as it can.

Hence the two phases. An artist with twenty missing tracks does not need
twenty searches — one search on the artist name returns a pool of their songs
that all twenty can be matched against locally. Only what the pool misses
falls through to a search of its own.

    phase 1   artists with 2+ missing tracks -> one search each, match locally
    phase 2   everything still missing       -> one search per track

Acceptance is the same in both, and deliberately strict: the search for
'Nirvana Something in the Way' returns the original, four live versions, two
covers and a lullaby rendition, and only one of them is the record that sat in
the old library.

    title similarity   >= 0.90
    artist similarity  >= 0.80
    duration           within 5s, and 'exact' or 'close' by verdict()

Writes tracks.csv in the shape the normal step 2 writes, so 03_add_tracks.py
consumes it unchanged.

Usage:
    python3 02_find_ru.py --delta work/delta.json
    python3 02_find_ru.py --delta work/delta.json --loose
"""

import argparse
import collections
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from common import (ACCEPTED, AMP, API, duration_delta, keep_awake, load_tokens,
                    norm, similar, verdict)

CHECKPOINT_EVERY = 25

# Adaptive pacing. Start civil, back off hard on 429, and only creep back down
# after a long clean streak — the limit is a moving window, so a burst of
# successes right after a block means nothing yet.
PAUSE_START = 10.0
PAUSE_MIN = 8.0
PAUSE_MAX = 20.0
BACKOFF = 1.6
RECOVER_AFTER = 20      # clean requests before easing off
RECOVER_BY = 0.85

# The two hosts are rate-limited separately, which sounds like free capacity
# and is not. Measured over a night: once api.music.apple.com is blocked it
# stays blocked for hours, while amp-api — the host the web player itself
# uses — keeps answering. Alternating between them therefore spends half the
# requests on a host that is certainly going to 429, and those 429s drove the
# adaptive pause into its ceiling. One host, the live one.
HOSTS = [AMP]


class Search:
    """Rate-limit aware GET, tuned for /search specifically."""

    def __init__(self, tokens, pause=PAUSE_START, verbose=True):
        self.tokens = tokens
        self.pause = pause
        self.verbose = verbose
        self.clean = 0
        self.blocked = 0
        self.calls = 0
        self.host = 0

    def _fetch(self, host, path, params):
        url = f"{host}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.tokens['developer_token']}",
            "Music-User-Token": self.tokens["music_user_token"],
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
        })
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode() or "{}")

    def get(self, path, params, attempts=0):
        """attempts=0 means keep trying: this is meant to run overnight."""
        i = 0
        offline = 0
        while attempts == 0 or i < attempts:
            i += 1
            time.sleep(self.pause)
            tried = 0
            while tried < len(HOSTS):
                host = HOSTS[self.host]
                try:
                    out = self._fetch(host, path, params)
                    self.calls += 1
                    self.clean += 1
                    offline = 0
                    if self.clean >= RECOVER_AFTER and self.pause > PAUSE_MIN:
                        self.pause = max(PAUSE_MIN, self.pause * RECOVER_BY)
                        self.clean = 0
                        self._say(f"    easing pace to {self.pause:.1f}s")
                    return out
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        return {}
                    if e.code != 429:
                        raise
                    # This host is blocked; the other one may not be.
                    self.blocked += 1
                    self.clean = 0
                    self.host = (self.host + 1) % len(HOSTS)
                    tried += 1
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    # Offline is not a 429: it says nothing about Apple's
                    # window, so it must not slow the pace or trigger the
                    # half-hour sleep below. Same host again, waiting longer
                    # each time, for as long as the network stays down.
                    wait = min(300, 10 * 2 ** offline)
                    offline += 1
                    self._say(f"    network ({type(e).__name__}) — retry in {wait}s")
                    time.sleep(wait)
            # Every host said 429 in this round: the window is genuinely shut.
            # It reopens on Apple's clock, not ours, and measured here that is
            # minutes rather than seconds — so wait in minutes and stop
            # counting attempts. The checkpoint makes patience free.
            self.pause = min(PAUSE_MAX, self.pause * BACKOFF)
            # Measured the hard way: hammering a shut window at four-minute
            # intervals bought a nine-hour block. Every request into a closed
            # window is another tick on their counter, so back off in half
            # hours and let it actually reopen.
            wait = 1800
            self._say(f"    all hosts 429 — pace {self.pause:.1f}s, sleeping {wait}s")
            time.sleep(wait)
            i = 0
        raise SystemExit("search blocked and attempts exhausted")

    def songs(self, store, term, limit=25, offset=0):
        p = {"term": term[:200], "types": "songs", "limit": limit}
        if offset:
            p["offset"] = offset
        r = self.get(f"/v1/catalog/{store}/search", p)
        return r.get("results", {}).get("songs", {}).get("data", [])

    def _say(self, msg):
        if self.verbose:
            print(msg, flush=True)


def judge(track, attrs, strict):
    """Accept or reject one candidate. Returns (verdict, title_r, artist_r)."""
    t_r = similar(norm(track.get("Title")), norm(attrs.get("name")))
    a_r = similar(norm(track.get("Artist")), norm(attrs.get("artistName")))
    delta = duration_delta({"duration": track.get("Track Duration")}, attrs)
    v = verdict(delta) if delta is not None else None
    if v is None:
        return None, t_r, a_r
    if strict and (t_r < 0.90 or a_r < 0.80):
        return None, t_r, a_r
    if v not in ACCEPTED:
        return None, t_r, a_r
    return v, t_r, a_r


def pick(track, candidates, strict):
    """Best acceptable candidate: closest duration wins."""
    best = None
    for d in candidates:
        a = d.get("attributes", {})
        v, t_r, a_r = judge(track, a, strict)
        if not v:
            continue
        cand = {"status": "found", "id": d["id"], "verdict": v,
                "found_artist": a.get("artistName"), "found_title": a.get("name"),
                "isrc": a.get("isrc"),
                "delta_ms": duration_delta({"duration": track.get("Track Duration")}, a),
                "title_r": round(t_r, 3), "artist_r": round(a_r, 3)}
        if best is None or (cand["delta_ms"] or 0) < (best["delta_ms"] or 0):
            best = cand
    return best


def write_csv(todo, done, out):
    """
    Dump the checkpoint as the CSV step 3 expects.

    Kept separate from the run because the run is long and gets interrupted:
    the results found so far should be loadable at any moment, not only when
    the last track has been searched.
    """
    header = ["Artist", "Title", "Album", "Source ID", "ID field", "Method",
              "Target ID", "Found artist", "Found title", "Delta sec",
              "Verdict", "Already in library"]
    found = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for t in todo:
            r = done.get(str(t["Track Identifier"]), {})
            if r.get("status") != "found":
                continue
            found += 1
            w.writerow([t.get("Artist"), t.get("Title"), t.get("Album"),
                        t["Track Identifier"], "local", r.get("via", "search"),
                        r["id"], r["found_artist"], r["found_title"],
                        round((r.get("delta_ms") or 0) / 1000, 2),
                        r["verdict"], ""])
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--delta", default="work/delta.json")
    p.add_argument("--work", default="work")
    p.add_argument("--store", default="ru")
    p.add_argument("--loose", action="store_true",
                   help="accept on duration alone, without the text thresholds")
    p.add_argument("--pause", type=float, default=PAUSE_START)
    p.add_argument("--playlists-only", action="store_true",
                   help="search only tracks that belong to a playlist")
    p.add_argument("--export-only", action="store_true",
                   help="write tracks.csv from the checkpoint and exit, "
                        "without searching or touching the network")
    args = p.parse_args()

    with open(args.delta, encoding="utf-8") as f:
        delta = json.load(f)
    # Uploads and dead entries were never in the catalog; searching for them
    # burns the very budget the real tracks need.
    todo = [t for t in delta if t.get("Cloud Status") == "subscription"]

    os.makedirs(args.work, exist_ok=True)
    cpath = os.path.join(args.work, "find_checkpoint.json")
    done = {}
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as f:
            done = json.load(f)

    # Artists whose pool has already been fetched. Without this every restart
    # re-searches every artist whose pool came up empty — and with a budget of
    # a few hundred searches a day, repeating yesterday's misses is the most
    # expensive thing the script can do.
    apath = os.path.join(args.work, "find_artists.json")
    seen_artists = set()
    if os.path.exists(apath):
        with open(apath, encoding="utf-8") as f:
            seen_artists = set(json.load(f))

    strict = not args.loose

    if args.export_only:
        n = write_csv(todo, done, os.path.join(args.work, "tracks.csv"))
        print(f"  {n} found tracks written to {args.work}/tracks.csv")
        return

    # Hours of searching: without this a laptop sleeps through most of it.
    keep_awake()

    api = Search(load_tokens(), pause=args.pause)

    def flush():
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
        with open(apath, "w", encoding="utf-8") as f:
            json.dump(sorted(seen_artists), f, ensure_ascii=False)

    def pending(tracks):
        return [t for t in tracks
                if done.get(str(t["Track Identifier"]), {}).get("status") != "found"]

    print("=" * 66)
    print(f"STEP 2 — SEARCH THE '{args.store}' CATALOG"
          + ("   (strict)" if strict else "   (loose)"))
    print("=" * 66)
    already = sum(1 for v in done.values() if v.get("status") == "found")
    print(f"\n  {len(todo)} tracks missing, {already} already found in checkpoint")

    # ── phase 1: one search per artist, for artists worth batching ──────
    groups = collections.defaultdict(list)
    for t in todo:
        groups[norm(t.get("Artist"))].append(t)
    multi = {a: ts for a, ts in groups.items() if a and len(ts) > 1}
    print(f"\n[1] artist pools — {len(multi)} artists covering "
          f"{sum(len(v) for v in multi.values())} tracks")

    fresh = [(a, ts) for a, ts in sorted(multi.items()) if a not in seen_artists]
    print(f"    {len(multi) - len(fresh)} artists already pooled in earlier runs, "
          f"{len(fresh)} to go")
    for n, (artist, tracks) in enumerate(fresh, 1):
        left = pending(tracks)
        if not left:
            seen_artists.add(artist)
            continue
        # One page per ten missing tracks, capped: a pool deeper than this
        # stops being about this artist and fills up with 'related' noise.
        pages = min(4, 1 + len(left) // 10)
        pool = []
        for page in range(pages):
            got = api.songs(args.store, tracks[0].get("Artist") or artist,
                            limit=25, offset=page * 25)
            pool.extend(got)
            if len(got) < 25:
                break
        for t in left:
            hit = pick(t, pool, strict)
            if hit:
                hit["via"] = "artist pool"
                done[str(t["Track Identifier"])] = hit
        seen_artists.add(artist)
        if n % CHECKPOINT_EVERY == 0:
            flush()
            found = sum(1 for v in done.values() if v.get("status") == "found")
            print(f"    artist {n}/{len(fresh)}   found {found}   "
                  f"pace {api.pause:.1f}s   429s {api.blocked}", flush=True)
    flush()

    # ── phase 2: one search per track, for whatever is still missing ────
    # This phase costs one request per track, so the order it runs in decides
    # what exists if the run is cut short. Tracks that sit in a playlist come
    # first: a library track nobody has opened in years can wait, a playlist
    # with holes in it is noticed immediately.
    in_playlist = set()
    for name in ("Apple Music Library Playlists.json", "playlists-from-xml.json"):
        path = os.path.join(os.path.dirname(args.delta), "us-export", name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for pl in json.load(f):
                    in_playlist.update(pl.get("Playlist Item Identifiers", []))

    left = pending(todo)
    left.sort(key=lambda t: t["Track Identifier"] not in in_playlist)
    if args.playlists_only:
        left = [t for t in left if t["Track Identifier"] in in_playlist]
    n_pl = sum(1 for t in left if t["Track Identifier"] in in_playlist)
    print(f"\n[2] per-track — {len(left)} tracks still missing "
          f"({n_pl} of them in playlists, searched first)")
    for n, t in enumerate(left, 1):
        term = f"{(t.get('Artist') or '').strip()} {(t.get('Title') or '').strip()}".strip()
        if not term:
            done[str(t["Track Identifier"])] = {"status": "no term"}
            continue
        hit = pick(t, api.songs(args.store, term, limit=10), strict)
        if hit:
            hit["via"] = "track search"
        done[str(t["Track Identifier"])] = hit or {"status": "not found"}
        if n % CHECKPOINT_EVERY == 0:
            flush()
            found = sum(1 for v in done.values() if v.get("status") == "found")
            print(f"    track {n}/{len(left)}   found {found}   "
                  f"pace {api.pause:.1f}s   429s {api.blocked}", flush=True)
    flush()

    # ── output ─────────────────────────────────────────────────────────
    out = os.path.join(args.work, "tracks.csv")
    found = write_csv(todo, done, out)

    via = collections.Counter(v.get("via") for v in done.values()
                              if v.get("status") == "found")
    print("\n" + "-" * 66)
    print(f"  Missing:     {len(todo)}")
    print(f"  Found:       {found}   -> {out}")
    print(f"    {dict(via)}")
    print(f"  Not found:   {len(todo) - found}")
    print(f"  Searches:    {api.calls}   (429s hit: {api.blocked}, "
          f"final pace {api.pause:.1f}s, host {HOSTS[api.host]})")


if __name__ == "__main__":
    main()
