"""Build a realistic, network-free ArcHub for tests.

The hub is a real :class:`ArcHub` populated from representative ``Alarm[]``
records, a getChannelsState snapshot and an AirFly device map, with ``start``
and ``stop`` mocked so no socket or thread is ever opened.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.dahua_arc.hub import ArcHub
from custom_components.dahua_arc.protocol.cgi import (
    discover_alarm_points,
    discover_multiio_parents,
)
from custom_components.dahua_arc.protocol.engine import StateEngine
from custom_components.dahua_arc.protocol.inventory import (
    extract_radio_devices,
    extract_zone_area_hints,
)
from custom_components.dahua_arc.research.detector_test import (
    DetectorTestController,
)
from custom_components.dahua_arc.research.pircam import PirCamMedia

SERIAL = "ARC-TEST-001"
MULTIIO_PARENT = 6
KITCHEN_WINDOW = 7
HALL_PIRCAM = 9
PLACEHOLDER = 10
OFFLINE_DOOR = 11  # physical, but missing from the snapshot
KEYFOB = 12

RECORDS: dict[int, dict[str, str]] = {
    MULTIIO_PARENT: {
        "Name": "MultiIO board 2",
        "Enable": "true",
        "SenseMethod": "MultiIOTransmitterP",
        "Level1": "2",
        "Level2": "0",
        "Slot": "0",
    },
    KITCHEN_WINDOW: {
        "Name": "Kitchen Window",
        "Enable": "true",
        "SenseMethod": "MultiIOTransmitterP",
        "Level1": "2",
        "Level2": "1",
        "Slot": "0",
    },
    HALL_PIRCAM: {
        "Name": "Hall PIR Camera",
        "Enable": "true",
        "SenseMethod": "PIRCam",
        "Level1": "3",
        "Level2": "0",
        "Slot": "0",
    },
    PLACEHOLDER: {
        "Name": "Zone11",
        "Enable": "true",
        "SenseMethod": "Default",
        "Level1": "-1",
        "Level2": "-1",
        "Slot": "-1",
    },
    OFFLINE_DOOR: {
        "Name": "Garage Door",
        "Enable": "true",
        "SenseMethod": "MultiIOTransmitterP",
        "Level1": "2",
        "Level2": "2",
        "Slot": "0",
    },
    KEYFOB: {
        "Name": "Keyfob",
        "Enable": "true",
        "SenseMethod": "RemoteControl",
        "Level1": "4",
        "Level2": "0",
        "Slot": "0",
    },
}

SNAPSHOT = [
    {
        "array_pos": idx,
        "nIndex": idx + 1,
        "online": 1,
        "alarm_state": 5,
        "tamper": 0,
        "low_power": 0,
    }
    for idx in (MULTIIO_PARENT, KITCHEN_WINDOW, HALL_PIRCAM, KEYFOB)
]


def _rpc(params: dict) -> dict:
    return {"ok": True, "response": {"result": True, "params": params}}


def inventory(serial: str = SERIAL) -> dict:
    return {
        "system": {
            "magicBox.getSerialNo": _rpc({"sn": serial}),
            "magicBox.getDeviceType": _rpc({"type": "ARC3800H"}),
            "magicBox.getSoftwareVersion": _rpc({"version": "test-fw"}),
        },
        "candidate_configs": {
            "_AirFlyDeviceMap_": _rpc(
                {
                    "table": {
                        "DeviceInfo": [
                            {
                                "ShotAddr": 3,
                                "State": 1,
                                "SN": "PIRCAM-SN-1",
                                "ModelName": "ARD1731",
                            },
                            {"ShotAddr": 4, "State": 1, "SN": "FOB-SN-1"},
                        ]
                    }
                }
            ),
            "AlarmSubSystem": _rpc(
                {"table": [{"Enable": True, "Name": "Kitchen", "Zone": [7]}]}
            ),
        },
    }


def make_hub(*, research: bool = False, serial: str = SERIAL) -> ArcHub:
    hub = ArcHub(
        "192.0.2.10",
        80,
        5000,
        "admin",
        "test-only",
        enable_research_features=research,
    )
    hub.alarm_records = {idx: dict(cfg) for idx, cfg in RECORDS.items()}
    hub.parents = discover_multiio_parents(hub.alarm_records)
    hub.zones = discover_alarm_points(hub.alarm_records, SNAPSHOT, hub.parents)
    hub.rpc_inventory = inventory(serial)
    hub.area_hints = extract_zone_area_hints(hub.rpc_inventory)
    for idx, zone in hub.zones.items():
        zone.area_hint = hub.area_hints.get(idx, "")
    hub.radio_devices = extract_radio_devices(
        hub.rpc_inventory, hub.alarm_records, hub.area_hints
    )
    hub.engine = StateEngine(hub.zones, hub._notify)  # never started: no thread
    hub.engine.apply_snapshot(SNAPSHOT, "test", "initial", 0)
    hub.realtime = SimpleNamespace(
        connected=True,
        generation=1,
        reconnect_count=0,
        last_alarm_event_time=None,
        last_frame_time=None,
        last_error=None,
        health=lambda: {"connected": True},
    )
    if research:
        hub.pircam = PirCamMedia(
            hub.host, 80, "admin", "test-only", hub.zones, None, hub._notify
        )
        hub.detector_test = DetectorTestController(
            hub.host, 5000, "admin", "test-only", hub.zones, hub._notify
        )
    hub.start = Mock()
    hub.stop = Mock()
    return hub
