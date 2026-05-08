from dataclasses import replace
from typing import Any

from spotify_repairer.repair import (
    DURATION_TOLERANCE_MS,
    Confidence,
    Match,
    Track,
    find_replacement,
    parse_track,
)


class FakeClient:
    """Fake Spotify search that returns canned results keyed by query substring."""

    def __init__(self, by_query: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._by_query = by_query or {}
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        q: str,
        limit: int = 10,
        offset: int = 0,
        type: str = "track",
        market: str | None = None,
    ) -> dict[str, Any] | None:
        self.calls.append({"q": q, "type": type, "market": market, "limit": limit})
        for substring, items in self._by_query.items():
            if substring in q:
                return {"tracks": {"items": items}}
        return {"tracks": {"items": []}}


def _result(
    track_id: str,
    name: str = "Song",
    artists: list[str] | None = None,
    album: str | None = "Album",
    is_playable: bool = True,
    isrc: str | None = None,
    duration_ms: int | None = 200_000,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": track_id,
        "name": name,
        "artists": [{"name": a} for a in (artists or ["Artist"])],
        "is_playable": is_playable,
        "duration_ms": duration_ms,
    }
    if album is not None:
        item["album"] = {"name": album}
    if isrc is not None:
        item["external_ids"] = {"isrc": isrc}
    return item


_DEFAULT_TRACK = Track(
    id="orig",
    name="Song",
    artists=("Artist",),
    is_playable=False,
    album="Album",
    isrc=None,
    duration_ms=200_000,
)


def _track(**overrides: Any) -> Track:
    return replace(_DEFAULT_TRACK, **overrides)


def test_isrc_match_returns_exact_confidence() -> None:
    track = _track(isrc="USRC12345678")
    client = FakeClient({"isrc:USRC12345678": [_result("new", isrc="USRC12345678")]})

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.confidence == Confidence.EXACT
    assert match.track.id == "new"


def test_isrc_search_skips_same_id() -> None:
    track = _track(id="same", isrc="USRC12345678")
    client = FakeClient(
        {
            "isrc:USRC12345678": [_result("same", isrc="USRC12345678")],
            'track:"Song"': [],
        }
    )

    assert find_replacement(client, track, market="US") is None


def test_isrc_search_skips_unplayable() -> None:
    track = _track(isrc="USRC12345678")
    client = FakeClient(
        {
            "isrc:USRC12345678": [
                _result("new", isrc="USRC12345678", is_playable=False)
            ],
            'track:"Song"': [],
        }
    )

    assert find_replacement(client, track, market="US") is None


def test_isrc_match_rejects_when_artist_differs() -> None:
    """Spotify occasionally shares ISRCs across covers/re-records. Even with
    a matching ISRC, an unrelated artist means it's not actually the same
    recording — fall through to the name+artist search instead."""
    track = _track(isrc="AUUM71101248", artists=("Matt Corby",), name="Brother")
    client = FakeClient(
        {
            "isrc:AUUM71101248": [
                _result("cover", artists=["İncici"], isrc="AUUM71101248")
            ],
            'track:"Brother"': [],
        }
    )

    assert find_replacement(client, track, market="US") is None


def test_isrc_match_rejects_when_title_substantively_differs() -> None:
    track = _track(isrc="ISRC1", name="Brother", artists=("Matt Corby",))
    client = FakeClient(
        {
            "isrc:ISRC1": [
                _result("wrong", name="Sister", artists=["Matt Corby"], isrc="ISRC1")
            ],
            'track:"Brother"': [],
        }
    )

    assert find_replacement(client, track, market="US") is None


def test_isrc_match_accepts_when_title_has_remaster_suffix() -> None:
    """'Brother' → 'Brother (Remastered)' is a legitimate same-recording variant."""
    track = _track(isrc="ISRC1", name="Brother", artists=("Matt Corby",))
    client = FakeClient(
        {
            "isrc:ISRC1": [
                _result(
                    "remaster",
                    name="Brother (Remastered)",
                    artists=["Matt Corby"],
                    isrc="ISRC1",
                )
            ],
        }
    )

    match = find_replacement(client, track, market="US")
    assert match is not None
    assert match.confidence == Confidence.EXACT
    assert match.track.id == "remaster"


def test_isrc_match_accepts_when_artist_credits_added() -> None:
    """'Adele' → 'Adele, Beyoncé' (added feature) is legitimate."""
    track = _track(isrc="ISRC1", name="Hello", artists=("Adele",))
    client = FakeClient(
        {
            "isrc:ISRC1": [
                _result(
                    "feat",
                    name="Hello",
                    artists=["Adele, Beyoncé"],
                    isrc="ISRC1",
                )
            ],
        }
    )

    match = find_replacement(client, track, market="US")
    assert match is not None
    assert match.confidence == Confidence.EXACT


def test_isrc_match_accepts_when_title_only_differs_in_case() -> None:
    track = _track(isrc="ISRC1", name="Brother", artists=("Matt Corby",))
    client = FakeClient(
        {
            "isrc:ISRC1": [
                _result(
                    "remaster",
                    name="brother",
                    artists=["matt corby"],
                    isrc="ISRC1",
                )
            ],
        }
    )

    match = find_replacement(client, track, market="US")
    assert match is not None
    assert match.confidence == Confidence.EXACT
    assert match.track.id == "remaster"


def test_isrc_match_accepts_when_original_primary_artist_appears_among_credits() -> None:
    """Re-uploads sometimes add featured-artist credits not on the original."""
    track = _track(isrc="ISRC1", name="Brother", artists=("Matt Corby",))
    client = FakeClient(
        {
            "isrc:ISRC1": [
                _result(
                    "rerelease",
                    name="Brother",
                    artists=["Matt Corby", "Featured"],
                    isrc="ISRC1",
                )
            ],
        }
    )

    match = find_replacement(client, track, market="US")
    assert match is not None
    assert match.confidence == Confidence.EXACT


