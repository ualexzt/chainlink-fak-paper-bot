from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class OfficialSettlement:
    winner: str
    resolved_at: int | None = None

    def __post_init__(self) -> None:
        if self.winner not in {"UP", "DOWN"}:
            raise ValueError("winner must be UP or DOWN")
        if (
            self.resolved_at is not None
            and (
                isinstance(self.resolved_at, bool)
                or not isinstance(self.resolved_at, int)
                or self.resolved_at < 0
            )
        ):
            raise ValueError("resolved_at must be a non-negative integer or None")


def _parse_json_string_pair(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list) or len(parsed) != 2 or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed[0], parsed[1]


def _parse_resolved_at(payload: Mapping[str, Any]) -> int | None | object:
    missing = object()
    raw = payload.get("resolvedAt", payload.get("closedTime", missing))
    if raw is missing or raw is None:
        return None
    if isinstance(raw, bool):
        return missing
    if isinstance(raw, int):
        return raw if raw >= 0 else missing
    if not isinstance(raw, str) or not raw:
        return missing
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return missing
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return missing
    return int(parsed.astimezone(timezone.utc).timestamp())


def parse_official_settlement(
    payload: Mapping[str, Any], expected_slug: str
) -> OfficialSettlement | None:
    """Parse only a closed Gamma market with an exact unique 1/0 outcome."""
    if not isinstance(payload, Mapping) or not isinstance(expected_slug, str) or not expected_slug:
        return None
    if payload.get("slug") != expected_slug or payload.get("closed") is not True:
        return None
    # Gamma's documented Market schema does not require a `resolved` field.
    # If a payload does provide it, an explicit false value remains fail-closed.
    if "resolved" in payload and payload.get("resolved") is not True:
        return None

    outcomes = _parse_json_string_pair(payload.get("outcomes"))
    prices = _parse_json_string_pair(payload.get("outcomePrices"))
    if outcomes is None or prices is None or set(outcomes) != {"Up", "Down"}:
        return None
    try:
        values = tuple(Decimal(value) for value in prices)
    except (InvalidOperation, ValueError):
        return None
    if values not in ((Decimal("1"), Decimal("0")), (Decimal("0"), Decimal("1"))):
        return None

    resolved_at = _parse_resolved_at(payload)
    if not (resolved_at is None or isinstance(resolved_at, int)):
        return None
    winner = outcomes[values.index(Decimal("1"))].upper()
    return OfficialSettlement(winner=winner, resolved_at=resolved_at)


__all__ = ["OfficialSettlement", "parse_official_settlement"]
