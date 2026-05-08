"""Append-only audit log of repairs applied. For manual review; not used for
idempotency (library state is the source of truth for that)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spotify_repairer.repair import Match, Track

DEFAULT_MANIFEST_PATH = Path("repairs.json")


def load(path: Path = DEFAULT_MANIFEST_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    repairs = data.get("repairs")
    return repairs if isinstance(repairs, list) else []


def record(
    source: str,
    original: Track,
    match: Match,
    position: int | None = None,
    path: Path = DEFAULT_MANIFEST_PATH,
) -> None:
    repairs = load(path)
    repairs.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "position": position,
            "confidence": match.confidence.value,
            "original": asdict(original),
            "replacement": asdict(match.track),
        }
    )
    path.write_text(json.dumps({"repairs": repairs}, indent=2))