def test_isrc_match_takes_priority_over_name_artist() -> None:
    track = _track(isrc="USRC12345678")
    client = FakeClient(
        {
            "isrc:USRC12345678": [_result("isrc-hit", isrc="USRC12345678")],
            'track:"Song"': [_result("name-artist-hit")],
        }
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.track.id == "isrc-hit"
    assert match.confidence == Confidence.EXACT


def test_falls_back_to_name_artist_when_no_isrc_hit() -> None:
    track = _track(isrc="USRC12345678")
    client = FakeClient(
        {
            "isrc:USRC12345678": [],
            'track:"Song"': [_result("new", duration_ms=200_000)],
        }
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.track.id == "new"
    assert match.confidence == Confidence.HIGH


def test_name_artist_match_within_duration_tolerance_is_high() -> None:
    track = _track(duration_ms=200_000)
    delta = DURATION_TOLERANCE_MS
    client = FakeClient(
        {'track:"Song"': [_result("new", duration_ms=200_000 + delta)]}
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.confidence == Confidence.HIGH


def test_name_artist_match_outside_duration_tolerance_is_low() -> None:
    track = _track(duration_ms=200_000)
    client = FakeClient(
        {
            'track:"Song"': [
                _result("new", duration_ms=200_000 + DURATION_TOLERANCE_MS + 1)
            ]
        }
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.confidence == Confidence.LOW


def test_picks_closest_duration_among_candidates() -> None:
    track = _track(duration_ms=200_000)
    client = FakeClient(
        {
            'track:"Song"': [
                _result("far", duration_ms=300_000),
                _result("close", duration_ms=200_500),
                _result("medium", duration_ms=210_000),
            ]
        }
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.track.id == "close"
    assert match.confidence == Confidence.HIGH


def test_requires_artist_match() -> None:
    track = _track()
    client = FakeClient({'track:"Song"': [_result("new", artists=["Different"])]})

    assert find_replacement(client, track, market="US") is None


def test_requires_name_match() -> None:
    track = _track()
    client = FakeClient({'track:"Song"': [_result("new", name="OtherSong")]})

    assert find_replacement(client, track, market="US") is None


def test_artist_match_is_case_insensitive_across_credits() -> None:
    track = _track(artists=("ARTIST",))
    client = FakeClient(
        {'track:"Song"': [_result("new", artists=["feature", "artist"])]}
    )

    match = find_replacement(client, track, market="US")

    assert match is not None
    assert match.track.id == "new"


def test_returns_none_when_no_match() -> None:
    track = _track()
    client = FakeClient({'track:"Song"': []})

    assert find_replacement(client, track, market="US") is None


def test_search_query_uses_primary_artist() -> None:
    track = _track(artists=("Primary", "Featured"))
    client = FakeClient()

    find_replacement(client, track, market="US")

    isrc_calls = [c for c in client.calls if "isrc" in c["q"]]
    name_calls = [c for c in client.calls if "track:" in c["q"]]
    assert isrc_calls == []  # no ISRC on the original
    assert name_calls and 'artist:"Primary"' in name_calls[0]["q"]


def test_parse_track_extracts_all_fields() -> None:
    item = {
        "track": {
            "id": "abc",
            "name": "Hello",
            "artists": [{"name": "Adele"}, {"name": "Tobias Jesso Jr."}],
            "is_playable": True,
            "album": {"name": "25"},
            "external_ids": {"isrc": "GBBKS1500214"},
            "duration_ms": 295_502,
        }
    }

    assert parse_track(item) == Track(
        id="abc",
        name="Hello",
        artists=("Adele", "Tobias Jesso Jr."),
        is_playable=True,
        album="25",
        isrc="GBBKS1500214",
        duration_ms=295_502,
    )


def test_parse_track_handles_missing_optional_fields() -> None:
    item = {
        "track": {
            "id": "abc",
            "name": "Hello",
            "artists": [{"name": "Adele"}],
        }
    }

    track = parse_track(item)

    assert track is not None
    assert track.album is None
    assert track.isrc is None
    assert track.duration_ms is None
    assert track.is_playable is True
    assert track.artists == ("Adele",)


def test_parse_track_returns_none_for_episode() -> None:
    assert parse_track({"track": None}) is None


def test_parse_track_returns_none_for_missing_id() -> None:
    assert parse_track({"track": {"name": "x", "artists": [{"name": "y"}]}}) is None


def test_parse_track_returns_none_for_missing_artists() -> None:
    assert parse_track({"track": {"id": "x", "name": "y", "artists": []}}) is None


def test_track_primary_artist_property() -> None:
    track = _track(artists=("Primary", "Featured"))
    assert track.primary_artist == "Primary"


def test_confidence_ordering() -> None:
    assert Confidence.LOW < Confidence.HIGH < Confidence.EXACT
    assert Confidence.EXACT > Confidence.HIGH > Confidence.LOW
    assert Confidence.HIGH >= Confidence.HIGH
    assert Confidence.HIGH <= Confidence.HIGH


def test_confidence_threshold_filtering() -> None:
    threshold = Confidence.HIGH
    accepted = [c for c in Confidence if c >= threshold]
    assert accepted == [Confidence.HIGH, Confidence.EXACT]


def test_confidence_value_preserved_for_serialization() -> None:
    assert Confidence.EXACT.value == "exact"
    assert Confidence.HIGH.value == "high"
    assert Confidence.LOW.value == "low"


def test_match_dataclass_is_frozen() -> None:
    match = Match(track=_track(id="x"), confidence=Confidence.EXACT)
    try:
        match.confidence = Confidence.LOW  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Match should be frozen")
