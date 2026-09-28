# Restore your Apple Music library after changing country

![Restore your Apple Music library after changing country](docs/assets/preview.png)

**Русская версия: [README.ru.md](README.ru.md)**

You changed your Apple ID's country — or moved to a new Apple ID — and your
Apple Music library is empty. Playlists gone, albums gone, thousands of songs
gone. Apple does not migrate libraries across storefronts, and support cannot
bring it back.

This restores it from the data export Apple gives you for free.

> Verified 6 August 2026 against Apple Music API v1, migrating a 10,532-track
> library from the `RU` storefront to `US`. **83.6%** of tracks that had a
> catalog ID were recovered, along with all playlists that still had content.

---

## Why the obvious approaches fail

**"Just import the JSON into the Music app."** The Music app's `add` command
takes files from disk. It cannot look up streaming tracks by title. AppleScript
has no command to add a catalog track to your library at all.

**"Use a playlist transfer service."** Those match on `artist — title` text.
That works for playlists and fails on remixes, live versions, radio edits and
non-Latin titles. They also do not restore a library — only playlists.

**"The IDs are right there in the export, just add them."** They are, and this
is the real trap: **Apple Music catalog IDs are storefront-specific.** The same
song has a different numeric ID in every country. Roughly a third of the IDs in
your export mean nothing in a different storefront.

## What this does instead

Your export contains real catalog IDs, not just text. That allows exact
matching — a different class of accuracy than any text-based tool. The
remaining problem is translating IDs between storefronts, which is done with a
three-step cascade, most reliable first:

| Step | How it works | Reach | Accuracy |
|---|---|---:|---:|
| **1. ISRC** | source catalog → ISRC → `filter[isrc]` in target | 24% | **100%** |
| **2. Direct ID** | some IDs happen to be identical across storefronts | 63–66% | 96% |
| **3. Equivalents** | `filter[equivalents]`, Apple's own converter | 76% | 87% |

Every match is then verified against the original track duration. This matters
more than it sounds: the equivalents converter will happily return a
7-minute extended mix when you asked for a 3-minute radio edit. Duration is
what catches it.

Anything that does not pass goes to `manual_review.csv` for you to decide,
instead of being silently added wrong.

See [docs/FINDINGS.md](docs/FINDINGS.md) for the full measurements, including
the quirks that cost the most time to discover.

## What you get back

- **Tracks** — everything still present in your new storefront
- **Playlists** — with original track order preserved
- **Albums and artists** — these rebuild themselves from the tracks, no extra work
- **Apple's curated playlists** you were subscribed to

## What is gone for good

Be realistic before you start:

- **Personal uploads (iTunes Match).** When the subscription lapses, Apple
  deletes the cloud copies. The library keeps the titles, so they appear as
  greyed-out entries that will not play. Only a local backup can bring these back.
- **Purchases** stay tied to the old Apple ID. You can re-download them there,
  but they do not transfer.
- **Play counts, date added, ratings history.** The API cannot write them.
- **Tracks pulled from your target storefront's catalog.** Nothing to point at.
- **Smart playlists and Genius mixes.** Rules are not in the export.

---

## Contact

Questions, bug reports, or measurements from your own library: open an issue,
or write to **vdovikov@me.com**.

## Requirements

- Python 3.9+ (no third-party packages — standard library only)
- **An active Apple Music subscription on the new account.** Without it, adding
  catalog tracks is impossible.
- Your Apple data export

## Get your data export

