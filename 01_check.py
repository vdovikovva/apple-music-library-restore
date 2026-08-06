#!/usr/bin/env python3
"""
Step 1 — sanity check. Read-only, nothing is written to your library.

Verifies your tokens work, then measures on a random sample how much of your
library can actually be recovered. Run this before anything else: it takes
two minutes and tells you whether the rest is worth doing.

    python3 01_check.py --export "/path/to/Apple Music Activity"
"""

import random
import sys

from common import (ACCEPTED, Client, base_parser, duration_delta,
                    load_library_tracks, load_tokens, require_export, verdict)

SAMPLE = 100
EQUIV_SAMPLE = 25


def main():
    p = base_parser(__doc__)
    p.add_argument("--sample", type=int, default=SAMPLE,
                   help=f"how many tracks to test (default: {SAMPLE})")
    args = p.parse_args()
    require_export(args)

    client = Client(load_tokens(), pause=args.pause)

    print("=" * 66)
    print("STEP 1 — CHECK  (read-only)")
    print("=" * 66)

    # 1. tokens
    sf = client.storefront()
    if not sf:
        sys.exit("  ✗ Tokens rejected. Get fresh ones — see README.")
    print(f"  ✓ Tokens accepted. Account storefront: {sf.upper()}")
    if sf != args.dst:
        print(f"  ! You passed --to {args.dst}, but the account is in {sf}.")
        print(f"    Unless you know better, use --to {sf}")

    _, with_id, without = load_library_tracks(args)
    print(f"\n  Library tracks with a catalog ID: {len(with_id)}")
    print(f"  Personal uploads (no catalog ID):  {len(without)}  — not recoverable")

    random.seed(42)
    sample = random.sample(with_id, min(args.sample, len(with_id)))

    # 2. how many source IDs work in the target storefront as-is
    print(f"\n[A] Source IDs tried directly in '{args.dst}' — sample of {len(sample)}")
    found, missing = {}, []
    for i in range(0, len(sample), 10):
        chunk = sample[i:i + 10]
        by_id, _ = client.catalog_batch(args.dst, "songs", "ids",
                                        [t["src_id"] for t in chunk])
        for t in chunk:
            (found.__setitem__(t["src_id"], by_id[t["src_id"]])
             if t["src_id"] in by_id else missing.append(t))
    direct_pct = 100 * len(found) / len(sample)
    print(f"    work directly:      {len(found)}/{len(sample)}  ({direct_pct:.0f}%)")
    print(f"    need conversion:    {len(missing)}")

    bad = [(t, found[t["src_id"]]) for t in sample
           if t["src_id"] in found
           and (duration_delta(t, found[t["src_id"]]["attributes"]) or 0) > 5000]
    if bad:
        print(f"    ! {len(bad)} matched but the duration is off — likely a different version:")
        for t, d in bad[:3]:
            print(f"        {t['artist']} — {t['title']}")
            print(f"          -> {d['attributes']['artistName']} — {d['attributes']['name']}")

    # 3. equivalents converter
    print(f"\n[B] filter[equivalents] converter")
    resolved = []
    if missing:
        for t in missing[:EQUIV_SAMPLE]:
            data = client.get(f"/v1/catalog/{args.dst}/songs",
                              {"filter[equivalents]": t["src_id"]}).get("data", [])
            if data:
                resolved.append((t, data[0],
                                 duration_delta(t, data[0]["attributes"])))
        tried = min(len(missing), EQUIV_SAMPLE)
        good = sum(1 for _, _, d in resolved if verdict(d) in ACCEPTED)
        print(f"    converted:          {len(resolved)}/{tried}")
        print(f"    of those reliable:  {good}/{max(len(resolved), 1)}")
        wrong = [(t, d, dl) for t, d, dl in resolved if verdict(dl) == "different"]
        if wrong:
            print(f"    ! {len(wrong)} came back as a different version:")
            for t, d, dl in wrong[:3]:
                print(f"        {t['artist']} — {t['title']}")
                print(f"          -> {d['attributes']['artistName']} — {d['attributes']['name']}"
                      f"  (off by {dl / 1000:.0f}s)")
    else:
        print("    not needed — everything resolved directly")

    # 4. ISRC route
    print(f"\n[C] ISRC route (most accurate, narrower reach)")
    isrc_hits = 0
    subset = missing[:EQUIV_SAMPLE]
    for t in subset:
        src = client.get(f"/v1/catalog/{args.src}/songs",
                         {"ids": t["src_id"]}).get("data", [])
        if not src:
            continue
        isrc = src[0]["attributes"].get("isrc")
        if not isrc:
            continue
        if client.get(f"/v1/catalog/{args.dst}/songs",
                      {"filter[isrc]": isrc}).get("data", []):
            isrc_hits += 1
    print(f"    found via ISRC:     {isrc_hits}/{len(subset)}")

    # verdict
    print("\n" + "=" * 66)
    conv_rate = len(resolved) / max(min(len(missing), EQUIV_SAMPLE), 1)
    estimate = direct_pct + (100 - direct_pct) * conv_rate
    print(f"  Estimated recovery: ~{estimate:.0f}% of {len(with_id)} tracks"
          f"  (~{int(len(with_id) * estimate / 100)} tracks)")
    print(f"  Plus {len(without)} personal uploads that cannot be recovered.")
    print("\n  This is a sample-based estimate. Run 02_resolve.py for exact numbers.")


if __name__ == "__main__":
    main()
