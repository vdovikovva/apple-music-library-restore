"""
Shared helpers: token loading, API client, library parsing.

No third-party dependencies — standard library only.
"""

import difflib
import html
import re
import unicodedata
import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Read-only catalog queries go here.
API = "https://api.music.apple.com"
# Library mutations (POST/DELETE) only work here — API returns 401 for DELETE.
AMP = "https://amp-api.music.apple.com"

# A track in the export can carry up to four different catalog IDs. We take the
# first one present, ordered by how trustworthy its origin is:
#
#   1. Apple Music    — the streaming catalog ID, exactly what we want back
#   2. Audio Matched  — Apple identified your file by its audio fingerprint,
#                       so the match is machine-verified
#   3. Purchased      — a real ID, but a purchase from years ago can point at
#                       an edition no longer in the catalog
#   4. Tag Matched    — matched on text tags, which humans write badly
#
# Rule: the better the provenance of the ID, the higher it ranks. Counts on the
# reference library (10,532 tracks): 6,615 / 1,704 / 161 / 31, with 2,021
# tracks carrying no catalog ID at all — those are personal uploads.
ID_FIELDS = [
    "Apple Music Track Identifier",
    "Audio Matched Track Identifier",
    "Purchased Track Identifier",
    "Tag Matched Track Identifier",
]

DEFAULT_PAUSE = 0.35   # ~3 req/s; Apple allows ~20, we stay well under


class ApiError(Exception):
    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


