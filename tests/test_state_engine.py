from __future__ import annotations

import io
import json
import struct

import pytest
from custom_components.dahua_arc.protocol import engine as engine_mod
from custom_components.dahua_arc.protocol import util
from custom_components.dahua_arc.protocol.models import Zone
from custom_components.dahua_arc.vendor.dahua import const
from custom_components.dahua_arc.vendor.dahua import transport as transport_mod
from custom_components.dahua_arc.vendor.dahua.exceptions import DHIPError

EVENT_CODE = engine_mod.EVENT_CODE
StateEngine = engine_mod.StateEngine
raw_to_active = util.raw_to_active


def test_raw_to_active_maps_known_arc_values() -> None:
    assert raw_to_active(1) is True
    assert raw_to_active(5) is False
    assert raw_to_active(0) is None
    assert raw_to_active(None) is None


def test_realtime_alarm_input_event_updates_zone_state() -> None:
    zone = Zone(index=7, name="Basement Door", classification="opening")
    changed: list[set[int]] = []
    engine = StateEngine({7: zone}, change_callback=changed.append)
    engine.begin_generation(1)

    engine._apply_event(
        {
            "Code": EVENT_CODE,
            "Index": 7,
            "Action": "Start",
            "Data": {"ExChannel": 3, "SN": "redacted-test-sn"},
        }
    )

    assert zone.active is True
    assert zone.raw_alarm_state == 1
    assert zone.event_exchannel == 3
    assert zone.parent_sn == "redacted-test-sn"
    assert zone.last_action == "Start"
    assert engine.realtime_events_received == 1
    assert engine.realtime_state_changes == 1
    assert changed == [{7}]


def test_duplicate_realtime_event_is_counted_without_state_change_callback() -> None:
    zone = Zone(index=2, name="Kitchen Window", classification="opening", active=True)
    changed: list[set[int]] = []
    engine = StateEngine({2: zone}, change_callback=changed.append)

    engine._apply_event({"Code": EVENT_CODE, "Index": 2, "Action": "Start"})

    assert zone.active is True
    assert engine.duplicate_events == 1
    assert changed == []


def test_sensor_abnormal_tracks_fault_without_changing_alarm_state() -> None:
    zone = Zone(index=9, name="Hall Shutter", active=False)
    changed: list[set[int]] = []
    engine = StateEngine({9: zone}, change_callback=changed.append)
    fault = {
        "Code": engine_mod.SENSOR_FAULT_EVENT_CODE,
        "Index": 9,
        "Data": {"Status": "Break"},
    }

    engine._apply_event({**fault, "Action": "Start"})
    assert zone.sensor_fault == "Break"
    assert zone.active is False
    assert engine.event_sequence == 0
    assert changed == [{9}]

    # A repeat changes nothing, and an unknown zone or action is ignored.
    engine._apply_event({**fault, "Action": "Start"})
    engine._apply_event({**fault, "Action": "Pulse"})
    engine._apply_event({**fault, "Index": 99, "Action": "Start"})
    assert changed == [{9}]

    engine._apply_event({**fault, "Action": "Stop"})
    assert zone.sensor_fault == ""
    assert changed == [{9}, {9}]


def test_sensor_abnormal_without_status_is_still_a_fault() -> None:
    zone = Zone(index=9)
    engine = StateEngine({9: zone})
    engine._apply_event(
        {"Code": engine_mod.SENSOR_FAULT_EVENT_CODE, "Index": 9, "Action": "Start"}
    )
    assert zone.sensor_fault == "Unknown"


def test_new_generation_makes_only_active_faults_unknown() -> None:
    faulted = Zone(index=1, sensor_fault="Break")
    clean = Zone(index=2)
    changed: list[set[int]] = []
    engine = StateEngine({1: faulted, 2: clean}, change_callback=changed.append)

    engine.begin_generation(1)
    assert faulted.sensor_fault is None
    assert clean.sensor_fault == ""
    assert changed == [{1}]

    # The next event resolves it either way.
    engine._apply_event(
        {"Code": engine_mod.SENSOR_FAULT_EVENT_CODE, "Index": 1, "Action": "Stop"}
    )
    assert faulted.sensor_fault == ""


