"""Deterministic area decisions for Dahua alarm inputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .area_matcher import DEFAULT_THRESHOLD, AreaCandidate, match_zone_with_hint


def decide_zone_areas(
    items: Iterable[Mapping[str, Any]],
    areas: Iterable[AreaCandidate],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    previous: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Persist one match per immutable Alarm[] index, including non-matches.

    Existing decisions are never rematched merely because an area is renamed.
    A new zone can be added without moving any existing zone.
    """
    choices = list(areas)
    decisions = {str(k): dict(v) for k, v in (previous or {}).items()}
    for item in items:
        try:
            index = str(int(item["index"]))
        except KeyError, TypeError, ValueError:
            continue
        if index in decisions:
            continue
        name = str(item.get("name") or "")
        hint = str(item.get("area_hint") or "") or None
        match = match_zone_with_hint(name, hint, choices, threshold=threshold)
        decisions[index] = {
            "area_id": match.area_id if match else None,
            "score": match.score if match else 0,
            "reason": match.reason if match else "ambiguous or below threshold",
            "zone_name": name,
        }
    return decisions


def may_auto_assign(
    current_area_id: str | None,
    target_area_id: str | None,
    owned_area_id: str | None,
    *,
    was_automatically_assigned: bool = False,
) -> bool:
    """Only set a blank area or an area we can prove we last set."""
    if not target_area_id:
        return False
    if was_automatically_assigned:
        return current_area_id == owned_area_id
    return current_area_id is None
