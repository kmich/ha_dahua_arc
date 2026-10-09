"""Area arm-state tracking from ARC arm/disarm events.

Event shapes follow an ARC3800H capture of a refused Home arm, the forced
Home arm that followed, and a disarm.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from custom_components.dahua_arc.protocol.arming import (
    ARMED_AWAY,
    ARMED_HOME,
    DISARMED,
    MIXED,
    ArmingTracker,
    parse_abnormal_zones,
    parse_area_arm_modes,
)
from custom_components.dahua_arc.protocol.engine import Reconciler, StateEngine
from custom_components.dahua_arc.protocol.inventory import extract_arm_areas
from custom_components.dahua_arc.protocol.models import Zone
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError

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


# AreaArmMode as read from an ARC3800H while armed Home: one row per
# AlarmSubSystem row; unused rows carry no ArmTime.
ARMED_HOME_TABLE = {
    "Areas": [{"ArmTime": 0, "Mode": "p1"}] * 3 + [{"Mode": "D"}] * 2,
    "SystemStatusCheck": {"Enable": True},
}


def test_parse_area_arm_modes() -> None:
    assert parse_area_arm_modes(ARMED_HOME_TABLE) == {
        0: "p1",
        1: "p1",
        2: "p1",
        3: "D",
        4: "D",
    }
    assert parse_area_arm_modes({"Areas": [{}, "x", {"Mode": "T"}]}) == {2: "T"}
    for junk in (None, [], {"Areas": None}):
        with pytest.raises(ValueError):
            parse_area_arm_modes(junk)


def test_table_sets_state_without_events() -> None:
    tracker, notified = _tracker()
    tracker.apply_table(parse_area_arm_modes(ARMED_HOME_TABLE), tracker.watermark())
    assert tracker.system_state() == ARMED_HOME
    assert tracker.areas[0].source == "AreaArmMode table"
    # Rows beyond the enabled areas are ignored.
    assert set(tracker.areas) == {0, 1, 2}
    assert notified == [None]
    assert tracker.table_corrections == 0

    # Same state again: no redraw.
    tracker.apply_table({0: "p1", 1: "p1", 2: "p1"}, tracker.watermark())
    assert notified == [None]
    assert tracker.table_reads == 2


def test_table_never_overrides_a_newer_event() -> None:
    tracker, _ = _tracker()
    watermark = tracker.watermark()  # the read starts here...
    tracker.apply_event(_event("AreaArmModeChange", 1, "D"))  # ...an event lands
    tracker.apply_table({0: "p1", 1: "p1", 2: "p1"}, watermark)
    assert tracker.areas[0].state == ARMED_HOME
    assert tracker.areas[1].state == DISARMED
    assert tracker.areas[1].source == "event"
    assert tracker.table_stale_rejects == 1
    # A later read is newer than that event and applies.
    tracker.apply_table({1: "p1"}, tracker.watermark())
    assert tracker.areas[1].state == ARMED_HOME
    assert tracker.table_corrections == 1


def test_table_unknown_mode_is_recorded() -> None:
    tracker, _ = _tracker()
    tracker.apply_table({0: "p7"}, tracker.watermark())
    assert tracker.areas[0].raw_mode == "p7"
    assert tracker.areas[0].state is None
    assert tracker.unknown_modes == {"p7"}


def test_invalidate_clears_table_source() -> None:
    tracker, _ = _tracker()
    tracker.apply_table({0: "D", 1: "D", 2: "D"}, tracker.watermark())
    tracker.invalidate()
    assert tracker.areas[0].source is None
    assert tracker.system_state() is None


class _TableClient:
    """Snapshot client stand-in: an empty zone snapshot plus a config table."""

    def __init__(self, table: Any = None, error: Exception | None = None):
        self.table, self.error = table, error

    def snapshot(self) -> list[dict[str, Any]]:
        return []

    def read_config(self, name: str) -> Any:
        assert name == "AreaArmMode"
        if self.error is not None:
            raise self.error
        return self.table


def test_reconciler_syncs_arm_state_after_zone_snapshot() -> None:
    tracker, _ = _tracker()
    engine = StateEngine({}, arming=tracker)
    Reconciler(_TableClient(ARMED_HOME_TABLE), engine).run("attach", "initial")
    assert tracker.system_state() == ARMED_HOME
    assert tracker.last_table_read is not None


def test_reconciler_arm_read_failure_does_not_fail_resync() -> None:
    tracker, _ = _tracker()
    engine = StateEngine({}, arming=tracker)
    client = _TableClient(error=RuntimeError("AreaArmMode read failed: 1"))
    assert Reconciler(client, engine).run("attach", "initial") == []
    assert tracker.system_state() is None
    assert tracker.last_table_error == "RuntimeError: AreaArmMode read failed: 1"

    client = _TableClient({"Areas": "junk"})
    Reconciler(client, engine).run("attach", "initial")
    assert tracker.last_table_error.startswith("ValueError")


def test_reconciler_arm_read_login_error_propagates() -> None:
    engine = StateEngine({}, arming=_tracker()[0])
    client = _TableClient(error=LoginError("rejected"))
    with pytest.raises(LoginError):
        Reconciler(client, engine).run("attach", "initial")


def test_reconciler_without_tracker_skips_arm_read() -> None:
    client = _TableClient(error=AssertionError("must not be called"))
    assert Reconciler(client, StateEngine({})).run("attach", "initial") == []


# -- alarms ------------------------------------------------------------------
# Shapes follow an ARC3800H capture: an instant window contact opened while
# armed Home, then a remote disarm.


def _alarm(
    zone_index: int, areas: list[int], action: str = "Start", **data: Any
) -> dict[str, Any]:
    return {
        "Action": action,
        "Code": "AlarmLocal",
        "Index": zone_index,
        "Data": {
            "AlarmType": "Intrusion",
            "AreaInfo": [{"Index": i, "Name": AREAS.get(i, "?")} for i in areas],
            "Areas": areas,
            "DefenceAreaType": "Intime",
            "DevType": "MultiIOTransmitterP",
            "Name": "Office Window ",
            **data,
        },
    }


def _alarm_clear(area_index: int, clear_type: str = "AlarmArea") -> dict[str, Any]:
    return {
        "Action": "Confirm",
        "Code": "AlarmClear",
        "Index": area_index,
        "Data": {
            "AreaInfo": [{"Index": area_index, "Name": AREAS.get(area_index, "?")}],
            "EventOptions": {"EventSource": "Hub", "EventType": "ArmOrDisarm"},
            "Mode": "D",
            "TriggerMode": "Remote",
            "Type": clear_type,
        },
    }


def test_alarm_unknown_until_arm_state_is_known() -> None:
    tracker, _ = _tracker()
    assert tracker.alarm_state() is None
    tracker.apply_table({0: "D", 1: "p1"}, tracker.watermark())
    # Office has no table row yet: still unknown.
    assert tracker.alarm_state() is None
    assert tracker.areas[0].alarm is False
    tracker.apply_event(_event("AreaArmModeChange", 2, "p1"))
    assert tracker.alarm_state() is False


def test_alarm_while_armed_lasts_until_disarm() -> None:
    tracker, notified = _tracker()
    for event in _global_burst("p1"):
        tracker.apply_event(event)
    assert tracker.alarm_state() is False
    notified.clear()

    tracker.apply_event(_alarm(192, [2]))
    office = tracker.areas[2]
    assert office.alarm is True
    assert tracker.alarm_state() is True
    assert tracker.alarm_areas() == [office]
    assert office.alarm_started is not None
    assert office.alarm_ended is None
    assert [
        (zone["area"], zone["zone"], zone["zone_index"], zone["alarm_type"])
        for zone in office.alarm_zones
    ] == [("Office", "Office Window", 192, "Intrusion")]
    assert tracker.last_alarm["areas"] == ["Office"]
    assert tracker.areas[0].alarm is False
    assert notified == [None]

    # The same zone again, or the input restoring, does not end the alarm.
    tracker.apply_event(_alarm(192, [2]))
    tracker.apply_event(_alarm(192, [2], action="Stop"))
    assert office.alarm is True
    assert len(office.alarm_zones) == 1
    # A second zone joins the running alarm.
    tracker.apply_event(_alarm(163, [2], Name="Office PIR"))
    assert [zone["zone"] for zone in office.alarm_zones] == [
        "Office Window",
        "Office PIR",
    ]

    for event in _global_burst("D"):
        tracker.apply_event(event)
    assert office.alarm is False
    assert office.alarm_ended is not None
    # The ended alarm's details stay for reference.
    assert len(office.alarm_zones) == 2
    tracker.apply_event(_alarm_clear(2))
    assert tracker.alarm_state() is False
    assert tracker.diagnostics()["alarm_events_received"] == 5


def test_alarm_clear_ends_an_alarm_without_disarm() -> None:
    tracker, _ = _tracker()
    tracker.apply_table({0: "D", 1: "D", 2: "D"}, tracker.watermark())
    # A 24-hour zone can alarm while disarmed.
    tracker.apply_event(_alarm(192, [2]))
    assert tracker.areas[2].alarm is True
    # A periodic table read showing the area disarmed does not end it.
    tracker.apply_table({0: "D", 1: "D", 2: "D"}, tracker.watermark())
    assert tracker.areas[2].alarm is True
    tracker.apply_event(_alarm_clear(2))
    assert tracker.areas[2].alarm is False
    assert tracker.areas[2].alarm_ended is not None


def test_alarm_area_falls_back_to_area_info() -> None:
    tracker, _ = _tracker()
    event = _alarm(192, [1])
    del event["Data"]["Areas"]
    tracker.apply_event(event)
    assert tracker.areas[1].alarm is True


def test_alarm_unknown_area_and_clear_type_are_recorded_not_guessed() -> None:
    tracker, notified = _tracker()
    tracker.apply_event(_alarm(192, [9]))
    assert tracker.alarm_areas() == []
    assert tracker.unknown_area_events == 1
    assert tracker.last_alarm["areas"] == []
    assert notified == []

    tracker.apply_event(_alarm(192, [2]))
    tracker.apply_event(_alarm_clear(2, clear_type="Fire"))
    assert tracker.areas[2].alarm is True
    tracker.apply_event(_alarm_clear(9))
    assert tracker.unknown_area_events == 2
    assert tracker.diagnostics()["unknown_clear_types"] == ["Fire"]


def test_reconnect_keeps_an_alarm_until_a_table_read_shows_disarm() -> None:
    tracker, _ = _tracker()
    for event in _global_burst("T"):
        tracker.apply_event(event)
    tracker.apply_event(_alarm(192, [2]))

    tracker.invalidate()
    assert tracker.areas[0].alarm is None
    assert tracker.areas[2].alarm is True
    assert tracker.areas[2].alarm_unconfirmed is True
    assert tracker.alarm_state() is True

    # Still armed after the reconnect: the ARC still holds the alarm.
    tracker.apply_table({0: "T", 1: "T", 2: "T"}, tracker.watermark())
    assert tracker.areas[2].alarm is True
    assert tracker.areas[2].alarm_unconfirmed is False
    assert tracker.areas[0].alarm is False

    # Disarmed while detached: the alarm ended unseen.
    tracker.invalidate()
    tracker.apply_table({0: "D", 1: "D", 2: "D"}, tracker.watermark())
    assert tracker.areas[2].alarm is False
    assert tracker.areas[2].alarm_ended is not None
    assert tracker.alarm_state() is False


def test_engine_routes_alarm_local_to_tracker_and_pircam_zone() -> None:
    tracker, _ = _tracker()
    pircam = Zone(index=5, name="Hall PIRCam", sense_method="PIRCam")
    window = Zone(index=192, name="Office Window", sense_method="MultiIO")
    engine = StateEngine({5: pircam, 192: window}, arming=tracker)
    engine.begin_generation(1)

    # A wired/wireless contact alarm never touches zone state.
    engine._apply_event(_alarm(192, [2]))
    assert tracker.areas[2].alarm is True
    assert window.active is None
    assert engine.realtime_events_received == 0

    # A PIRCam alarm is both an alarm and that camera's motion.
    engine._apply_event(_alarm(5, [0], DevType="PIRCam", SenseMethod="PIRCam"))
    assert tracker.areas[0].alarm is True
    assert pircam.active is True

    engine._apply_event(_alarm_clear(2))
    assert tracker.areas[2].alarm is False
