"""Interactive CLI for finding and replacing unavailable Spotify tracks."""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterator, NamedTuple

import dotenv
import requests
import spotipy
from blessed import Terminal

from spotify_repairer import manifest
from spotify_repairer.repair import (
    Confidence,
    Match,
    Track,
    find_replacement,
    parse_track,
)

REDIRECT_URI = "http://127.0.0.1:3000"
BATCH_SIZE = 50
PLAYLIST_PAGE_SIZE = 50
REPLACEMENT_SEARCH_WORKERS = 10
MAX_REPLACEMENT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5
MIN_CONFIDENCE = Confidence.HIGH
TRANSIENT_NETWORK_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    ConnectionResetError,
)
SCOPES = (
    "user-library-read user-library-modify "
    "playlist-read-private playlist-modify-public playlist-modify-private"
)
LIKED_SOURCE = "liked_songs"


def playlist_source(playlist_id: str) -> str:
    """Build the source identifier used in the audit log for a playlist."""
    return f"playlist:{playlist_id}"


# --- auth & user info -------------------------------------------------------


def login() -> spotipy.Spotify:
    dotenv.load_dotenv()
    client_id = os.environ.get("CLIENT_ID")
    if not client_id:
        sys.exit(
            "Missing CLIENT_ID. Copy .env.example to .env and set your Spotify "
            "app's Client ID. See README.md for setup."
        )
    return spotipy.Spotify(
        auth_manager=spotipy.SpotifyPKCE(
            client_id=client_id,
            redirect_uri=REDIRECT_URI,
            scope=SCOPES,
        )
    )


def get_user_market(sp: spotipy.Spotify) -> str:
    user = sp.current_user() or {}
    return user.get("country") or "US"


# --- pagination -------------------------------------------------------------


def iterate_tracks(
    fetch_page: Callable[..., dict[str, Any] | None],
    page_args: dict[str, Any],
    market: str,
) -> Iterator[tuple[int, Track]]:
    """Yield (position, track) pairs across all pages of a paginated endpoint."""
    offset = 0
    while True:
        results = fetch_page(
            **page_args, limit=BATCH_SIZE, offset=offset, market=market
        )
        items = (results or {}).get("items") or []
        if not items:
            return
        for idx, item in enumerate(items):
            track = parse_track(item)
            if track is not None:
                yield offset + idx, track
        if len(items) < BATCH_SIZE:
            return
        offset += BATCH_SIZE


def _fetch_all_playlists(sp: spotipy.Spotify) -> list[dict[str, Any]]:
    """Page through all of the user's playlists, not just the first page."""
    playlists: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=PLAYLIST_PAGE_SIZE, offset=offset)
        items = (page or {}).get("items") or []
        if not items:
            return playlists
        playlists.extend(items)
        if len(items) < PLAYLIST_PAGE_SIZE:
            return playlists
        offset += PLAYLIST_PAGE_SIZE


# --- formatting -------------------------------------------------------------


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return "?"
    total_seconds = ms // 1000
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _confidence_label(confidence: Confidence) -> str:
    return {
        Confidence.EXACT: "exact match — same ISRC, same recording",
        Confidence.HIGH: "high confidence — matching name, artist, and duration",
        Confidence.LOW: "low confidence — duration differs, may be a different version",
    }[confidence]


def _confidence_color(term: Terminal, confidence: Confidence) -> Callable[[str], str]:
    return {
        Confidence.EXACT: term.green,
        Confidence.HIGH: term.green,
        Confidence.LOW: term.red,
    }[confidence]


def _track_lines(track: Track) -> list[str]:
    lines = [
        f"    Artists:  {', '.join(track.artists)}",
        f"    Title:    {track.name}",
        f"    Album:    {track.album or '?'}",
        f"    Length:   {_format_duration(track.duration_ms)}",
    ]
    if track.isrc:
        lines.append(f"    ISRC:     {track.isrc}")
    return lines


class DiffLine(NamedTuple):
    text: str
    changed: bool


