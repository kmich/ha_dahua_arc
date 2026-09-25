"""Area decision tests without needing a running Home Assistant instance."""

from __future__ import annotations

from custom_components.dahua_arc.area_assignment import (
    decide_zone_areas,
    may_auto_assign,
)
from custom_components.dahua_arc.area_matcher import AreaCandidate

AREAS = [
    AreaCandidate("kitchen", "Kitchen", ("Kouzina",)),
    AreaCandidate("living", "Living Room", ("Saloni",)),
    AreaCandidate("office", "Office"),
]


def test_exact_normalization_alias_and_sensor_words() -> None:
    decisions = decide_zone_areas(
        [
            {"index": 7, "name": "Kitchen Window Contact"},
            {"index": 8, "name": "Saloni PIR Sensor"},
        ],
        AREAS,
    )
    assert decisions["7"]["area_id"] == "kitchen"
    assert decisions["7"]["score"] >= 90
    assert decisions["8"]["area_id"] == "living"


def test_ambiguous_or_low_confidence_remains_unassigned() -> None:
    decisions = decide_zone_areas([{"index": 2, "name": "Sensor"}], AREAS)
    assert decisions["2"]["area_id"] is None
    assert decisions["2"]["reason"]


def test_persisted_decision_is_not_rematched_and_new_zone_is_added() -> None:
    old = decide_zone_areas([{"index": 1, "name": "Kitchen Door"}], AREAS)
    newer = decide_zone_areas(
        [
            {"index": 1, "name": "Office Door"},
            {"index": 2, "name": "Office PIR"},
        ],
        AREAS,
        previous=old,
    )
    assert newer["1"] == old["1"]
    assert newer["2"]["area_id"] == "office"


def test_manual_area_change_and_clear_are_protected() -> None:
    assert may_auto_assign(None, "kitchen", None)
    assert not may_auto_assign("living", "kitchen", None)
    assert may_auto_assign(
        "kitchen", "office", "kitchen", was_automatically_assigned=True
    )
    assert not may_auto_assign(
        "living", "office", "kitchen", was_automatically_assigned=True
    )
    assert not may_auto_assign(
        None, "office", "kitchen", was_automatically_assigned=True
    )


def test_possessive_label_matches_singular_area_without_hard_coded_names() -> None:
    areas = [AreaCandidate("anna", "Anna Office"), AreaCandidate("kitchen", "Kitchen")]
    decisions = decide_zone_areas(
        [
            {"index": 1, "name": "Annas Window"},
            {"index": 2, "name": "Anna's Door"},
            {"index": 3, "name": "Glass Break"},
        ],
        areas,
    )
    assert decisions["1"]["area_id"] == "anna"
    assert decisions["2"]["area_id"] == "anna"
    assert decisions["3"]["area_id"] is None