def test_snapshot_rejects_stale_values_after_newer_event() -> None:
    zone = Zone(index=4, name="Office PIR", classification="motion")
    engine = StateEngine({4: zone})

    engine._apply_event({"Code": EVENT_CODE, "Index": 4, "Action": "Start"})
    watermark_before_event = 0

    changed, skipped = engine.apply_snapshot(
        [
            {
                "array_pos": 4,
                "nIndex": 5,
                "online": 1,
                "tamper": 0,
                "low_power": 0,
                "alarm_state": 5,
            }
        ],
        source="unit-test stale snapshot",
        source_kind="periodic",
        event_watermark=watermark_before_event,
    )

    assert zone.active is True
    assert changed == []
    assert skipped == [zone]
    assert engine.stale_snapshot_rejects == 1


def test_snapshot_can_reconcile_zone_state_when_not_stale() -> None:
    zone = Zone(index=1, name="Flood Sensor", classification="flood_or_water")
    engine = StateEngine({1: zone})

    changed, skipped = engine.apply_snapshot(
        [
            {
                "array_pos": 1,
                "nIndex": 2,
                "online": 1,
                "tamper": 0,
                "low_power": 0,
                "alarm_state": 1,
            }
        ],
        source="unit-test initial snapshot",
        source_kind="initial",
        event_watermark=0,
    )

    assert zone.active is True
    assert zone.raw_alarm_state == 1
    assert zone.online_state == 1
    assert skipped == []
    assert changed == []
    assert engine.snapshot_count == 1


def test_raw_normal_snapshot_clears_active_zone() -> None:
    zone = Zone(
        index=5, name="Garage Door", classification="multiio_input", active=True
    )
    engine = StateEngine({5: zone})
    engine.apply_snapshot(
        [{"array_pos": 5, "nIndex": 6, "online": 1, "alarm_state": 5}],
        source="normal snapshot",
        source_kind="periodic",
        event_watermark=0,
    )
    assert zone.active is False
    assert zone.raw_alarm_state == 5


def test_unknown_raw_state_does_not_claim_closed() -> None:
    zone = Zone(index=5, name="Garage Door", classification="multiio_input")
    engine = StateEngine({5: zone})
    engine.apply_snapshot(
        [{"array_pos": 5, "nIndex": 6, "online": 1, "alarm_state": None}],
        source="unknown snapshot",
        source_kind="initial",
        event_watermark=0,
    )
    assert zone.active is None


def test_fragmented_snapshot_reconstruction_and_order_guard() -> None:
    payload = json.dumps({"result": True, "params": {"States": []}}).encode()
    halves = (payload[:15], payload[15:])
    header = const.HEADER_FMT

    def frame(part: bytes, index: int) -> bytes:
        return (
            struct.pack(
                header,
                const.HEADER_SIZE,
                const.DHIP_MAGIC,
                1,
                42,
                len(part),
                index,
                len(payload),
                0,
            )
            + part
        )

    def fake_transport(data: bytes):
        transport = transport_mod.DHIPTransport("192.0.2.10")
        stream = io.BytesIO(data)
        transport.recv_exact = stream.read
        return transport

    result, fragments, byte_count = fake_transport(
        frame(halves[0], 0) + frame(halves[1], 1)
    ).recv_fragmented_json(42)
    assert result["result"] is True
    assert fragments == 2
    assert byte_count == len(payload)

    with pytest.raises(DHIPError, match="fragment order mismatch"):
        fake_transport(frame(halves[0], 1)).recv_fragmented_json(42)
    with pytest.raises(DHIPError, match="request id mismatch"):
        fake_transport(frame(halves[0], 0)).recv_fragmented_json(41)
