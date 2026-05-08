# spotify-repairer

A small CLI that finds tracks in your Spotify liked songs and playlists that
have become unavailable in your market and adds playable alternatives next to
them, with your confirmation per-track.

## Why this exists

Tracks in a Spotify library quietly turn grey over time. The usual causes are
labels moving distributor (the recording is re-uploaded under a new Spotify
track ID and the old ID stops resolving), regional licensing changes, and
artist/label takedowns. The longer you've curated a library, the more of
these you accumulate — and because grey tracks keep their title and position,
it's easy to miss them until you try to play one.

This tool walks the library, finds the grey tracks, and proposes a playable
alternative for each one. Two design choices follow from how Spotify behaves:

- **Non-destructive.** Originals are kept, not replaced. A grey track often
  comes back on its own — when the same recording is re-ingested under a new
  ID, Spotify re-routes the old ID to it and the track plays again with no
  intervention. Deleting the original throws away that recovery path.
- **Per-track confirmation.** ISRC matches are safe to auto-apply in
  principle, but name+artist matches can land on a re-master, live cut, or
  remix. The tool surfaces a confidence level and lets you decide rather
  than silently rewriting the library. A dry-run preview mode is available
  if you want to see what *would* change before committing to anything.

## How it matches replacements

For each unplayable track, candidates are classified:

1. **Exact** — same **ISRC** (industry-standard recording identifier). Same
   recording, just ingested under a different Spotify track ID. This is the
   common case when a label moves distributors.
2. **High** — same name and artist, with duration within ±2 seconds.
3. **Low** — same name and artist, but duration differs (likely a re-master,
   live version, or a different cut). Auto-skipped — too risky to apply
   without listening, and you'd want to pick the right version yourself.

Exact and high matches are surfaced for confirmation (`y/n/q`); low matches
are reported in the post-scan summary so you know they exist but aren't
prompted on.

## What gets changed

Repairs are **non-destructive — originals are never removed**, only a playable
alternative is added alongside:

- **Playlists** — the replacement is inserted immediately after the original.
- **Liked songs** — the replacement is added (Spotify orders liked songs by
  date added, so it appears at the top, not next to the original).

Every applied repair is logged to `repairs.json` in the project root with the
original/replacement IDs, source, position, and confidence level. The tool
doesn't read this log for idempotency — it derives that from the library
itself (replacements already present in the source are skipped automatically).
The log is purely an audit trail you can inspect manually.

## Setup

Requires Python 3.10+. You need a Spotify Developer app to authorize this
tool against your account — setup is one-time and takes ~2 minutes.

1. Open <https://developer.spotify.com/dashboard> and create an app.
   - Name / description: anything.
   - Redirect URI: `http://127.0.0.1:3000` — paste exactly. Spotify
     deprecated `localhost`, so it must be the loopback IP. Click **Add**,
     then **Save** at the bottom; missing the Save step produces an
     `INVALID_CLIENT: Invalid redirect URI` error on first run.
   - Which APIs: **Web API**.
   - After saving, open the app's **Settings** page to find your
     **Client ID**. (No client secret needed — this tool uses the PKCE
     flow, which is why a public Client ID alone is enough.)
2. Copy `.env.example` to `.env` and set:

   ```bash
   CLIENT_ID=<paste your Client ID here>
   ```

3. Install:

   ```bash
   pip install -e .
   ```

## Usage

```bash
spotify-repairer
```

or

```bash
python -m spotify_repairer
```

On first run, your browser opens Spotify's authorization page asking you to
grant library and playlist read/write permissions. After you accept, Spotify
redirects to `http://127.0.0.1:3000/...` — the tool spins up a one-shot
local listener on that port to catch the redirect, complete the PKCE code
exchange, and cache the resulting token in `.cache`. Subsequent runs reuse
the cached token and don't re-prompt.

An arrow-key menu offers:

- **Preview liked songs (dry run)** — scan and show what would be added,
  without writing anything.
- **Preview playlist (dry run)** — same, for a chosen playlist.
- **Repair liked songs** — scan and prompt to apply each suggestion.
- **Repair playlist** — same, for a chosen playlist.
- **Exit**.

Use ↑/↓ to navigate, Enter to select, `q` to cancel. In repair mode, each
unplayable track shows the suggested replacement as a per-field diff against
the original, then prompts `y` to add it, `n` to skip, or `q` to stop scanning.

### Caveats

- **Playlists grow.** Each repair inserts an additional track without removing
  the original, so playlists with many unplayable tracks will get longer.
- **Liked-songs replacements appear at the top** (Spotify API can't insert at
  a specific date-added position).
- **Audio fingerprinting isn't possible** through Spotify's public API —
  ISRC is the closest thing available. Duration is used as a secondary signal.
- **No automatic undo.** `repairs.json` records every change you've applied;
  removing a repair is currently a manual operation. Spotify's
  [playlist recovery page](https://www.spotify.com/account/recover-playlists/)
  keeps 90 days of playlist snapshots if you need to roll back wholesale.

## Development

```bash
pip install -e ".[dev]"
pytest
```
