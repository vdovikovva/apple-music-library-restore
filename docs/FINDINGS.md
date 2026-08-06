# Measurements and API behaviour

Everything here was measured on a real migration, **6 August 2026**, Apple Music
API v1, storefront `RU` → `US`. Source library: 10,532 tracks accumulated
2012–2026. Sample sizes are stated for every number.

Apple changes this API without notice. If you are reading this much later,
re-measure with `01_check.py` before trusting anything below.

---

## 1. Catalog IDs are storefront-specific

The single fact that breaks naive migration. The same song carries a different
numeric ID in each country's storefront, because different storefronts may
carry different releases of the same recording.

Measured on two independent random samples from the same library:

| Sample | Source IDs valid in target storefront |
|---|---:|
| n = 100 | 63% |
| n = 150 | 66% |

So roughly **one ID in three is meaningless** in the target storefront.

## 2. Three ways to translate an ID, and they are not equivalent

### `filter[equivalents]`

```
GET /v1/catalog/{target}/songs?filter[equivalents]={source_id}
```

Apple's own cross-storefront converter. Target storefront in the path, source
ID in the filter.

- **Reach:** 39 of 51 unresolved tracks (76%)
- **Accuracy:** 87% within 5s of the original duration
- **Accepts one ID per request** — no batching, and this dominates runtime

### ISRC

```
GET /v1/catalog/{source}/songs?ids={source_id}      → attributes.isrc
GET /v1/catalog/{target}/songs?filter[isrc]={isrc}
```

- **Reach:** 12 of 51 (24%)
- **Accuracy:** **100%** — every match within 2s of the original
- **Batches up to 100 IDs**, so it costs almost nothing

### Head to head

On the same 51 tracks, where both methods found something:

| | |
|---|---:|
| Agreed on the same target ID | 5 |
| **Disagreed** | **7** |
| Found only by ISRC | 0 |
| Found only by equivalents | 27 |

Where they disagreed, ISRC was right every time — its matches were duration-exact,
the converter's were not. ISRC never found anything the converter missed, but
it never lied either.

**Conclusion: cascade, do not choose.** ISRC first for accuracy, direct ID next,
converter last for reach — and verify everything against duration.

## 3. The converter swaps track versions

This is the failure mode worth knowing about. Real examples:

```
Dario G — Sunchyme (Radio Edit)       → Sunchyme                        off by 144s
Arty — The Wall (Radio Edit)          → The Wall (Original Extended Mix) off by 234s
Gouryella — Gouryella                 → Gouryella                        off by 422s
Pet Shop Boys — Rent (Seven-Inch Mix) → Rent                             off by  92s
```

Consistently, the radio edit is replaced by the original or extended mix. If
your library is dance, trance or electronic, this hits a meaningful share of it.

Direct-ID matches are not immune either — 4% were the wrong version, e.g.
`Sunlite & Aki — Boogie Time` → `Boogie Time (Radio House Mix)`, off by 126s.

**Duration is the only cheap discriminator.** Thresholds used here:

| Difference | Verdict | Action |
|---|---|---|
| ≤ 2s | exact | add |
| ≤ 5s | close | add |
| ≤ 30s | suspect | human review |
| > 30s | different | human review |

Note that a transliterated artist name is not itself a problem —
`Каста → Kasta` with a 8s difference is usually the same recording from a
different release. Judge by duration, not by the name.

## 4. Library writes only work on a different host

| Host | GET | POST | DELETE |
|---|---|---|---|
| `api.music.apple.com` | ✅ | ✅ | **401** |
| `amp-api.music.apple.com` | ✅ | ✅ | ✅ 204 |

The web player uses `amp-api`. `DELETE` against the documented host returns 401
with an empty body regardless of headers — tried with and without `Origin`,
`Referer`, and a browser `User-Agent`. Same token, same moment, works on
`amp-api`.

Without this there is no rollback, which turns a large migration into a
one-way operation.

## 5. HTTP 202 does not mean the track was added

`POST /v1/me/library?ids[songs]=...` returns `202 Accepted` and processes
asynchronously. Some tracks never appear.

- 6,665 tracks sent → 6,428 confirmed in library
- **237 (3.5%) accepted and silently dropped**
- Retrying changes nothing; sending them one at a time changes nothing
- Those tracks exist in the target catalog and stream fine — verified via
  `GET /v1/catalog/{sf}/songs/{id}`, `playParams` present

The pattern was seasonal compilations and live albums (Sinatra Christmas
recordings, live sets). Read as a per-release licence that permits streaming
but not library adds. **Always verify after adding — do not trust 202.**

## 6. Dead library entries

Personal uploads (iTunes Match) whose subscription lapsed leave behind entries
with metadata but **no `playParams`**:

```json
{ "name": "Artist - Title", "durationInMillis": 176195,
  "albumName": "", "trackNumber": 0 }
```

No artwork, no artist name, nothing playable. They appear greyed out in the
Music app. In the migrated account, 218 of 243 pre-existing entries were of this
kind.

Two consequences:

1. **Do not count them when deduplicating** — otherwise you skip adding the
   real track because a corpse with the same name is present.
2. **Personal uploads are unrecoverable** once the cloud copies are deleted.
   Only a local backup helps.

## 7. Albums and artists rebuild themselves

Adding tracks pulled in **3,490 albums and 2,284 artists** automatically —
more than the 2,625 and 1,688 in the original library, because tracks bring
their whole album with them.

A separate album-import pass was planned and turned out to be unnecessary.
`02_resolve.py` still resolves album IDs, but importing them explicitly is
usually redundant and risks bloating the library with tracks you never had.

## 8. Playlists

- Track order is preserved by `Playlist Item Identifiers` in the export
- Duplicated entries are common — one playlist had every track listed twice
  (104 entries, 59 unique)
- Playlists that already exist and are editable should be **filled, not
  recreated**, or you end up with duplicates
- `Favorite Songs` is system-owned (`canEdit: false`) and cannot be written to;
  it fills from ratings
- Apple's curated playlists re-add by `Public Playlist Identifier`; 41 of 47
  still existed
- **Beware same-named playlists.** Keying a journal by name alone silently
  skips the second one. Key by name + index.

The export does not contain contents for every playlist: 150 of 190 user
playlists came through with names only.

## 9. Final tally for this migration

| | Count | Share |
|---|---:|---:|
| Tracks in export | 10,532 | |
| — with a catalog ID | 8,511 | 81% |
| — personal uploads (unrecoverable) | 2,021 | 19% |
| Resolved reliably | 7,115 | 83.6% |
| — unique after collapsing duplicates | 6,662 | |
| Sent to library | 6,665 | |
| **Live in library after migration** | **6,453** | |
| Needing manual review | 279 | 3.3% |
| Not in target catalog | 1,108 | 13.0% |

By method: direct ID 4,791 · equivalents 1,619 · ISRC 628.

Playlists: 34 user playlists restored (1,215 of 1,632 track slots, 78%),
plus 41 Apple curated playlists.

Runtime: ~51 min to resolve 8,511 tracks, ~6 min to add 6,665, at ~3 req/s.

## 10. Rate limits

Apple's documented ceiling is around 20 requests/second per user, and library
endpoints are stricter than catalog ones. This tool runs at ~3 req/s and saw
**zero 429s** across roughly 12,000 requests.

Network drops (SSL handshake timeouts) occurred twice in a long session — worth
retrying on `URLError`/`TimeoutError`, not just on HTTP errors.
