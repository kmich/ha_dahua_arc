"""Area arm-state tracking from ARC arm/disarm events.

Event shapes follow an ARC3800H capture of a refused Home arm, the forced
Home arm that followed, and a disarm.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from custom_components.dahua_arc.protocol.arming import (
    ARMED_AWAY,
    ARMED_HOME,
    DISARMED,
    MIXED,
    ArmingTracker,
    parse_abnormal_zones,
)
from custom_components.dahua_arc.protocol.engine import StateEngine
from custom_components.dahua_arc.protocol.inventory import extract_arm_areas

AREAS = {0: "Living Room", 1: "Garage", 2: "Office"}

# Abnormal detail uses the one-based AreaId; event Index is zero-based.
ABNORMAL = {
    "detail": [
        {
            "Area": 3,
            "AreaName": "Office ",
            "ZoneAbnormal": [
                {
                    "AbnormalType": 55,
                    "Channel": 12,
                    "Index": 191,
                    "Model": "ARM9A4-W2",
                    "Name": "Office Window",
                    "Reason": "Open",
                }
            ],
        }
    ]
}


def _event(
    code: str,
    index: int,
    mode: str,
    *,
    profile: str = "Auto",
    parent: str | None = None,
    is_global: bool = True,
    abnormal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "EventOptions": {"EventType": "ArmOrDisarm"},
        "IsGlobal": is_global,
        "Mode": mode,
        "Name": "E1827",
        "Profile": profile,
        "TriggerMode": "Remote",
    }
    if index >= 0:
        data["AreaInfo"] = [{"Index": index, "Name": AREAS.get(index, "?")}]
    if parent:
        data["ParentEvent"] = parent
    if abnormal:
        data["Abnormal"] = abnormal
    return {"Action": "Pulse", "Code": code, "Data": data, "Index": index}


def _global_burst(mode: str, *, failure: bool = False) -> list[dict[str, Any]]:
    """A global arm/disarm: summary first, then one event per area.

    The first area event omits ParentEvent, as the ARC does.
    """
    area_code = "ArmingFailure" if failure else "AreaArmModeChange"
    global_code = "GlobalArmingFailure" if failure else "GlobalAreaArmModeChange"
    open_zones = mode != "D"
    events = [
        _event(
            global_code,
            -1,
            mode,
            profile="Auto" if failure or not open_zones else "Force",
            abnormal=ABNORMAL if open_zones else None,
        )
    ]
    for index in AREAS:
        forced = open_zones and index == 2
        events.append(
            _event(
                area_code,
                index,
                mode,
                profile="Force" if forced and not failure else "Auto",
                parent=global_code if index else None,
                abnormal=ABNORMAL if forced else None,
            )
        )
    return events


def _tracker(**kwargs) -> tuple[ArmingTracker, list[None]]:
    notified: list[None] = []
    kwargs.setdefault("quiet_seconds", 0)
    tracker = ArmingTracker(AREAS, lambda: notified.append(None), **kwargs)
    return tracker, notified


def test_state_unknown_until_first_event() -> None:
    tracker, _ = _tracker()
    assert tracker.system_state() is None
    assert all(area.state is None for area in tracker.areas.values())
    assert tracker.areas[0].area_id == 1


def test_refused_then_forced_home_arm_then_disarm() -> None:
    tracker, notified = _tracker()

    for event in _global_burst("p1", failure=True):
        tracker.apply_event(event)
    # A refused arm leaves the state untouched but records why.
    assert tracker.system_state() is None
    failure = tracker.last_failure
    assert failure is not None
    assert failure.state == ARMED_HOME
    assert failure.trigger_mode == "Remote"
    assert failure.open_zones == [
        {
            "area_id": 3,
            "area": "Office",
            "zone_index": 191,
            "zone": "Office Window",
            "reason": "Open",
        }
    ]
    assert tracker.areas[2].last_failure is not None

    for event in _global_burst("p1"):
        tracker.apply_event(event)
    assert tracker.system_state() == ARMED_HOME
    assert tracker.armed_areas() == ["Living Room", "Garage", "Office"]
    office = tracker.areas[2]
    assert office.profile == "Force"
    assert [zone["zone"] for zone in office.bypassed_zones] == ["Office Window"]
    assert tracker.areas[0].profile == "Auto"
    assert tracker.areas[0].bypassed_zones == []
    # Area-level failures from a global burst do not replace the global one.
    assert tracker.last_failure is failure
    assert tracker.last_global_change["state"] == ARMED_HOME

    for event in _global_burst("D"):
        tracker.apply_event(event)
    assert tracker.system_state() == DISARMED
    assert tracker.armed_areas() == []
    assert office.bypassed_zones == []
    assert notified  # without debouncing every change notifies


def test_system_state_is_mixed_when_areas_differ() -> None:
    tracker, _ = _tracker()
    tracker.apply_event(_event("AreaArmModeChange", 0, "T", is_global=False))
    tracker.apply_event(_event("AreaArmModeChange", 1, "D", is_global=False))
    assert tracker.system_state() is None  # area 2 is still unknown
    tracker.apply_event(_event("AreaArmModeChange", 2, "D", is_global=False))
    assert tracker.system_state() == MIXED
    assert tracker.areas[0].state == ARMED_AWAY
    assert tracker.armed_areas() == ["Living Room"]


def test_single_area_failure_is_the_last_failure() -> None:
    tracker, _ = _tracker()
    tracker.apply_event(
        _event("ArmingFailure", 2, "T", is_global=False, abnormal=ABNORMAL)
    )
    assert tracker.last_failure is tracker.areas[2].last_failure
    assert tracker.last_failure.state == ARMED_AWAY


def test_unknown_mode_and_area_are_recorded_not_guessed() -> None:
    tracker, _ = _tracker()
    tracker.apply_event(_event("AreaArmModeChange", 0, "p9"))
    tracker.apply_event(_event("AreaArmModeChange", 7, "D"))
    assert tracker.areas[0].raw_mode == "p9"
    assert tracker.areas[0].state is None
    diagnostics = tracker.diagnostics()
    assert diagnostics["unknown_modes"] == ["p9"]
    assert diagnostics["unknown_area_events"] == 1
    assert diagnostics["areas"][1]["raw_mode"] == "p9"


def test_non_arm_events_are_ignored() -> None:
    tracker, notified = _tracker()
    tracker.apply_event({"Code": "AlarmInputSourceSignal", "Index": 0})
    assert tracker.events_received == 0
    assert notified == []


def test_invalidate_forgets_states_but_keeps_failure_history() -> None:
    tracker, notified = _tracker()
    for event in _global_burst("p1", failure=True) + _global_burst("p1"):
        tracker.apply_event(event)
    notified.clear()

    tracker.invalidate()
    assert tracker.system_state() is None
    assert tracker.last_failure is not None
    assert notified == [None]

    # Nothing to forget: no redraw.
    tracker.invalidate()
    assert notified == [None]
    assert tracker.invalidations == 2


def test_burst_notifies_once_after_it_settles() -> None:
    fired = threading.Event()
    calls: list[str | None] = []
    tracker = ArmingTracker(AREAS, quiet_seconds=0.05, max_delay_seconds=1.0)

    def callback() -> None:
        calls.append(tracker.system_state())
        fired.set()

    tracker.change_callback = callback
    for event in _global_burst("p1"):
        tracker.apply_event(event)
    assert fired.wait(2)
    time.sleep(0.1)
    # One redraw, straight to the final state, never a transient "mixed".
    assert calls == [ARMED_HOME]
    tracker.stop()


def test_max_delay_bounds_a_continuous_stream() -> None:
    fired = threading.Event()
    tracker = ArmingTracker(AREAS, fired.set, quiet_seconds=0.2, max_delay_seconds=0.05)
    tracker.apply_event(_event("AreaArmModeChange", 0, "D"))
    assert fired.wait(0.15)
    tracker.stop()


def test_stop_cancels_pending_notification() -> None:
    fired = threading.Event()
    tracker = ArmingTracker(AREAS, fired.set, quiet_seconds=0.05)
    tracker.apply_event(_event("AreaArmModeChange", 0, "D"))
    tracker.stop()
    assert not fired.wait(0.15)


def test_callback_errors_are_contained() -> None:
    def broken() -> None:
        raise RuntimeError("boom")

    tracker = ArmingTracker(AREAS, broken, quiet_seconds=0)
    tracker.apply_event(_event("AreaArmModeChange", 0, "D"))
    assert tracker.areas[0].state == DISARMED


def test_parse_abnormal_zones_tolerates_junk() -> None:
    assert parse_abnormal_zones({}) == []
    assert parse_abnormal_zones({"Abnormal": "x"}) == []
    assert parse_abnormal_zones(
        {"Abnormal": {"detail": ["x", {"ZoneAbnormal": ["y", {"Index": "4"}]}]}}
    ) == [
        {"area_id": None, "area": None, "zone_index": 4, "zone": None, "reason": None}
    ]


def test_engine_routes_arm_events_and_invalidates_on_new_generation() -> None:
    tracker, _ = _tracker()
    engine = StateEngine({}, arming=tracker)
    engine.begin_generation(1)
    engine._apply_event(_event("AreaArmModeChange", 1, "D"))
    assert tracker.areas[1].state == DISARMED
    # Zone counters are untouched by arm events.
    assert engine.realtime_events_received == 0

    engine.begin_generation(2)  # reconnect: changes may have been missed
    assert tracker.areas[1].state is None


def test_engine_without_tracker_ignores_arm_events() -> None:
    engine = StateEngine({})
    engine.begin_generation(1)
    engine._apply_event(_event("AreaArmModeChange", 1, "D"))
    assert engine.realtime_events_received == 0


def _inventory(table: Any) -> dict[str, Any]:
    return {
        "candidate_configs": {
            "AlarmSubSystem": {
                "ok": True,
                "response": {"result": True, "params": {"table": table}},
            }
        }
    }


def test_extract_arm_areas_uses_area_id_and_skips_disabled() -> None:
    table = [
        {"AreaId": 1, "Enable": True, "Name": "LivingRoom", "Zone": [6]},
        {"AreaId": 2, "Enable": True, "Name": "Office ", "Zone": []},
        {"AreaId": 3, "Enable": False, "Name": "Room3", "Zone": []},
        {"AreaId": 4, "Enable": True, "Name": "", "Zone": []},
        {"Enable": True, "Name": "No id"},
        "junk",
    ]
    assert extract_arm_areas(_inventory(table)) == {
        0: "LivingRoom",
        1: "Office",
        3: "Area 4",
        4: "No id",
    }


def test_extract_arm_areas_without_table() -> None:
    assert extract_arm_areas({}) == {}
    assert extract_arm_areas(_inventory(None)) == {}
