"""Drive a real ArcHub against the in-process fake ARC."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from unittest.mock import patch

import pytest
from custom_components.dahua_arc.hub import ArcHub, probe_connection
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from tests.fake_arc import FakeArc
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
