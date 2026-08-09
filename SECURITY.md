# Security

**Русская версия: [SECURITY.ru.md](SECURITY.ru.md)**

## What this tool has access to

It runs with two tokens you supply in `tokens.json`:

- **developer token** — identifies the client to Apple
- **music-user-token** — grants full read and write access to *your* Apple
  Music library

The second one matters. Anyone holding it can read, add to and delete from your
library until it expires (a few months).

## Rules

**Never commit `tokens.json`.** It is in `.gitignore`, but verify before every
push:

```bash
git status

# Filled-in tokens, whichever format
grep -rIn -E '"(developer_token|music_user_token)"[[:space:]]*:[[:space:]]*"[^"<]{40,}"' . \
  --exclude-dir=.git

# JWTs anywhere, e.g. pasted into a log
grep -rIn -E 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}' . --exclude-dir=.git
```

Both patterns are verified against real tokens. Note that Apple's tokens do
**not** match the common `eyJhbGciOi` prefix people grep for: the developer
token starts with `eyJ0eXAi`, and the music-user-token is not a JWT at all.
Matching on the field name rather than the value's shape avoids that trap.

**Never share the CSV files in `work/`.** They contain your full listening
history and your Apple ID number.

**Revoke a leaked user token** by signing out of Apple Music on
[music.apple.com](https://music.apple.com), which invalidates the cookie the
token came from. Then sign back in and fetch a new one.

## What the tool does and does not do

- Talks only to `api.music.apple.com` and `amp-api.music.apple.com`
- Sends no data anywhere else — no telemetry, no analytics, no third parties
- Has no dependencies beyond the Python standard library, so there is no
  supply chain to compromise
- Writes only to your library, and only in steps 3 and 4, and only with
  `--apply`

## Reporting a vulnerability

Open an issue for anything non-sensitive.

For something that could put other users' accounts at risk — a token leaking
into logs, an unintended write path — please report it privately — via
[GitHub Security Advisories](../../security/advisories/new) or by email to
**vdovikov@me.com** — rather than a public issue.

## On the terms-of-service question

The documented way to obtain a developer token is the Apple Developer Program.
This tool also documents taking the token the Apple Music web player uses.

That path reaches your own library with your own credentials, circumvents no
payment and touches no one else's data — but it is not a use Apple's terms
provide for. It is offered as an informed choice, and the official route is
listed first in the README. Neither this project nor its authors are affiliated
with Apple.
