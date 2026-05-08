import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from spotify_repairer import manifest
from spotify_repairer.repair import Confidence, Match, Track


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "repairs.json"


_DEFAULT_TRACK = Track(
    id="orig",
    name="Song",
    artists=("Artist",),
    is_playable=False,
    album="Album",
    isrc="USRC12345678",
    duration_ms=200_000,
)


def _track(**overrides: Any) -> Track:
    return replace(_DEFAULT_TRACK, **overrides)


def _match(
    track_id: str = "new", confidence: Confidence = Confidence.EXACT
) -> Match:
    return Match(
        track=Track(
            id=track_id,
            name="Song",
            artists=("Artist",),
            is_playable=True,
            album="Album",
            isrc="USRC12345678",
            duration_ms=200_000,
        ),
        confidence=confidence,
    )


def test_load_returns_empty_when_missing(manifest_path: Path) -> None:
    assert manifest.load(manifest_path) == []


def test_load_returns_empty_for_corrupt_file(manifest_path: Path) -> None:
    manifest_path.write_text("not json{")
    assert manifest.load(manifest_path) == []


def test_record_writes_repair(manifest_path: Path) -> None:
    manifest.record(
        "liked_songs", _track(), _match(), position=None, path=manifest_path
    )

    data = json.loads(manifest_path.read_text())
    assert len(data["repairs"]) == 1
    entry = data["repairs"][0]
    assert entry["source"] == "liked_songs"
    assert entry["confidence"] == "exact"
    assert entry["original"]["id"] == "orig"
    assert entry["replacement"]["id"] == "new"
    assert "timestamp" in entry


def test_record_appends_without_overwriting(manifest_path: Path) -> None:
    manifest.record("liked_songs", _track(id="a"), _match("a-new"), path=manifest_path)
    manifest.record(
        "playlist:p1",
        _track(id="b"),
        _match("b-new", confidence=Confidence.HIGH),
        position=5,
        path=manifest_path,
    )

    repairs = manifest.load(manifest_path)
    assert [r["original"]["id"] for r in repairs] == ["a", "b"]
    assert repairs[1]["position"] == 5
    assert repairs[1]["confidence"] == "high"


