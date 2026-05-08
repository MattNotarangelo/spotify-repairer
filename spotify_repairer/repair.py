"""Pure logic for matching unplayable tracks to playable replacements.

Match strategy, in order:
  1. ISRC search — same recording, just ingested under a different track ID.
     This is the typical "republished by a new distributor" case and is treated
     as an exact match.
  2. Name + primary-artist match, ranked by duration proximity. A duration delta
     within DURATION_TOLERANCE_MS is considered a high-confidence same-recording
     match; outside that window is flagged as low-confidence (likely a different
     master, re-master, live version, etc.).
"""

from dataclasses import dataclass
from enum import Enum
from functools import total_ordering
from typing import Any, Protocol

DURATION_TOLERANCE_MS = 2000


@total_ordering
class Confidence(Enum):
    """Ordered enum: LOW < HIGH < EXACT.

    Use comparison operators to express thresholds, e.g.
    `match.confidence >= Confidence.HIGH` to accept high or exact matches.
    """

    LOW = "low"  # same name + artist, duration differs significantly
    HIGH = "high"  # same name + artist + duration within tolerance
    EXACT = "exact"  # same ISRC — same recording

    @property
    def _rank(self) -> int:
        return {Confidence.LOW: 0, Confidence.HIGH: 1, Confidence.EXACT: 2}[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self._rank < other._rank


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artists: tuple[str, ...]
    is_playable: bool
    album: str | None = None
    isrc: str | None = None
    duration_ms: int | None = None

    @property
    def primary_artist(self) -> str:
        return self.artists[0]


@dataclass(frozen=True)
class Match:
    track: Track
    confidence: Confidence


class SearchClient(Protocol):
    """Subset of `spotipy.Spotify` that `find_replacement` depends on.

    Signature matches spotipy's so a real `Spotify` instance satisfies the
    protocol structurally.
    """

    def search(
        self,
        q: str,
        limit: int = 10,
        offset: int = 0,
        type: str = "track",
        market: str | None = None,
    ) -> dict[str, Any] | None: ...


def _track_from_api(track: dict[str, Any]) -> Track | None:
    if not track or not track.get("id"):
        return None
    artists = track.get("artists") or []
    if not artists:
        return None
    album = track.get("album") or {}
    return Track(
        id=track["id"],
        name=track["name"],
        artists=tuple(a["name"] for a in artists),
        is_playable=track.get("is_playable", True),
        album=album.get("name"),
        isrc=(track.get("external_ids") or {}).get("isrc"),
        duration_ms=track.get("duration_ms"),
    )


def parse_track(item: dict[str, Any]) -> Track | None:
    """Parse a Spotify saved-track or playlist-item entry into a Track.

    Returns None for non-track items (episodes) or malformed entries.
    """
    return _track_from_api(item.get("track") or {})


def _playable_search_results(
    client: SearchClient, query: str, market: str, limit: int
) -> list[dict[str, Any]]:
    results = client.search(q=query, type="track", market=market, limit=limit)
    items = (results or {}).get("tracks", {}).get("items") or []
    return [item for item in items if item.get("is_playable", True)]


def _names_compatible(a: str, b: str) -> bool:
    """Loose name/title equivalence: either is a substring of the other,
    case-insensitive. Tolerates additions like '(Remastered)' or feature
    credits while rejecting substantively different values."""
    af, bf = a.casefold(), b.casefold()
    return af in bf or bf in af


def _isrc_match_is_consistent(original: Track, candidate: Track) -> bool:
    """Sanity-check an ISRC hit before treating it as the same recording.

    Spotify's ISRC tagging is occasionally wrong — covers, re-records, and
    mistagged compilations sometimes share an ISRC with an unrelated track.
    Require that the title and the original's primary artist are roughly
    compatible with the candidate (allowing minor variations) before
    accepting.
    """
    if not _names_compatible(original.name, candidate.name):
        return False
    return any(
        _names_compatible(original.primary_artist, a) for a in candidate.artists
    )


def find_replacement(
    client: SearchClient, track: Track, market: str, limit: int = 10
) -> Match | None:
    """Find a playable replacement for an unplayable track.

    Returns None if no acceptable match is found.
    """
    if track.isrc:
        for item in _playable_search_results(
            client, f"isrc:{track.isrc}", market, limit
        ):
            if item["id"] == track.id:
                continue
            replacement = _track_from_api(item)
            if replacement is None:
                continue
            if not _isrc_match_is_consistent(track, replacement):
                continue
            return Match(track=replacement, confidence=Confidence.EXACT)

    primary = track.primary_artist
    query = f'track:"{track.name}" artist:"{primary}"'
    track_name_lower = track.name.lower()
    primary_lower = primary.lower()
    candidates = []
    for item in _playable_search_results(client, query, market, limit):
        if item["id"] == track.id:
            continue
        if item["name"].lower() != track_name_lower:
            continue
        item_artists = {a["name"].lower() for a in item.get("artists", [])}
        if primary_lower not in item_artists:
            continue
        candidates.append(item)

    if not candidates:
        return None

    if track.duration_ms is not None:
        target_duration = track.duration_ms
        candidates.sort(
            key=lambda c: abs((c.get("duration_ms") or 0) - target_duration)
        )

    best = candidates[0]
    replacement = _track_from_api(best)
    if replacement is None:
        return None

    confidence = Confidence.LOW
    if track.duration_ms is not None and best.get("duration_ms") is not None:
        if abs(best["duration_ms"] - track.duration_ms) <= DURATION_TOLERANCE_MS:
            confidence = Confidence.HIGH

    return Match(track=replacement, confidence=confidence)
