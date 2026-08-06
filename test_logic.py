#!/usr/bin/env python3
"""
Tests for the decision logic. No network, no tokens, no Apple account needed.

    python3 test_logic.py

These cover the parts that silently corrupt a migration when wrong: the
duration check that separates a track from a different version of it, playlist
ordering, and the same-named-playlist case that already caused a bug once.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import verdict, duration_delta, ACCEPTED  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


p4 = load("p4", "04_add_playlists.py")

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok    {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


print("duration verdicts")
check("identical", verdict(0), "exact")
check("2s off is still exact", verdict(2000), "exact")
check("4s off is close", verdict(4000), "close")
check("10s off is suspect", verdict(10000), "suspect")
# The real failure mode: radio edit replaced by an extended mix.
check("Arty radio edit vs extended (234s)", verdict(234000), "different")
check("Gouryella radio vs original (422s)", verdict(422000), "different")
check("missing duration", verdict(None), "no-duration")
check("only exact and close are added", ACCEPTED, ("exact", "close"))

print("\nduration_delta")
check("computes absolute difference",
      duration_delta({"duration": 180000}, {"durationInMillis": 183000}), 3000)
check("handles reversed order",
      duration_delta({"duration": 183000}, {"durationInMillis": 180000}), 3000)
check("no duration in export", duration_delta({"duration": None}, {"durationInMillis": 1}), None)

print("\nplaylist track resolution")
src_of = {1: "a", 2: "b", 3: "c", 4: "d"}
dst_of = {"a": "A", "b": "B", "c": "C"}          # 'd' could not be resolved
check("order kept, repeat collapsed, unresolved dropped",
      p4.resolve_items([1, 2, 1, 3, 4], src_of, dst_of), ["A", "B", "C"])
check("empty playlist", p4.resolve_items([], src_of, dst_of), [])
check("nothing resolvable", p4.resolve_items([4], src_of, dst_of), [])


def pick(existing, name, n):
    """Mirrors the slot-picking in 04_add_playlists: n-th copy -> n-th existing."""
    slot = existing.get(name, [])
    t = slot[n] if n < len(slot) and slot[n]["editable"] else None
    return t["id"] if t else "CREATE"


print("\nsame-named playlists (this was a real bug)")
one = {"Lounge": [{"id": "p.1", "editable": True}]}
check("first copy fills the existing one", pick(one, "Lounge", 0), "p.1")
check("second copy is created separately", pick(one, "Lounge", 1), "CREATE")
check("system playlist is never written to",
      pick({"Favorite Songs": [{"id": "p.sys", "editable": False}]}, "Favorite Songs", 0),
      "CREATE")
check("unknown name is created", pick(one, "Techno", 0), "CREATE")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