def _diff_track_lines(original: Track, replacement: Track) -> list[DiffLine]:
    """Render each replacement field as either '<value> (no change)' or
    '<old> → <new>'. The `changed` flag lets the caller emphasize the
    differences visually.

    Comparison is case-insensitive (via `casefold`) so trivial capitalization
    drift between Spotify ingests of the same recording — e.g. "Time for Us"
    vs "Time For Us" — doesn't get flagged as a meaningful change.
    """

    def diff(label: str, orig: str, repl: str) -> DiffLine:
        if orig.casefold() == repl.casefold():
            return DiffLine(f"    {label}  {orig} (no change)", changed=False)
        return DiffLine(f"    {label}  {orig} → {repl}", changed=True)

    lines = [
        diff("Artists:", ", ".join(original.artists), ", ".join(replacement.artists)),
        diff("Title:  ", original.name, replacement.name),
        diff("Album:  ", original.album or "?", replacement.album or "?"),
        diff(
            "Length: ",
            _format_duration(original.duration_ms),
            _format_duration(replacement.duration_ms),
        ),
    ]
    if replacement.isrc:
        lines.append(diff("ISRC:   ", original.isrc or "?", replacement.isrc))
    return lines


def _print_unplayable(track: Track) -> None:
    print()
    print("─" * 60)
    print("Unplayable in your market:")
    for line in _track_lines(track):
        print(line)


def _print_replacement(
    term: Terminal,
    original: Track,
    match: Match,
    label: str = "Suggested replacement:",
) -> None:
    """Print the replacement as a per-field diff against the original.

    Heading and changed lines get the confidence color; unchanged lines stay
    plain so the diff stands out without coloring the whole block.
    """
    color = _confidence_color(term, match.confidence)
    print()
    print(color(label))
    for line in _diff_track_lines(original, match.track):
        print(color(line.text) if line.changed else line.text)
    print(color(f"    Match:    {_confidence_label(match.confidence)}"))


# --- prompts ----------------------------------------------------------------


def confirm_repair(term: Terminal, original: Track, match: Match) -> str:
    _print_replacement(term, original, match)
    print("\n  [y] add replacement  [n] skip  [q] quit  ", end="", flush=True)
    with term.cbreak():
        while True:
            key = term.inkey().lower()
            if key in ("y", "n", "q"):
                print(key)
                return key


def select_from_menu(
    term: Terminal, header_lines: list[str], options: list[tuple[str, str]]
) -> str | None:
    """Render an arrow-key menu. Returns selected option id, or None on cancel."""
    if not options:
        return None
    selected = 0
    while True:
        print(term.home + term.clear, end="")
        for line in header_lines:
            print(line)
        print()
        for i, (_, label) in enumerate(options):
            if i == selected:
                print(term.reverse(f"› {label}"))
            else:
                print(f"  {label}")
        print("\n[↑/↓] navigate  [Enter] select  [q] cancel")
        with term.cbreak():
            key = term.inkey()
        if key.name == "KEY_UP":
            selected = (selected - 1) % len(options)
        elif key.name == "KEY_DOWN":
            selected = (selected + 1) % len(options)
        elif key.name == "KEY_ENTER":
            return options[selected][0]
        elif key.lower() == "q":
            return None


def select_playlist(sp: spotipy.Spotify, term: Terminal) -> tuple[str, str] | None:
    playlists = _fetch_all_playlists(sp)
    if not playlists:
        print(term.yellow("No playlists found."))
        return None
    options = [(p["id"], p["name"]) for p in playlists]
    header = [term.black_on_darkkhaki(term.center("select a playlist"))]
    chosen_id = select_from_menu(term, header, options)
    if chosen_id is None:
        return None
    name = next(label for pid, label in options if pid == chosen_id)
    return chosen_id, name


# --- core repair flow -------------------------------------------------------


def _find_replacement_with_retry(
    sp: spotipy.Spotify, track: Track, market: str
) -> Match | None:
    """Wrap find_replacement with retry on transient connection errors.

    Spotipy's built-in retry adapter handles HTTP error codes (429, 5xx) but
    not socket-level errors like ECONNRESET. Under parallel load against
    Spotify, transient resets do happen — a small backoff loop keeps a single
    blip from killing a full scan.
    """
    for attempt in range(MAX_REPLACEMENT_RETRIES):
        try:
            return find_replacement(sp, track, market)
        except TRANSIENT_NETWORK_ERRORS:
            if attempt == MAX_REPLACEMENT_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
    return None  # unreachable; satisfies type checker


