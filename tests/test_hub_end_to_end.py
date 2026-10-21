"""Drive a real ArcHub against the in-process fake ARC."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from unittest.mock import patch

import pytest
from custom_components.dahua_arc.hub import ArcHub, probe_connection
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from tests.fake_arc import ARM_METHOD, FakeArc
from tests.hub_factory import (
    HALL_PIRCAM,
    KITCHEN_WINDOW,
    OFFLINE_DOOR,
    RECORDS,
    SNAPSHOT,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body\xff\xd9"


def _states() -> list[dict]:
    return [
        {
            "Index": item["nIndex"],
            "AlarmState": "Normal",
            "OnlineState": 1,
            "SensorState": {"Tamper": 0, "LowPowerState": 0},
        }
        for item in SNAPSHOT
    ]


def _arc(**kwargs) -> FakeArc:
    return FakeArc(
        snapshot=_states(),
        inventory={
            "_AirFlyDeviceMap_": {
                "DeviceInfo": [
                    {"ShotAddr": 3, "State": 1, "SN": "PIRCAM-SN-1"},
                ]
            },
            "AlarmSubSystem": [{"Enable": True, "Name": "Kitchen", "Zone": [7]}],
        },
        **kwargs,
    )


def _wait(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def cgi_records():
    with (
        patch(
            "custom_components.dahua_arc.hub.fetch_alarm_config",
            return_value={k: dict(v) for k, v in RECORDS.items()},
        ),
    ):
        yield


def _dahua_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("dahua-arc")]


def test_start_stream_events_and_stop(cgi_records) -> None:
    arc = _arc()
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only", 3600)
    notified: list[set[int] | None] = []
    hub.add_listener(notified.append)
    with arc.patch():
        try:
            hub.start()
            assert hub.available
            assert set(hub.primary_zones) == {KITCHEN_WINDOW, HALL_PIRCAM}
            assert hub.serial_number == "ARC-TEST-001"
            assert hub.zones[KITCHEN_WINDOW].area_hint == "Kitchen"
            assert hub.zones[KITCHEN_WINDOW].active is False
            assert hub.snapshot_client.last_snapshot_fragment_count == 3
            assert "eventManager.attach" in arc.calls
            # Production inventory never enumerates the RPC method catalog.
            assert "system.methodSignature" not in arc.calls

            arc.push_event(
                {
                    "Code": "AlarmInputSourceSignal",
                    "Index": KITCHEN_WINDOW,
                    "Action": "Start",
                }
            )
            assert _wait(lambda: hub.zones[KITCHEN_WINDOW].active is True)
            assert _wait(lambda: {KITCHEN_WINDOW} in notified)
            assert hub.event_catalog.counts() == (1, 1)
        finally:
            hub.stop()
    assert not hub.available
    assert arc.sockets == []
    assert _wait(lambda: not _dahua_threads())


def test_arm_events_and_arm_state_probe(cgi_records) -> None:
    arc = _arc()
    arc.config_tables["AreaArmMode"] = {"Areas": [{"ArmTime": 0, "Mode": "D"}]}
    arc.method_lists["alarmSubSystem"] = ["alarmSubSystem.getState"]
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only", 3600)
    redraws: list[None] = []
    hub.add_arm_listener(lambda: redraws.append(None))
    with arc.patch():
        try:
            hub.start()
            assert set(hub.arming.areas) == {0}
            # Known before setup finishes: the attach resync reads the table.
            assert hub.arming.system_state() == "disarmed"
            assert hub.arming.areas[0].source == "AreaArmMode table"
            for code, index in (
                ("GlobalAreaArmModeChange", -1),
                ("AreaArmModeChange", 0),
            ):
                arc.push_event(
                    {
                        "Action": "Pulse",
                        "Code": code,
                        "Index": index,
                        "Data": {"Mode": "p1", "IsGlobal": True, "Profile": "Auto"},
                    }
                )
            assert _wait(lambda: hub.arming.system_state() == "armed_home")
            assert hub.arming.areas[0].source == "event"
            assert _wait(lambda: len(redraws) >= 1)
            # Arm events are catalogued but never counted as zone events.
            assert hub.engine.realtime_events_received == 0

            hub.refresh_arm_state_probe()
            probe = hub.diagnostics()["arm_state_probe"]
            assert probe["configs"]["AreaArmMode"]["ok"] is True
            assert probe["configs"]["DefenceStatus"]["ok"] is False
            assert probe["method_lists"]["alarmSubSystem"] == [
                "alarmSubSystem.getState"
            ]
            assert probe["method_lists"]["AlarmRegion"]["ok"] is False
            # Introspection only: nothing discovered is ever called.
            assert "alarmSubSystem.getState" not in arc.calls
            assert hub.diagnostics()["arming"]["areas"][1]["raw_mode"] == "p1"
        finally:
            hub.stop()
    assert arc.sockets == []


def test_arm_state_table_resync_and_missing_table(cgi_records) -> None:
    arc = _arc()  # no AreaArmMode table: the read is refused
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only", 3600)
    with arc.patch():
        try:
            hub.start()
            # A refused read leaves arm state unknown but setup succeeds.
            assert hub.available
            assert hub.arming.system_state() is None
            assert "AreaArmMode read failed" in hub.arming.last_table_error

            # Armed while an event was missed: the next resync corrects it.
            arc.config_tables["AreaArmMode"] = {"Areas": [{"Mode": "T"}]}
            hub.reconciler.run("test resync", "periodic")
            assert hub.arming.system_state() == "armed_away"
            assert hub.arming.last_table_error is None
            arc.config_tables["AreaArmMode"] = {"Areas": [{"Mode": "D"}]}
            hub.reconciler.run("test resync", "periodic")
            assert hub.arming.system_state() == "disarmed"
            assert hub.arming.table_corrections == 1
            # The session survived the refused read.
            assert hub.snapshot_client.health()["successful_connections"] == 1
        finally:
            hub.stop()
    assert arc.sockets == []


def test_arm_state_probe_failure_is_recorded(cgi_records) -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only", 3600)
    with patch(
        "custom_components.dahua_arc.hub.InventoryRpcClient.connect",
        side_effect=OSError("unreachable"),
    ):
        hub.refresh_arm_state_probe()
    assert hub.arm_state_probe["error"] == "OSError: unreachable"
    assert not hub.auth_failed

    with patch(
        "custom_components.dahua_arc.hub.InventoryRpcClient.connect",
        side_effect=LoginError("bad password"),
    ):
        hub.refresh_arm_state_probe()
    assert hub.auth_failed
    # No further logins once the ARC has rejected the credentials.
    with patch("custom_components.dahua_arc.hub.InventoryRpcClient.connect") as connect:
        hub.refresh_arm_state_probe()
    connect.assert_not_called()


def test_wrong_password_raises_login_error(cgi_records) -> None:
    arc = _arc(password="something-else")
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only")
    with arc.patch():
        try:
            with pytest.raises(LoginError):
                hub.start()
        finally:
            hub.stop()
    assert arc.calls.count("global.login") == 2  # challenge + one real attempt
    assert arc.sockets == []


def test_probe_connection_reports_serial_and_area_items() -> None:
    arc = _arc()
    records = {k: dict(v) for k, v in RECORDS.items()}
    with (
        arc.patch(),
        patch(
            "custom_components.dahua_arc.hub.fetch_alarm_config",
            return_value=records,
        ),
    ):
        # A configured MultiIO input missing from the snapshot fails setup.
        with pytest.raises(RuntimeError, match=f"indexes: \\[{OFFLINE_DOOR}\\]"):
            probe_connection("192.0.2.10", 80, 5000, "admin", "test-only")
        del records[OFFLINE_DOOR]
        result = probe_connection("192.0.2.10", 80, 5000, "admin", "test-only")
    assert result["serial_number"] == "ARC-TEST-001"
    assert result["zones"] == 2
    assert {
        "index": KITCHEN_WINDOW,
        "name": "Kitchen Window",
        "area_hint": "Kitchen",
    } in (result["area_match_items"])
    assert arc.sockets == []


def test_research_pircam_image_download(cgi_records) -> None:
    arc = _arc()
    arc.files["/var/tmp/snap.jpg"] = JPEG
    hub = ArcHub(
        "192.0.2.10",
        80,
        5000,
        "admin",
        "test-only",
        3600,
        enable_research_features=True,
    )
    with (
        arc.patch(),
        patch("custom_components.dahua_arc.research.wpan.WPANResearchPoller.start"),
    ):
        try:
            hub.start()
            assert sorted(hub.detector_test.targets) == [HALL_PIRCAM]
            hub.pircam.process_event(
                {
                    "Code": "ManualTest",
                    "Index": HALL_PIRCAM,
                    "Data": {"DevType": "PIRCam", "DelayUploadSeq": "42"},
                }
            )
            hub.pircam.process_event(
                {
                    "Code": "SpecialFileDelayUpload",
                    "Data": {
                        "UploadSeq": "42",
                        "Files": [{"FilePath": "/var/tmp/snap.jpg", "Length": 26}],
                    },
                }
            )
            assert hub.fetch_pircam_image(HALL_PIRCAM) == JPEG
            meta = hub.pircam_snapshot(HALL_PIRCAM)
            assert meta["last_fetch_method"] == "dhip-temp"
            assert meta["last_fetch_bytes"] == len(JPEG)
            # The snapshot connection is still usable after the download.
            assert hub.snapshot_client.snapshot()
        finally:
            hub.stop()
    assert arc.sockets == []


def _arm_hub(*, enabled: bool, spec: bool) -> ArcHub:
    hub = ArcHub(
        "192.0.2.10",
        80,
        5000,
        "admin",
        "test-only",
        3600,
        enable_arm_control=enabled,
    )
    if spec:
        from custom_components.dahua_arc.protocol.control import FAKE_COMMAND_SPEC

        hub.arm_command_spec = FAKE_COMMAND_SPEC
        hub.allow_fake_arm_spec = True
    return hub


def test_arm_control_is_off_unless_opted_in(cgi_records) -> None:
    arc = _arc()
    arc.config_tables["AreaArmMode"] = {"Areas": [{"Mode": "D"}]}
    hub = _arm_hub(enabled=False, spec=True)
    with arc.patch():
        try:
            hub.start()
            assert hub.arm_control is None
            assert not hub.arm_control_unsupported
            assert hub.arm_control_diagnostics() is None
        finally:
            hub.stop()
    assert ARM_METHOD not in arc.calls


def test_arm_control_without_a_verified_command_is_inert(cgi_records) -> None:
    arc = _arc()
    arc.config_tables["AreaArmMode"] = {"Areas": [{"Mode": "D"}]}
    hub = _arm_hub(enabled=True, spec=False)
    with arc.patch():
        try:
            hub.start()
            assert hub.arm_control is None
            assert hub.arm_control_unsupported
            assert hub.arm_control_diagnostics() == {
                "enabled": True,
                "supported": False,
            }
        finally:
            hub.stop()
    assert ARM_METHOD not in arc.calls


def test_arm_command_is_confirmed_by_the_real_event_stream(cgi_records) -> None:
    from custom_components.dahua_arc.protocol.control import CommandOutcome

    arc = _arc()
    arc.config_tables["AreaArmMode"] = {"Areas": [{"Mode": "D"}]}
    arc.config_tables["AlarmSubSystem"] = [
        {"Enable": True, "AreaId": 1, "Name": "Kitchen", "Zone": [7]}
    ]
    hub = _arm_hub(enabled=True, spec=True)
    with arc.patch():
        try:
            hub.start()
            assert hub.arm_control is not None
            assert hub.area_zones == {0: [KITCHEN_WINDOW]}
            assert hub.arming.system_state() == "disarmed"
            from custom_components.dahua_arc.protocol.control import ArmCommand

            result = hub.arm_control.execute(ArmCommand(mode="T", areas=(0,)))
            assert result.outcome is CommandOutcome.CONFIRMED
            assert result.confirmed_by == "event"
            assert hub.arming.system_state() == "armed_away"
            assert arc.calls.count(ARM_METHOD) == 1
            assert hub.arm_control_diagnostics()["history"][0]["outcome"] == (
                "confirmed"
            )
            # The control session is closed again; realtime and snapshot remain.
            assert len(arc.sockets) == 2

            # Unload releases a waiting command instead of hanging.
            hub.arm_stop.set()
            stopped = hub.arm_control.execute(ArmCommand(mode="D", areas=(0,)))
            assert stopped.reason == "unloading"
        finally:
            hub.stop()
    assert arc.calls.count(ARM_METHOD) == 1


def test_open_zones_reads_live_zone_state(cgi_records) -> None:
    arc = _arc()
    hub = _arm_hub(enabled=False, spec=False)
    with arc.patch():
        try:
            hub.start()
            assert hub.open_zones((0,)) == []
            arc.push_event(
                {
                    "Code": "AlarmInputSourceSignal",
                    "Index": KITCHEN_WINDOW,
                    "Action": "Start",
                }
            )
            assert _wait(lambda: hub.open_zones((0,)) == ["Kitchen Window"])
            assert hub.open_zones((5,)) is None
        finally:
            hub.stop()