class Client:
    """Apple Music API client with retry on rate limits and network drops."""

    def __init__(self, tokens, pause=DEFAULT_PAUSE, verbose=True):
        self.tokens = tokens
        self.pause = pause
        self.verbose = verbose
        self.ctx = ssl.create_default_context()

    def _say(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def _raw(self, method, host, path, params=None, body=None, timeout=40):
        url = host + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="[],")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.tokens['developer_token']}")
        req.add_header("Music-User-Token", self.tokens["music_user_token"])
        req.add_header("Origin", "https://music.apple.com")
        req.add_header("Referer", "https://music.apple.com/")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode()) from None

    def get(self, path, params=None, attempts=5, strict=False):
        """
        GET that tolerates 404 (nothing found), 429 and network drops.

        When the network stays down past all retries, the default is to return
        an empty page, same as a 404. With strict=True it raises instead: a
        caller that pages through the whole library must not mistake 'offline'
        for 'the library is empty'.
        """
        for i in range(attempts):
            try:
                time.sleep(self.pause)
                return self._raw("GET", API, path, params=params)[1]
            except ApiError as e:
                if e.status == 404:
                    return {"data": []}
                if e.status == 429:
                    wait = 5 * (i + 1)
                    self._say(f"    rate limited — waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                wait = 3 * (i + 1)
                self._say(f"    network ({type(e).__name__}) — retry in {wait}s")
                time.sleep(wait)
        if strict:
            raise ApiError(0, "network down, giving up after retries")
        return {"data": []}

    def mutate(self, method, path, body=None, attempts=3):
        """POST/DELETE against the AMP host, with retries."""
        for i in range(attempts):
            try:
                time.sleep(self.pause)
                return self._raw(method, AMP, path, body=body)
            except ApiError as e:
                if e.status == 429:
                    wait = 10 * (i + 1)
                    self._say(f"    rate limited — waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                wait = 3 * (i + 1)
                self._say(f"    network ({type(e).__name__}) — retry in {wait}s")
                time.sleep(wait)
        raise ApiError(0, "giving up after retries")

    # ── convenience wrappers ──

    def storefront(self):
        d = self.get("/v1/me/storefront", strict=True).get("data", [])
        return d[0]["id"] if d else None

    def library_songs(self, limit_pages=400):
        """All library songs, paged."""
        out, offset = [], 0
        for _ in range(limit_pages):
            page = self.get("/v1/me/library/songs",
                            {"limit": 100, "offset": offset},
                            strict=True).get("data", [])
            if not page:
                break
            out += page
            offset += 100
        return out

    def catalog_batch(self, storefront, kind, param, values):
        """
        Batched catalog lookup. Splits the batch in half on error so one bad ID
        cannot poison the rest.
        Returns (by_id: dict, items: list).
        """
        if not values:
            return {}, []
        try:
            p = self.get(f"/v1/catalog/{storefront}/{kind}",
                         {param: ",".join(values)}, strict=True)
            items = p.get("data", [])
            return {d["id"]: d for d in items}, items
        except ApiError as e:
            # Offline is not a bad ID: splitting would only multiply the
            # retries, and an empty answer would read as 'not in catalog'.
            if e.status == 0:
                raise
            if len(values) == 1:
                return {}, []
            mid = len(values) // 2
            a, ia = self.catalog_batch(storefront, kind, param, values[:mid])
            b, ib = self.catalog_batch(storefront, kind, param, values[mid:])
            a.update(b)
            return a, ia + ib


def keep_awake():
    """
    Hold off idle sleep for as long as this process lives. macOS only.

    A long write left running on a laptop otherwise goes to sleep with it:
    the run crawls forward only during the short maintenance wakes, and the
    network drops out under it. caffeinate -w exits by itself with the process.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ─────────────────────────── tokens & CLI ────────────────────────────

def load_tokens(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")
    if not os.path.exists(path):
        sys.exit(
            f"tokens.json not found at {path}\n"
            "Copy tokens.example.json to tokens.json and fill it in.\n"
            "See README for how to obtain the two tokens."
        )
    with open(path, encoding="utf-8") as f:
        t = json.load(f)
    for k in ("developer_token", "music_user_token"):
        if not t.get(k) or t[k].startswith("<"):
            sys.exit(f"tokens.json: field '{k}' is not filled in")
    return t


def base_parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--export", metavar="DIR",
                   help="path to the 'Apple Music Activity' folder from your Apple data export")
    p.add_argument("--from", dest="src", default="ru", metavar="XX",
                   help="source storefront, the country your library came from (default: ru)")
    p.add_argument("--to", dest="dst", default="us", metavar="XX",
                   help="target storefront, the country of your new account (default: us)")
    p.add_argument("--work", default="./work", metavar="DIR",
                   help="where to keep reports and journals (default: ./work)")
    p.add_argument("--pause", type=float, default=DEFAULT_PAUSE,
                   help=f"seconds between requests (default: {DEFAULT_PAUSE})")
    return p


def require_export(args):
    """--export is optional in the parser because --undo does not need it."""
    if not args.export:
        sys.exit("--export is required: point it at your 'Apple Music Activity' folder")
    if not os.path.isdir(args.export):
        sys.exit(f"--export is not a directory: {args.export}")
    return args.export


def work_dir(args):
    os.makedirs(args.work, exist_ok=True)
    return args.work


def export_file(args, name):
    path = os.path.join(args.export, name)
    if not os.path.exists(path):
        sys.exit(f"Not found: {path}\n"
                 "Is --export pointing at the 'Apple Music Activity' folder?")
    return path


# ─────────────────────────── library parsing ─────────────────────────

def load_library_tracks(args):
    """
    Returns (tracks_with_catalog_id, tracks_without).
    Tracks without a catalog ID are personal uploads — they cannot be
    restored from the streaming catalog.
    """
    with open(export_file(args, "Apple Music Library Tracks.json"), encoding="utf-8") as f:
        raw = json.load(f)
    with_id, without = [], []
    for t in raw:
        rec = {
            "lib_id": t.get("Track Identifier"),
            "title": t.get("Title"),
            "artist": t.get("Artist"),
            "album": t.get("Album"),
            "duration": t.get("Track Duration"),
        }
        for field in ID_FIELDS:
            if t.get(field):
                rec["src_id"] = str(t[field])
                rec["id_source"] = field
                with_id.append(rec)
                break
        else:
            without.append(rec)
    return raw, with_id, without


def live_library_ids(client):
    """
    Catalog IDs of songs that actually play.

    Entries with no playParams are dead: metadata left behind after an
    iTunes Match / uploaded file lost its cloud copy. They show up greyed
    out in the Music app and must not be treated as 'already restored'.
    """
    live, dead = set(), 0
    for d in client.library_songs():
        pp = d.get("attributes", {}).get("playParams")
        if not pp:
            dead += 1
            continue
        if pp.get("catalogId"):
            live.add(str(pp["catalogId"]))
    return live, dead


# ─────────────────────────── text matching ──────────────────────────

# Bracketed junk that bootleg uploads carry and the catalog never does, plus
# the edition suffixes Apple adds to its own titles. Stripped only for the
# comparison key — the original text is kept for display and for search terms.
NOISE = re.compile(
    r"\((?:feat|ft|with|prod)\.?[^)]*\)"
    r"|\[[^\]]*\]"
    r"|\b(?:bass ?boosted|sped ?up|slowed|remastered|single|explicit|clean)\b"
    r"|[-–—]\s*single\s*$",
    re.I)


def norm(text):
    """
    Comparison key for a title or an artist.

    Used wherever two storefronts have to be matched on text alone — when the
    source account is gone, no catalog ID survives and this is all that is
    left to join on.
    """
    if not text:
        return ""
    # The API returns HTML entities: 'A &amp; B'. Left as is, the '&' turns
    # into a bare 'amp' token and the pair stops matching its own snapshot.
    s = html.unescape(text)
    s = unicodedata.normalize("NFKD", s).lower()
    # NFKD leaves the combining diaeresis of 'ё' in place, which then survives
    # as its own character and splits 'Всё' from 'Все'. Russian catalogs write
    # it both ways for the same recording.
    s = s.replace("\u0308", "")
    s = NOISE.sub(" ", s)
    # Keep letters and digits of any alphabet, drop everything else. This is
    # what kills the emoji walls in the bootleg artist names.
    s = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in s)
    return " ".join(s.split())


def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def duration_delta(track, attrs):
    if not track.get("duration"):
        return None
    return abs(attrs.get("durationInMillis", 0) - track["duration"])


def verdict(delta, exact_ms=2000, close_ms=5000):
    """
    Duration is what separates 'same track' from 'different version'.
    The equivalents API happily returns an extended mix for a radio edit.
    """
    if delta is None:
        return "no-duration"
    if delta <= exact_ms:
        return "exact"
    if delta <= close_ms:
        return "close"
    if delta <= 30000:
        return "suspect"
    return "different"


ACCEPTED = ("exact", "close")
