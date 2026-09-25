"""Realtime ``eventManager.attach`` stream with reconnect and liveness watchdog."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..vendor.dahua import DHIPTransport, const
from ..vendor.dahua.exceptions import LoginError
from .engine import EVENT_CODE, PIRCAM_EVENT_CODE, Reconciler, StateEngine
from .inventory import EventCatalog
from .util import keepalive_delay, timestamp

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAYS = (5, 10, 20, 30, 60)
# Connect/login/attach must complete quickly; only the attached event stream
# is allowed to idle between keepalive replies.
ATTACH_TIMEOUT_SECONDS = 15.0
# The ARC answers every keepalive, so a healthy stream always delivers a frame
# at least once per keepalive interval. Declare the peer dead after this many
# intervals of silence.
LIVENESS_INTERVALS = 2.0
LIVENESS_GRACE_SECONDS = 5.0


def liveness_timeout(keepalive_interval: int) -> float:
    return LIVENESS_INTERVALS * keepalive_interval + LIVENESS_GRACE_SECONDS


class RealtimeClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        engine: StateEngine,
        reconciler: Reconciler,
        event_catalog: EventCatalog,
        health_callback: Callable[[], None] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        auth_failed_callback: Callable[[LoginError], None] | None = None,
    ):
        self.host, self.port, self.username, self.password = (
            host,
            port,
            username,
            password,
        )
        self.engine, self.reconciler = engine, reconciler
        self.event_catalog = event_catalog
        self.health_callback = health_callback
        self.event_callback = event_callback
        self.auth_failed_callback = auth_failed_callback
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.transport: DHIPTransport | None = None
        self.transport_lock = threading.RLock()
        self.connected = False
        self.auth_failed = False
        self.generation = 0
        self.connected_since: str | None = None
        self.last_frame_time: str | None = None
        self.last_alarm_event_time: str | None = None
        self.last_disconnect_time: str | None = None
        self.last_error: str | None = None
        self.last_login_error: LoginError | None = None
        self.keepalive_interval = 60
        self.keepalive_request_id: int | None = None
        self.last_keepalive_sent: str | None = None
        self.last_keepalive_reply: str | None = None
        self.reconnect_count = 0
        self.connection_attempts = 0
        self.watchdog_disconnects = 0
        self._watchdog_tripped = False
        self._last_rx_monotonic = time.monotonic()

    def _notify_health(self) -> None:
        if self.health_callback:
            try:
                self.health_callback()
            except Exception:
                _LOGGER.exception("ARC health callback failed")

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name="dahua-arc-realtime", daemon=True
        )
        self.thread.start()

    def _attach(self) -> DHIPTransport:
        transport = DHIPTransport(self.host, self.port, timeout=ATTACH_TIMEOUT_SECONDS)
        try:
            transport.connect()
            login_resp = transport.login(self.username, self.password)
            params = login_resp.get("params") or {}
            self.keepalive_interval = int(params.get("keepAliveInterval", 60) or 60)
            resp, _ = transport.request(
                const.EVENT_ATTACH, {"codes": ["All"]}, extra={}
            )
            if not resp.get("result"):
                raise RuntimeError(f"eventManager.attach failed: {resp}")
            # Backstop for the watchdog: a blocked recv can never outlive the
            # liveness window, even if the keepalive thread dies.
            transport.set_timeout(liveness_timeout(self.keepalive_interval) + 10)
        except Exception:
            transport.close()
            raise
        return transport

    def _set_transport(self, transport: DHIPTransport | None) -> None:
        with self.transport_lock:
            self.transport = transport

    def _close_current_transport(self) -> None:
        with self.transport_lock:
            transport, self.transport = self.transport, None
            if transport is None:
                return
            try:
                transport.shutdown()
            finally:
                with contextlib.suppress(Exception):
                    transport.close()

    def _send_keepalive(self, transport: DHIPTransport) -> None:
        request_id = transport.send_request(
            const.KEEPALIVE,
            {"timeout": self.keepalive_interval, "active": True},
        )
        self.keepalive_request_id = request_id
        self.last_keepalive_sent = timestamp()

    def seconds_since_last_frame(self) -> float:
        return time.monotonic() - self._last_rx_monotonic

    def _keepalive_loop(self, transport: DHIPTransport, stop: threading.Event) -> None:
        interval = self.keepalive_interval
        limit = liveness_timeout(interval)
        while not stop.wait(keepalive_delay(interval)):
            if self.stop_event.is_set():
                return
            silent_for = self.seconds_since_last_frame()
            if silent_for > limit:
                # A powered-off ARC or pulled cable does not reset the TCP
                # connection, so recv() would block and HA would keep showing
                # stale state as available. Force a reconnect instead.
                self.watchdog_disconnects += 1
                self._watchdog_tripped = True
                self.last_error = (
                    f"No DHIP frame for {silent_for:.0f}s "
                    f"(limit {limit:.0f}s); forcing reconnect"
                )
                _LOGGER.warning("ARC realtime stream stalled: %s", self.last_error)
                self._close_current_transport()
                return
            try:
                self._send_keepalive(transport)
            except Exception:
                self._close_current_transport()
                return

    def _read_events_forever(self, transport: DHIPTransport, generation: int) -> None:
        while not self.stop_event.is_set():
            obj, _, _ = transport.recv_frame()
            self._last_rx_monotonic = time.monotonic()
            self.last_frame_time = timestamp()
            if (
                self.keepalive_request_id is not None
                and obj.get("id") == self.keepalive_request_id
                and "result" in obj
            ):
                self.last_keepalive_reply = timestamp()
                continue
            params = obj.get("params") or {}
            events = params.get("eventList")
            if events is None:
                events = [params] if params else []
            for event in events:
                if not event:
                    continue
                self.event_catalog.observe(event)
                if self.event_callback:
                    try:
                        self.event_callback(event)
                    except Exception:
                        _LOGGER.exception("ARC event callback failed")
                if event.get("Code") in (EVENT_CODE, PIRCAM_EVENT_CODE):
                    self.last_alarm_event_time = timestamp()
                # Zone changes notify their own entities through the engine;
                # health listeners only care about connection state changes.
                self.engine.enqueue_event(generation, event)

    def _handle_login_error(self, exc: LoginError) -> None:
        self.connected = False
        self.auth_failed = True
        self.last_login_error = exc
        self.last_disconnect_time = timestamp()
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.ready_event.set()
        self._notify_health()
        _LOGGER.error(
            "ARC login failed; automatic reconnect stopped until the "
            "credentials are updated: %s",
            exc,
        )
        if self.auth_failed_callback is not None:
            try:
                self.auth_failed_callback(exc)
            except Exception:
                _LOGGER.exception("ARC auth-failure callback failed")

    def _run(self) -> None:
        backoff_index, ever_connected = 0, False
        while not self.stop_event.is_set():
            transport = None
            keepalive_stop = None
            keepalive_thread = None
            try:
                self.connection_attempts += 1
                transport = self._attach()
                self._set_transport(transport)
                self._last_rx_monotonic = time.monotonic()
                self.generation += 1
                generation = self.generation
                self.engine.begin_generation(generation)
                self.last_error = None
                self.keepalive_request_id = None
                self.last_keepalive_sent = None
                self.last_keepalive_reply = None
                keepalive_stop = threading.Event()
                keepalive_thread = threading.Thread(
                    target=self._keepalive_loop,
                    args=(transport, keepalive_stop),
                    name="dahua-arc-realtime-keepalive",
                    daemon=True,
                )
                keepalive_thread.start()
                if ever_connected:
                    self.reconnect_count += 1
                source_kind = "reconnect" if ever_connected else "initial"
                self.reconciler.run(
                    f"DHIP generation {generation} attach/resync", source_kind
                )
                self.connected = True
                self.connected_since = timestamp()
                if not self.ready_event.is_set():
                    self.ready_event.set()
                ever_connected = True
                backoff_index = 0
                self._notify_health()
                self._read_events_forever(transport, generation)
            except LoginError as exc:
                self._handle_login_error(exc)
                break
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                was_connected = self.connected
                self.connected = False
                self.last_disconnect_time = timestamp()
                if self._watchdog_tripped:
                    # Keep the watchdog's explanation rather than the
                    # resulting "socket closed" error.
                    self._watchdog_tripped = False
                else:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                if was_connected:
                    self._notify_health()
                _LOGGER.warning("ARC realtime connection lost: %s", self.last_error)
            finally:
                self.connected = False
                if keepalive_stop is not None:
                    keepalive_stop.set()
                if (
                    keepalive_thread is not None
                    and keepalive_thread is not threading.current_thread()
                ):
                    keepalive_thread.join(timeout=2)
                if transport is not None:
                    with contextlib.suppress(Exception):
                        transport.close()
                with self.transport_lock:
                    if self.transport is transport:
                        self.transport = None
            if self.stop_event.is_set():
                break
            delay = RECONNECT_DELAYS[min(backoff_index, len(RECONNECT_DELAYS) - 1)]
            if self.stop_event.wait(delay):
                break
            backoff_index = min(backoff_index + 1, len(RECONNECT_DELAYS) - 1)

    def health(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "auth_failed": self.auth_failed,
            "generation": self.generation,
            "connection_attempts": self.connection_attempts,
            "successful_reconnects": self.reconnect_count,
            "watchdog_disconnects": self.watchdog_disconnects,
            "connected_since": self.connected_since,
            "last_frame_time": self.last_frame_time,
            "last_alarm_event_time": self.last_alarm_event_time,
            "last_disconnect_time": self.last_disconnect_time,
            "last_error": self.last_error,
            "keepalive_interval": self.keepalive_interval,
            "liveness_timeout_seconds": liveness_timeout(self.keepalive_interval),
            "last_keepalive_sent": self.last_keepalive_sent,
            "last_keepalive_reply": self.last_keepalive_reply,
        }

    def stop(self) -> None:
        self.stop_event.set()
        self._close_current_transport()
        if self.thread is not None:
            self.thread.join(timeout=5)