def _find_replacements_parallel(
    sp: spotipy.Spotify, tracks: list[Track], market: str
) -> list[Match | None]:
    """Find replacements concurrently. Returns list aligned with input tracks.

    Tracks where the search ultimately failed (after retries) get None — the
    caller treats them the same as "no replacement found", and the count of
    failures is surfaced to the user.
    """
    matches: list[Match | None] = [None] * len(tracks)
    failed = 0
    with ThreadPoolExecutor(max_workers=REPLACEMENT_SEARCH_WORKERS) as ex:
        futures = {
            ex.submit(_find_replacement_with_retry, sp, track, market): idx
            for idx, track in enumerate(tracks)
        }
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                matches[idx] = fut.result()
            except TRANSIENT_NETWORK_ERRORS:
                failed += 1
                # leave matches[idx] as None — treated as "no replacement"
            done += 1
            print(
                f"\r  Searching for replacements... {done}/{len(tracks)}",
                end="",
                flush=True,
            )
    print(f"\r  Searched {len(tracks)} tracks for replacements.        ")
    if failed:
        print(
            f"  {failed} search(es) failed after {MAX_REPLACEMENT_RETRIES} retries "
            f"and were skipped — re-run to retry those tracks."
        )
    return matches


def _classify(
    needs_repair: list[tuple[int, Track]],
    matches: list[Match | None],
    track_ids_in_source: set[str],
) -> tuple[list[tuple[int, Track, Match]], dict[str, int]]:
    """Partition matched candidates into to-review and to-skip groups.

    Returns (to_review, skip_counts) where skip_counts has keys
    'no_replacement', 'already_added', 'below_threshold'.
    """
    to_review: list[tuple[int, Track, Match]] = []
    counts = {"no_replacement": 0, "already_added": 0, "below_threshold": 0}
    for (position, track), match in zip(needs_repair, matches):
        if match is None:
            counts["no_replacement"] += 1
        elif match.track.id in track_ids_in_source:
            counts["already_added"] += 1
        elif match.confidence < MIN_CONFIDENCE:
            counts["below_threshold"] += 1
        else:
            to_review.append((position, track, match))
    return to_review, counts


def _print_already_covered(term: Terminal, count: int) -> None:
    """Filter that runs pre-search; surfaced inline so the drop in search count
    is explained at the point it happens."""
    if count:
        print(
            term.green(
                f"  {count} already covered (same ISRC playable in source) — "
                f"no action needed."
            )
        )


def _print_post_search_summary(
    term: Terminal, counts: dict[str, int], to_review: int
) -> None:
    """Account for every track that came back from the search — none of these
    require user input. Counts should sum to len(needs_repair)."""
    if counts["no_replacement"]:
        print(
            term.yellow(
                f"  {counts['no_replacement']} had no available replacement — skipped."
            )
        )
    if counts["below_threshold"]:
        print(
            term.yellow(
                f"  {counts['below_threshold']} below the "
                f"{MIN_CONFIDENCE.value} confidence threshold — skipped."
            )
        )
    if counts["already_added"]:
        print(
            term.green(
                f"  {counts['already_added']} already repaired in a previous run "
                f"— skipped."
            )
        )
    if to_review:
        print(term.bold(f"  → {to_review} to review."))


def _collect_repairs(
    sp: spotipy.Spotify,
    term: Terminal,
    market: str,
    fetch_page: Callable[..., dict[str, Any] | None],
    page_args: dict[str, Any],
    dry_run: bool,
) -> list[tuple[int, Track, Match]]:
    """Scan a source, prompt the user per unplayable track, return confirmed repairs.

    Idempotency comes from the library state itself, not a manifest:
      - If an unplayable track's ISRC matches a playable track already in the
        source, no repair is needed (you already have the recording).
      - If the search returns a replacement whose track ID is already in the
        source, it was almost certainly added by a previous run — skip it.

    Replacements are searched in parallel after the full scan completes — Spotify
    search is the slow part, and parallelizing turns sequential N×latency into
    roughly N/workers×latency.

    In dry_run mode the proposed replacement is printed but no prompt is shown
    and the returned list is always empty — caller will skip the apply step.
    """
    unplayable: list[tuple[int, Track]] = []
    track_ids_in_source: set[str] = set()
    playable_isrcs: set[str] = set()
    scanned = 0
    for position, track in iterate_tracks(fetch_page, page_args, market):
        scanned += 1
        track_ids_in_source.add(track.id)
        if track.is_playable:
            if track.isrc:
                playable_isrcs.add(track.isrc)
        else:
            unplayable.append((position, track))
        if scanned % 25 == 0:
            print(
                f"\r  Scanned {scanned} tracks "
                f"({len(unplayable)} unplayable so far)...",
                end="",
                flush=True,
            )
    print(
        f"\r  Scanned {scanned} tracks ({len(unplayable)} unplayable).        "
    )

    needs_repair: list[tuple[int, Track]] = []
    already_covered = 0
    for position, track in unplayable:
        if track.isrc and track.isrc in playable_isrcs:
            already_covered += 1
        else:
            needs_repair.append((position, track))

    _print_already_covered(term, already_covered)

    if not needs_repair:
        if not unplayable:
            print(term.green("\nNothing to repair — all tracks are playable."))
        return []

    matches = _find_replacements_parallel(
        sp, [t for _, t in needs_repair], market
    )
    to_review, skip_counts = _classify(
        needs_repair, matches, track_ids_in_source
    )
    _print_post_search_summary(term, skip_counts, to_review=len(to_review))

    confirmed: list[tuple[int, Track, Match]] = []
    for position, track, match in to_review:
        _print_unplayable(track)
        if dry_run:
            _print_replacement(term, track, match, label="Would replace with:")
            continue
        choice = confirm_repair(term, track, match)
        if choice == "q":
            break
        if choice == "y":
            confirmed.append((position, track, match))
    return confirmed


