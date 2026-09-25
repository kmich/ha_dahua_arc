"""Realtime watchdog, authentication-failure handling and research control."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from custom_components.dahua_arc.hub import ArcHub
from custom_components.dahua_arc.protocol import realtime as realtime_mod
from custom_components.dahua_arc.protocol.engine import StateEngine
from custom_components.dahua_arc.protocol.inventory import EventCatalog
from custom_components.dahua_arc.protocol.models import Zone
from custom_components.dahua_arc.protocol.realtime import RealtimeClient
from custom_components.dahua_arc.protocol.snapshot import SnapshotClient
from custom_components.dahua_arc.research import detector_test as detector_mod
from custom_components.dahua_arc.research.detector_test import (
    DetectorTestController,
)
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.closed = False
        self.shut_down = False
        self.sock = object()

    def send_request(self, method, params=None, **kwargs) -> int:
        self.sent.append((method, params))
        return len(self.sent)

    def shutdown(self) -> None:
        self.shut_down = True

    def close(self) -> None:
        self.closed = True


def _realtime(**kwargs) -> RealtimeClient:
    engine = StateEngine({})
    return RealtimeClient(
        "192.0.2.10",
        5000,
        "admin",
        "test-only",
        engine,
        Mock(),
        EventCatalog(),
        **kwargs,
    )


@pytest.fixture
def fast_keepalive():
    with patch.object(realtime_mod, "keepalive_delay", return_value=0.01):
        yield


def test_watchdog_forces_reconnect_on_silent_stream(fast_keepalive) -> None:
    client = _realtime()
    transport = FakeTransport()
    client._set_transport(transport)
    client.keepalive_interval = 60
    client._last_rx_monotonic = time.monotonic() - 1000

    client._keepalive_loop(transport, threading.Event())

    assert transport.shut_down and transport.closed
    assert client.transport is None
    assert client.watchdog_disconnects == 1
    assert client.last_error.startswith("No DHIP frame")
    assert transport.sent == []


def test_keepalive_sent_while_stream_is_healthy(fast_keepalive) -> None:
    client = _realtime()
    transport = FakeTransport()
    client._set_transport(transport)
    stop = threading.Event()
    client._last_rx_monotonic = time.monotonic()

    def stop_after_first(method, params=None, **kwargs):
        stop.set()
        return FakeTransport.send_request(transport, method, params)

    transport.send_request = stop_after_first
    client._keepalive_loop(transport, stop)
    assert transport.sent[0][0] == "global.keepAlive"
    assert not transport.closed
    assert client.keepalive_request_id == 1


def test_liveness_timeout_is_bounded_by_keepalive_interval() -> None:
    assert realtime_mod.liveness_timeout(60) == 125
    assert realtime_mod.liveness_timeout(20) == 45


def test_realtime_login_error_stops_reconnect_and_reports_once() -> None:
    health = Mock()
    failed = Mock()
    client = _realtime(health_callback=health, auth_failed_callback=failed)
    error = LoginError("Unknown user or wrong password", code=268632080)
    with patch.object(client, "_attach", side_effect=error) as attach:
        client._run()
    attach.assert_called_once()
    failed.assert_called_once_with(error)
    assert client.auth_failed is True
    assert client.ready_event.is_set()
    assert client.last_login_error is error
    health.assert_called()


def test_snapshot_client_refuses_logins_after_auth_failure() -> None:
    client = SnapshotClient("192.0.2.10", 5000, "admin", "bad")
    client.auth_failed = True
    with (
        patch(
            "custom_components.dahua_arc.protocol.snapshot.DHIPTransport"
        ) as transport,
        pytest.raises(LoginError, match="reauthentication required"),
    ):
        client.snapshot()
    transport.assert_not_called()


def test_hub_periodic_resync_stops_on_login_error() -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "bad", periodic_resync_seconds=0)
    hub.reconciler = Mock()
    hub.reconciler.run.side_effect = LoginError("bad")
    hub.on_auth_failed = Mock()
    listener = Mock()
    hub.add_listener(listener)

    worker = threading.Thread(target=hub._periodic_loop)
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    hub.reconciler.run.assert_called_once()
    hub.on_auth_failed.assert_called_once()
    listener.assert_called_with(None)
    assert hub.auth_failed


def test_inventory_summary_static_part_is_cached() -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only")
    with patch(
        "custom_components.dahua_arc.hub.summarize_rpc_inventory", return_value={}
    ) as summarize:
        hub.inventory_summary()
        hub.event_catalog.observe({"Code": "Test"})
        summary = hub.inventory_summary()
    summarize.assert_called_once()
    assert summary["all_events_observed"] == 1
    assert summary["event_codes_observed"] == 1


def test_snapshot_health_change_notifies_zone() -> None:
    zone = Zone(index=3, name="Door", active=False, online_state=1)
    changed: list[set[int]] = []
    engine = StateEngine({3: zone}, change_callback=changed.append)
    engine.apply_snapshot(
        [{"array_pos": 3, "nIndex": 4, "online": 0, "alarm_state": 5}],
        source="test",
        source_kind="periodic",
        event_watermark=0,
    )
    assert changed == [{3}]
    assert zone.online_state == 0


# -- research: detector test ---------------------------------------------------


def _pircams() -> dict[int, Zone]:
    return {
        4: Zone(index=4, name="Garden PIR", sense_method="PIRCam", level1=5),
        6: Zone(index=6, name="Hall PIR", sense_method="PIRCam", level1=7),
        8: Zone(index=8, name="Door", sense_method="MagneticContact", level1=9),
        9: Zone(index=9, name="Unpaired PIR", sense_method="PIRCam", level1=None),
    }


def test_detector_test_targets_every_paired_pircam() -> None:
    controller = DetectorTestController(
        "192.0.2.10", 5000, "admin", "test-only", _pircams(), Mock()
    )
    assert sorted(controller.targets) == [4, 6]
    assert controller.status(6)["target_name"] == "Hall PIR"
    with pytest.raises(RuntimeError, match="not a PIRCam"):
        controller.stop(8)


def test_detector_test_rpc_sequence_and_cleanup() -> None:
    calls: list[tuple[str, object, object]] = []

    class Transport:
        def __init__(self, *args, **kwargs) -> None:
            self.sock = object()

        def connect(self) -> None:
            pass

        def login(self, *args) -> dict:
            return {"result": True}

        def call(self, method, params=None, *, object_id=None, **kwargs):
            calls.append((method, params, object_id))
            if method == "LowRateWPAN.factory.instance":
                return {"result": 77}
            if method == "LowRateWPAN.setAccessoryParam":
                ok = "ShortAddr" in params["Info"]
                return {"result": ok}
            return {"result": True}

        def close(self) -> None:
            calls.append(("close", None, None))

    notify = Mock()
    controller = DetectorTestController(
        "192.0.2.10", 5000, "admin", "test-only", _pircams(), notify
    )
    with patch.object(detector_mod, "DHIPTransport", Transport):
        controller._rpc(6, True)
    assert calls[0] == ("LowRateWPAN.factory.instance", None, None)
    assert calls[1] == (
        "LowRateWPAN.setAccessoryParam",
        {"Info": {"ShortAddr": 7, "SensitivityTest": 1}},
        77,
    )
    assert calls[-2] == ("LowRateWPAN.destroy", None, 77)
    assert calls[-1][0] == "close"
    status = controller.status(6)
    assert status["enabled"] is True
    assert status["successful_style"] == "InfoPascal"
    notify.assert_called_with({6})


def test_hub_research_delegation_is_off_by_default() -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "test-only")
    assert hub.pircam_snapshot(1) is None
    assert hub.fetch_pircam_image(1) is None
    assert hub.detector_test_status(1) == {}
    with pytest.raises(RuntimeError, match="disabled"):
        hub.start_detector_test(1)
    hub.realtime = SimpleNamespace(connected=True)
    assert hub.available


def test_watchdog_message_survives_its_own_reconnect_only(fast_keepalive) -> None:
    client = _realtime()
    attempts = iter(
        [
            FakeTransport(),  # first attach succeeds, then the stream stalls
            OSError("connection refused"),  # the next attempt fails differently
            LoginError("stop the loop"),
        ]
    )

    seen_before_attempt: list[str | None] = []

    def attach():
        seen_before_attempt.append(client.last_error)
        item = next(attempts)
        if isinstance(item, Exception):
            raise item
        return item

    def read_until_watchdog(transport, generation):
        # The real keepalive thread started by _run() must detect the stall
        # and close the transport, which unblocks the reader.
        assert _wait_for(lambda: transport.closed)
        raise OSError("socket closed")

    errors: list[str | None] = []
    with (
        patch.object(client, "_attach", side_effect=attach),
        patch.object(client, "_read_events_forever", side_effect=read_until_watchdog),
        patch.object(realtime_mod, "RECONNECT_DELAYS", (0,)),
        patch.object(realtime_mod, "liveness_timeout", return_value=0.05),
        patch.object(
            client,
            "_notify_health",
            side_effect=lambda: errors.append(client.last_error),
        ),
    ):
        client._run()
    assert client.watchdog_disconnects == 1
    assert any(e and e.startswith("No DHIP frame") for e in errors)
    # Before the 2nd attempt the watchdog's reason is shown; before the 3rd,
    # the unrelated "connection refused" failure is reported as itself.
    assert seen_before_attempt[1].startswith("No DHIP frame")
    assert seen_before_attempt[2] == "OSError: connection refused"


def test_auth_failure_stops_research_poller() -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "bad", enable_research_features=True)
    hub.wpan_research = SimpleNamespace(stop_event=threading.Event())
    hub._handle_auth_failure(LoginError("bad"))
    assert hub.wpan_research.stop_event.is_set()
