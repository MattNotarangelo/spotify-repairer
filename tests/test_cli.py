"""Tests for cli orchestration logic.

Focus on the parts most likely to break: classification of search results into
review/skip buckets, threshold filtering, ISRC pre-coverage detection, and
reverse-position-order playlist insertion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from blessed import Terminal

from spotify_repairer import cli
from spotify_repairer.repair import Confidence, Match, Track


# --- helpers ----------------------------------------------------------------


def _track(
    track_id: str,
    *,
    name: str = "Song",
    artists: tuple[str, ...] = ("Artist",),
    is_playable: bool = True,
    album: str | None = "Album",
    isrc: str | None = None,
    duration_ms: int | None = 200_000,
) -> Track:
    return Track(
        id=track_id,
        name=name,
        artists=artists,
        is_playable=is_playable,
        album=album,
        isrc=isrc,
        duration_ms=duration_ms,
    )


def _saved_track_item(track: Track) -> dict[str, Any]:
    """Build a Spotify saved-track / playlist-item shape from a Track."""
    return {
        "track": {
            "id": track.id,
            "name": track.name,
            "artists": [{"name": a} for a in track.artists],
            "is_playable": track.is_playable,
            "album": {"name": track.album} if track.album else {},
            "external_ids": {"isrc": track.isrc} if track.isrc else {},
            "duration_ms": track.duration_ms,
        }
    }


def _paged_fetcher(
    pages: list[list[dict[str, Any]]],
) -> Any:
    """Build a callable mimicking spotipy's page-fetch shape, returning given pages."""

    def fetcher(
        limit: int = 50, offset: int = 0, market: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        idx = offset // cli.BATCH_SIZE
        if idx >= len(pages):
            return {"items": []}
        return {"items": pages[idx]}

    return fetcher


@pytest.fixture
def term() -> Terminal:
    return Terminal()


# --- _classify --------------------------------------------------------------


def test_classify_buckets_each_skip_reason(term: Terminal) -> None:
    a = _track("a", is_playable=False)
    b = _track("b", is_playable=False)
    c = _track("c", is_playable=False)
    d = _track("d", is_playable=False)
    needs_repair = [(0, a), (1, b), (2, c), (3, d)]
    matches: list[Match | None] = [
        None,  # a → no_replacement
        Match(track=_track("b-rep"), confidence=Confidence.EXACT),  # b → already_added
        Match(track=_track("c-rep"), confidence=Confidence.LOW),  # c → below_threshold
        Match(track=_track("d-rep"), confidence=Confidence.HIGH),  # d → reviewable
    ]
    track_ids_in_source = {"a", "b", "c", "d", "b-rep"}

    to_review, counts = cli._classify(needs_repair, matches, track_ids_in_source)

    assert [t.id for _, t, _ in to_review] == ["d"]
    assert counts == {
        "no_replacement": 1,
        "already_added": 1,
        "below_threshold": 1,
    }


def test_classify_threshold_uses_min_confidence_constant() -> None:
    # Sanity: HIGH passes, LOW fails, given default MIN_CONFIDENCE = HIGH.
    assert cli.MIN_CONFIDENCE == Confidence.HIGH
    assert Confidence.HIGH >= cli.MIN_CONFIDENCE
    assert Confidence.LOW < cli.MIN_CONFIDENCE
    assert Confidence.EXACT >= cli.MIN_CONFIDENCE


# --- _collect_repairs (dry-run paths to avoid prompts) ----------------------


def test_collect_repairs_no_unplayable(term: Terminal) -> None:
    sp = MagicMock()
    fetch_page = _paged_fetcher([[_saved_track_item(_track("a"))]])

    confirmed = cli._collect_repairs(
        sp, term, "US", fetch_page, {}, dry_run=True
    )

    assert confirmed == []


def test_collect_repairs_skips_unplayable_already_covered_by_isrc(
    term: Terminal,
) -> None:
    """If a playable track in the source has the same ISRC, no repair needed."""
    playable = _track("a", isrc="ISRC1", is_playable=True)
    unplayable = _track("b", isrc="ISRC1", is_playable=False)
    fetch_page = _paged_fetcher(
        [[_saved_track_item(playable), _saved_track_item(unplayable)]]
    )
    sp = MagicMock()
    sp.search = MagicMock()  # should not be called

    confirmed = cli._collect_repairs(
        sp, term, "US", fetch_page, {}, dry_run=True
    )

    assert confirmed == []
    sp.search.assert_not_called()


def test_collect_repairs_dry_run_returns_empty_even_with_matches(
    term: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    unplayable = _track("a", is_playable=False, isrc=None)
    fetch_page = _paged_fetcher([[_saved_track_item(unplayable)]])
    sp = MagicMock()
    monkeypatch.setattr(
        cli,
        "_find_replacements_parallel",
        lambda _sp, tracks, _market: [
            Match(track=_track("a-rep"), confidence=Confidence.EXACT)
            for _ in tracks
        ],
    )

    confirmed = cli._collect_repairs(
        sp, term, "US", fetch_page, {}, dry_run=True
    )

    assert confirmed == []


# --- repair_playlist_by_id reverse-order application -----------------------


def test_repair_playlist_applies_in_reverse_position_order(
    term: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inserting at low positions first would shift the captured positions of
    later items. Applying highest-position-first preserves them."""
    sp = MagicMock()
    sp.playlist_items = _paged_fetcher([[]])  # not exercised in this test
    monkeypatch.setattr("spotify_repairer.manifest.record", lambda *args, **kwargs: None)

    # Stub _collect_repairs so we don't need to drive the real scan.
    confirmed = [
        (0, _track("a", is_playable=False), Match(_track("a-rep"), Confidence.EXACT)),
        (5, _track("b", is_playable=False), Match(_track("b-rep"), Confidence.EXACT)),
        (10, _track("c", is_playable=False), Match(_track("c-rep"), Confidence.EXACT)),
    ]
    monkeypatch.setattr(cli, "_collect_repairs", lambda *args, **kwargs: confirmed)

    cli.repair_playlist_by_id(sp, term, "playlist123", "Test", "US", dry_run=False)

    # Inserts should fire in descending position order: 10, 5, 0 → +1 each.
    positions_called = [
        call.kwargs["position"] for call in sp.playlist_add_items.call_args_list
    ]
    assert positions_called == [11, 6, 1]


def test_repair_playlist_skips_apply_when_dry_run(
    term: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = MagicMock()
    monkeypatch.setattr(cli, "_collect_repairs", lambda *args, **kwargs: [])
    cli.repair_playlist_by_id(sp, term, "p", "Test", "US", dry_run=True)
    sp.playlist_add_items.assert_not_called()


# --- repair_liked_songs -----------------------------------------------------


def test_repair_liked_songs_adds_each_confirmed_replacement(
    term: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = MagicMock()
    monkeypatch.setattr("spotify_repairer.manifest.record", lambda *args, **kwargs: None)
    confirmed = [
        (0, _track("a", is_playable=False), Match(_track("a-rep"), Confidence.EXACT)),
        (1, _track("b", is_playable=False), Match(_track("b-rep"), Confidence.HIGH)),
    ]
    monkeypatch.setattr(cli, "_collect_repairs", lambda *args, **kwargs: confirmed)

    cli.repair_liked_songs(sp, term, "US", dry_run=False)

    add_calls = sp.current_user_saved_tracks_add.call_args_list
    assert len(add_calls) == 2
    assert [c.args[0] for c in add_calls] == [["a-rep"], ["b-rep"]]


# --- pagination -------------------------------------------------------------


def test_iterate_tracks_walks_multiple_pages(term: Terminal) -> None:
    page1 = [_saved_track_item(_track(f"t{i}")) for i in range(cli.BATCH_SIZE)]
    page2 = [_saved_track_item(_track("last"))]
    fetch_page = _paged_fetcher([page1, page2])

    results = list(cli.iterate_tracks(fetch_page, {}, "US"))

    assert len(results) == cli.BATCH_SIZE + 1
    assert results[0][0] == 0
    assert results[-1][0] == cli.BATCH_SIZE
    assert results[-1][1].id == "last"


def test_iterate_tracks_skips_episodes() -> None:
    items = [_saved_track_item(_track("a")), {"track": None}, _saved_track_item(_track("b"))]
    fetch_page = _paged_fetcher([items])

    results = list(cli.iterate_tracks(fetch_page, {}, "US"))

    assert [t.id for _, t in results] == ["a", "b"]


# --- playlist_source --------------------------------------------------------


def test_playlist_source_format() -> None:
    assert cli.playlist_source("abc123") == "playlist:abc123"


# --- diff display -----------------------------------------------------------


def test_diff_track_lines_marks_identical_fields_as_no_change() -> None:
    original = _track("a", isrc="ISRC1", duration_ms=200_000)
    replacement = _track("b", isrc="ISRC1", duration_ms=200_000)

    lines = cli._diff_track_lines(original, replacement)

    assert all("(no change)" in line.text for line in lines)
    assert all(line.changed is False for line in lines)


def test_diff_track_lines_shows_arrow_and_changed_flag_when_field_differs() -> None:
    original = _track("a", album="25", duration_ms=200_000, isrc="ISRC1")
    replacement = _track(
        "b", album="25 (Remastered)", duration_ms=200_000, isrc="ISRC1"
    )

    lines = cli._diff_track_lines(original, replacement)

    by_label = {
        "Artists:": next(l for l in lines if "Artists:" in l.text),
        "Title:": next(l for l in lines if "Title:" in l.text),
        "Album:": next(l for l in lines if "Album:" in l.text),
        "Length:": next(l for l in lines if "Length:" in l.text),
        "ISRC:": next(l for l in lines if "ISRC:" in l.text),
    }

    assert by_label["Artists:"].changed is False
    assert "(no change)" in by_label["Artists:"].text
    assert by_label["Title:"].changed is False
    assert by_label["Album:"].changed is True
    assert "25 → 25 (Remastered)" in by_label["Album:"].text
    assert by_label["Length:"].changed is False
    assert by_label["ISRC:"].changed is False


def test_diff_track_lines_treats_case_only_differences_as_no_change() -> None:
    """Spotify ingests of the same recording sometimes differ only in
    capitalization — those should not surface as a diff."""
    original = _track(
        "a",
        name="Time for Us",
        artists=("composer",),
        album="soundtrack",
        isrc="ISRC1",
    )
    replacement = _track(
        "b",
        name="Time For Us",
        artists=("Composer",),
        album="Soundtrack",
        isrc="isrc1",
    )

    lines = cli._diff_track_lines(original, replacement)

    assert all(line.changed is False for line in lines)
    assert all("(no change)" in line.text for line in lines)


def test_diff_track_lines_omits_isrc_row_when_replacement_has_no_isrc() -> None:
    original = _track("a", isrc="ISRC1")
    replacement = _track("b", isrc=None)

    lines = cli._diff_track_lines(original, replacement)

    assert not any("ISRC:" in line.text for line in lines)


# --- retry behaviour --------------------------------------------------------


def test_find_replacement_with_retry_succeeds_after_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sp = MagicMock()
    track = _track("a")
    expected = Match(track=_track("a-rep"), confidence=Confidence.EXACT)
    calls = {"n": 0}

    def fake_find(sp_arg: Any, track_arg: Track, market_arg: str) -> Match | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("simulated reset")
        return expected

    monkeypatch.setattr(cli, "find_replacement", fake_find)
    monkeypatch.setattr("spotify_repairer.cli.time.sleep", lambda _s: None)

    assert cli._find_replacement_with_retry(sp, track, "US") == expected
    assert calls["n"] == 2


def test_find_replacement_with_retry_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sp = MagicMock()

    def always_fails(*_args: Any, **_kwargs: Any) -> Match | None:
        raise requests.ConnectionError("simulated reset")

    monkeypatch.setattr(cli, "find_replacement", always_fails)
    monkeypatch.setattr("spotify_repairer.cli.time.sleep", lambda _s: None)

    with pytest.raises(requests.ConnectionError):
        cli._find_replacement_with_retry(sp, _track("a"), "US")


def test_find_replacements_parallel_isolates_persistent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One track that fails after all retries should not kill the others."""
    sp = MagicMock()
    good = _track("good")
    bad = _track("bad")
    expected = Match(track=_track("good-rep"), confidence=Confidence.EXACT)

    def fake_retry(sp_arg: Any, track_arg: Track, market_arg: str) -> Match | None:
        if track_arg.id == "bad":
            raise requests.ConnectionError("persistent")
        return expected

    monkeypatch.setattr(cli, "_find_replacement_with_retry", fake_retry)

    results = cli._find_replacements_parallel(sp, [good, bad], "US")

    assert results[0] == expected
    assert results[1] is None  # bad track gracefully became None


# --- get_user_market handles None -------------------------------------------


def test_get_user_market_falls_back_when_current_user_returns_none() -> None:
    sp = MagicMock()
    sp.current_user.return_value = None
    assert cli.get_user_market(sp) == "US"


def test_get_user_market_falls_back_when_country_missing() -> None:
    sp = MagicMock()
    sp.current_user.return_value = {"display_name": "x"}
    assert cli.get_user_market(sp) == "US"


def test_get_user_market_returns_country() -> None:
    sp = MagicMock()
    sp.current_user.return_value = {"country": "AU"}
    assert cli.get_user_market(sp) == "AU"
