# spotify-repairer

A small CLI that finds tracks in your Spotify liked songs and playlists that
have become unavailable in your market and adds playable alternatives next to
them, with your confirmation per-track.

## How it matches replacements

For each unplayable track, candidates are ranked:

1. **Exact** — same **ISRC** (industry-standard recording identifier). Same
   recording, just ingested under a different Spotify track ID. This is the
   common case when a label moves distributors.
2. **High** — same name and artist, with duration within ±2 seconds.
3. **Low** — same name and artist, but duration differs (likely a re-master,
   live version, or a different cut). Flagged so you can decide.

You're shown the confidence level before each suggestion and asked `y/n/q`.

## What gets changed

Repairs are **non-destructive — originals are never removed**, only a playable
alternative is added alongside:

- **Originals are never deleted.** If the unplayable track ever becomes
  playable again (via the same ID or via a same-ISRC re-upload Spotify
  re-routes), it just works — no manual recovery needed.
- **Playlists** — the replacement is inserted immediately after the original.
- **Liked songs** — the replacement is added (Spotify orders liked songs by
  date added, so it appears at the top, not next to the original).

Every applied repair is logged to `repairs.json` in the project root with the
original/replacement IDs, source, position, and confidence level. The tool
doesn't read this log for idempotency — it derives that from the library
itself (replacements already present in the source are skipped automatically).
The log is purely an audit trail you can inspect manually.

## Setup

You need a Spotify Developer app to authorize this tool against your account.
Setup is one-time and takes ~2 minutes.

1. Open <https://developer.spotify.com/dashboard> and create an app.
   - Name / description: anything.
   - Redirect URI: `http://127.0.0.1:3000` (Spotify deprecated `localhost`
     redirects — must be a loopback IP).
   - Which APIs: **Web API**.
2. Copy `.env.example` to `.env` and paste in your app's **Client ID**.
   (No client secret needed — this tool uses the PKCE flow.)
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

On first run, your browser will open Spotify's authorization page. The token
is cached in `.cache` so subsequent runs don't re-prompt.

Menu:

- **1** — scan liked songs
- **2** — scan a playlist
- **x** — exit

For each unplayable track found, you'll see the suggested replacement and can
press `y` to add it, `n` to skip, or `q` to stop scanning.

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