1. Go to [privacy.apple.com](https://privacy.apple.com) → *Request a copy of your data*
2. Select **Apple Media Services information**
3. Wait — Apple takes up to 7 days
4. Unzip and find the folder `Apple_Media_Services/Apple Music Activity`

## Get your tokens

Two tokens are needed. Open [music.apple.com](https://music.apple.com) in
Chrome or Safari, signed in to the **new** account.

**Option A — official (Apple Developer Program, $99/year).** Generate a
developer token as described in
[Apple's documentation](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens),
then obtain a Music User Token via MusicKit JS. This is the route Apple
sanctions.

**Option B — the web player's own token.** Open the browser console and run:

```js
copy(JSON.stringify({
  developer_token: MusicKit.getInstance().developerToken,
  music_user_token: document.cookie.match(/media-user-token=([^;]+)/)[1]
}, null, 2))
```

That copies a ready JSON to your clipboard. If `MusicKit` is undefined, play
any track for a second and retry.

> **Note on Option B.** It reuses the token Apple's own web player runs on.
> You are reaching your own library with your own key, and nothing here
> circumvents payment or accesses anyone else's data — but it is not a use
> Apple's terms provide for. Use it at your own discretion. Tokens last months;
> never commit `tokens.json` or share it, as it grants full access to your library.

Save the result:

```bash
cp tokens.example.json tokens.json
# paste your two values into tokens.json
```

---

## Usage

Run the steps in order. Everything before step 3 is read-only.

```bash
EXPORT="/path/to/Apple_Media_Services/Apple Music Activity"

# 1. Check tokens and estimate what is recoverable (~2 min, read-only)
python3 01_check.py --export "$EXPORT" --from ru --to us

# 2. Resolve the whole library, produce CSV reports (~30-60 min, read-only)
python3 02_resolve.py --export "$EXPORT" --from ru --to us

# --- open work/manual_review.csv and look at the questionable matches ---

# 3. Add the reliable matches — preview first, then apply
python3 03_add_tracks.py --export "$EXPORT"
python3 03_add_tracks.py --export "$EXPORT" --apply

# 4. Rebuild playlists — same pattern
python3 04_add_playlists.py --export "$EXPORT"
python3 04_add_playlists.py --export "$EXPORT" --apply
```

Both writing steps preview by default and do nothing until you pass `--apply`.

Set `--from` to the country your library came from and `--to` to your new one.

### If something goes wrong

```bash
python3 03_add_tracks.py --undo
```

Every added track is journalled, so this removes exactly what the tool added
and nothing else. Interrupted runs resume from where they stopped — just run
the same command again.

### Adding tracks you approved manually

Edit `work/manual_review.csv`, delete the rows you do not want, then:

```bash
python3 03_add_tracks.py --include-manual work/manual_review.csv --apply
```

### When the old Apple ID is gone

Blocked, deleted, or simply not yours any more: then there is no token for it
and no data export either. What is left is the Music app on a Mac that was
signed in to it — it still holds the library metadata, and AppleScript can
read it.

That snapshot has **no catalog IDs** — the Music app never exposes them — so
the ID cascade above cannot run. Tracks are matched by text and duration
instead, which is less exact and much slower: Apple's `/search` endpoint gives
out after a few hundred requests and then answers 429 for a long while.
Step 2 below backs off on its own and resumes from a checkpoint.

Take the snapshot **before** the Music app signs out of the old account,
while the library is still there.

```bash
# 0. Snapshot the Mac's Music library into export-shaped JSON (read-only)
python3 00_dump_local.py --out work/local-export

# 1. Diff it against the new account's library -> work/delta.json (read-only)
#    The new library is cached in work/ru-library.json; add --refresh on any
#    re-run after step 3, or tracks added since come back as missing
python3 01_diff_ru.py --snapshot work/local-export

# 2. Search the target catalog for what is missing -> work/tracks.csv
#    (read-only, can take hours; re-run the same command to resume)
python3 02_find_ru.py --delta work/delta.json --store ru

# 3. Add the found tracks — preview first, then apply
python3 03_add_tracks.py --export work/local-export
python3 03_add_tracks.py --export work/local-export --apply

# 4. Rebuild playlists — same pattern; --only restores a single one
python3 04_add_playlists.py --export work/local-export
python3 04_add_playlists.py --export work/local-export --apply
```

`--store` in step 2 is the country of the new account. Matching is strict by
default (title ≥ 0.90, artist ≥ 0.80, duration within 5 s); `--loose` accepts
on duration alone and is worth a manual look before step 3.

**A playlist is on the Mac but never reaches the phone?** Look inside it for
tracks of kind "Internet audio stream" with no address. They are what is left
of tracks that sat in the playlist without being in the library, and they
appear to keep the whole playlist out of the cloud. Do not repair the Mac
copy: recreate the playlist with step 4 (`--only "Name"`), wait until it
arrives on the Mac as a second playlist, then delete the old one. Details in
[FINDINGS](docs/FINDINGS.md#playlists-that-stay-on-the-mac-and-never-reach-the-phone).

---

## Notes and gotchas

- **Library writes only work on `amp-api.music.apple.com`.** On
  `api.music.apple.com`, `DELETE` returns 401 while `GET` and `POST` succeed.
  Hours were lost to this.
- **HTTP 202 does not mean success.** Apple accepts the request and may
  silently not add the track — its licence forbids library adds even though it
  streams fine. About 3.5% of adds in testing. There is no workaround.
- **`filter[equivalents]` takes one ID per request.** The ISRC route batches
  up to 100, which is why the cascade puts it first.
- **Greyed-out tracks with no `playParams`** are dead entries, not restored
  music. Do not count them as already-present when deduplicating.
- Apple's rate limit is around 20 requests/second; this tool stays near 3.

## Related work

[thatmanmatt/Add-to-Apple-Music](https://github.com/thatmanmatt/Add-to-Apple-Music)
solves the same problem — a library lost when moving country — by parsing the
`Library.xml` you export from the Music app and re-adding albums.

The difference is the input. That approach needs a Mac with the old library
still loaded, and matches at album level. This one works from Apple's privacy
data export, which you can request after everything is already gone, and
matches individual tracks by catalog ID with a duration check. If you still
have a working Music.app library, that tool is the simpler path.

## Tests

```bash
python3 test_logic.py
```

Covers the decision logic that silently corrupts a migration when wrong — the
duration check, playlist ordering, and same-named playlists. No network, no
tokens, no account required.

## Roadmap

Not implemented yet — contributions welcome:

- Restoring loved/favourited tracks (`PUT /v1/me/ratings/songs/{id}`)
- Re-downloading purchases from the old Apple ID

## Disclaimer

Not affiliated with or endorsed by Apple. Provided as-is under the MIT licence.
It only ever touches your own library, and step 3 is the first step that writes
anything. Read [docs/FINDINGS.md](docs/FINDINGS.md) before running it on a
library you care about.