# --- repair entrypoints -----------------------------------------------------


def repair_liked_songs(
    sp: spotipy.Spotify, term: Terminal, market: str, dry_run: bool
) -> None:
    print(term.bold("\nScanning liked songs..."))
    confirmed = _collect_repairs(
        sp, term, market, sp.current_user_saved_tracks, {}, dry_run
    )
    for _, original, match in confirmed:
        sp.current_user_saved_tracks_add([match.track.id])
        manifest.record(LIKED_SOURCE, original, match)
    if confirmed:
        print(term.green(f"\nAdded {len(confirmed)} replacement(s)."))
        print(
            "Replacements appear at the top of liked songs (Spotify orders by "
            "date added). Originals are unchanged."
        )


def repair_playlist_by_id(
    sp: spotipy.Spotify,
    term: Terminal,
    playlist_id: str,
    playlist_name: str,
    market: str,
    dry_run: bool,
) -> None:
    print(term.bold(f"\nScanning playlist: {playlist_name}"))
    source = playlist_source(playlist_id)
    confirmed = _collect_repairs(
        sp,
        term,
        market,
        sp.playlist_items,
        {"playlist_id": playlist_id},
        dry_run,
    )
    # Apply in reverse position order so earlier insertions don't shift later ones.
    for position, original, match in sorted(confirmed, key=lambda c: -c[0]):
        sp.playlist_add_items(
            playlist_id,
            [f"spotify:track:{match.track.id}"],
            position=position + 1,
        )
        manifest.record(source, original, match, position=position)
    if confirmed:
        print(term.green(f"\nAdded {len(confirmed)} replacement(s)."))


def _run_playlist_action(
    sp: spotipy.Spotify, term: Terminal, market: str, dry_run: bool
) -> None:
    selection = select_playlist(sp, term)
    if selection is not None:
        playlist_id, name = selection
        repair_playlist_by_id(sp, term, playlist_id, name, market, dry_run)


# --- entry ------------------------------------------------------------------


def main() -> None:
    logging.getLogger("spotipy").setLevel(logging.ERROR)
    sp = login()
    term = Terminal()
    market = get_user_market(sp)
    options = [
        ("preview_liked", "Preview liked songs (dry run)"),
        ("preview_playlist", "Preview playlist (dry run)"),
        ("repair_liked", "Repair liked songs"),
        ("repair_playlist", "Repair playlist"),
        ("exit", "Exit"),
    ]
    while True:
        header = [
            term.black_on_darkkhaki(term.center("spotify-repairer")),
            f"Market: {market}",
        ]
        choice = select_from_menu(term, header, options)
        if choice is None or choice == "exit":
            return
        if choice == "preview_liked":
            repair_liked_songs(sp, term, market, dry_run=True)
        elif choice == "preview_playlist":
            _run_playlist_action(sp, term, market, dry_run=True)
        elif choice == "repair_liked":
            repair_liked_songs(sp, term, market, dry_run=False)
        elif choice == "repair_playlist":
            _run_playlist_action(sp, term, market, dry_run=False)
        print(term.move_down(1) + "Press any key for menu...", end="", flush=True)
        with term.cbreak():
            term.inkey()


if __name__ == "__main__":
    main()
