"""Authoritative ``AlarmRegion.getChannelsState`` snapshots over DHIP."""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any

from ..vendor.dahua import DHIPTransport, const
from ..vendor.dahua.exceptions import LoginError
from .files import extract_download_length, extract_embedded_jpeg, split_json_prefix
from .util import keepalive_delay, safe_int, timestamp

SNAPSHOT_METHOD = "AlarmRegion.getChannelsState"


class SnapshotClient:
    """Own one persistent DHIP session used for snapshots and file downloads."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host, self.port, self.username, self.password = (
            host,
            port,
            username,
            password,
        )
        self.transport: DHIPTransport | None = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.keepalive_thread: threading.Thread | None = None
        self.keepalive_interval = 60
        self.connected = False
        self.connected_since: str | None = None
        self.connection_attempts = 0
        self.successful_connections = 0
        self.last_error: str | None = None
        self.last_snapshot_time: str | None = None
        self.last_snapshot_fragment_count: int | None = None
        self.last_snapshot_bytes: int | None = None
        # Set after an authentication failure so background keepalive and
        # resync attempts cannot lock the ARC account with repeated logins.
        self.auth_failed = False

    def _close_locked(self) -> None:
        transport, self.transport = self.transport, None
        self.connected = False
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    def _connect_locked(self) -> None:
        if self.auth_failed:
            raise LoginError("ARC credentials were rejected; reauthentication required")
        self._close_locked()
        self.connection_attempts += 1
        transport = DHIPTransport(self.host, self.port, timeout=15)
        try:
            transport.connect()
            login_resp = transport.login(self.username, self.password)
        except LoginError:
            self.auth_failed = True
            transport.close()
            raise
        except Exception:
            transport.close()
            raise
        params = login_resp.get("params") or {}
        self.keepalive_interval = int(params.get("keepAliveInterval", 60) or 60)
        self.transport = transport
        self.connected = True
        self.connected_since = timestamp()
        self.successful_connections += 1
        self.last_error = None

    def connect(self) -> None:
        with self.lock:
            self._connect_locked()
        if self.keepalive_thread is None:
            self.keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                name="dahua-arc-snapshot-keepalive",
                daemon=True,
            )
            self.keepalive_thread.start()

    @staticmethod
    def _alarm_state_to_raw(value: str | None) -> int | None:
        return 1 if value == "Alarm" else 5 if value == "Normal" else None

    @classmethod
    def parse_states(cls, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert a getChannelsState reply into array-position records."""
        if not response.get("result"):
            raise RuntimeError(f"{SNAPSHOT_METHOD} failed: {response.get('error')}")
        states = (response.get("params") or {}).get("States")
        if not isinstance(states, list) or not states:
            raise RuntimeError("Snapshot response has no States")
        result = []
        for state in states:
            if not isinstance(state, dict):
                continue
            nindex = safe_int(state.get("Index"))
            if nindex is None or nindex <= 0:
                continue
            sensor_state = state.get("SensorState") or {}
            result.append(
                {
                    "array_pos": nindex - 1,
                    "nIndex": nindex,
                    "online": safe_int(state.get("OnlineState")),
                    "alarm_state": cls._alarm_state_to_raw(state.get("AlarmState")),
                    "tamper": safe_int(sensor_state.get("Tamper")),
                    "low_power": safe_int(sensor_state.get("LowPowerState")),
                }
            )
        return result

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                if self.transport is None:
                    self._connect_locked()
                transport = self.transport
                if transport is None:
                    raise RuntimeError("Snapshot transport unavailable")
                with transport.lock:
                    request_id = transport.send_request(
                        SNAPSHOT_METHOD, {"Condition": {"Type": "AlarmIn"}}
                    )
                    response, fragments, message_bytes = transport.recv_fragmented_json(
                        request_id
                    )
                result = self.parse_states(response)
                self.last_snapshot_time = timestamp()
                self.last_snapshot_fragment_count = fragments
                self.last_snapshot_bytes = message_bytes
                self.last_error = None
                return result
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._close_locked()
                raise

    def download_file(
        self,
        remote_path: str,
        expected_length: int | None = None,
        timeout: float = 20.0,
    ) -> bytes | None:
        """Download a file locally from the ARC over DHIP.

        Dahua FileManager.downloadFile returns one or more DHIP frames. The
        response may contain JSON metadata, raw binary data, or a JSON prefix
        followed by binary data in the same frame. Used by research mode only.
        """
        if not remote_path:
            return None

        with self.lock:
            try:
                if self.transport is None:
                    self._connect_locked()
                transport = self.transport
                if transport is None or transport.sock is None:
                    raise RuntimeError("Snapshot/download transport unavailable")

                old_timeout = transport.sock.gettimeout()
                transport.set_timeout(timeout)
                try:
                    with transport.lock:
                        request_id = transport.send_request(
                            "FileManager.downloadFile",
                            {"fileName": remote_path},
                            extra={"magic": "0x1234"},
                        )
                        return self._read_download(
                            transport, request_id, expected_length
                        )
                finally:
                    with contextlib.suppress(Exception):
                        transport.set_timeout(old_timeout)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                # A failed file transfer can leave the DHIP framing ambiguous.
                # Drop only this dedicated transport so the next snapshot/fetch
                # reconnects cleanly.
                self._close_locked()
                raise

    @staticmethod
    def _read_download(
        transport: DHIPTransport, request_id: int, expected_length: int | None
    ) -> bytes | None:
        buf = bytearray()
        server_length = expected_length

        # Read up to 256 DHIP frames, which is ample for PIRCam JPEGs.
        for _ in range(256):
            _session, response_id, package_len, _idx, _msg_len, _data_len = (
                transport.recv_header()
            )
            payload = transport.recv_exact(package_len) if package_len else b""

            # Ignore unrelated responses, though none are expected on this
            # dedicated snapshot/download connection.
            if response_id not in (0, request_id):
                continue

            # Pure JSON response?
            try:
                obj = json.loads(payload.decode("utf-8")) if payload else {}
            except UnicodeDecodeError, json.JSONDecodeError:
                obj = None

            if isinstance(obj, dict):
                if obj.get("error"):
                    raise RuntimeError(
                        f"FileManager.downloadFile failed: {obj.get('error')}"
                    )
                found = extract_download_length(obj)
                if found:
                    server_length = found
                continue

            # Some Dahua firmware returns JSON metadata and binary in the same
            # DHIP payload.
            prefix, binary = split_json_prefix(payload)
            if prefix is not None:
                if prefix.get("error"):
                    raise RuntimeError(
                        f"FileManager.downloadFile failed: {prefix.get('error')}"
                    )
                found = extract_download_length(prefix)
                if found:
                    server_length = found
                if binary:
                    buf.extend(binary)
            else:
                buf.extend(payload)

            # Prefer an actual complete JPEG over trusting the advertised file
            # length. Some ARC firmware wraps the file bytes in a small binary
            # envelope, so stopping at expected_length can truncate the tail.
            if len(buf) >= 4:
                jpeg, _debug = extract_embedded_jpeg(bytes(buf))
                if jpeg is not None:
                    return jpeg

            # Once we've received at least the advertised file length, allow
            # additional wrapper/tail bytes rather than truncating immediately.
            if server_length is not None and len(buf) >= server_length + 65536:
                return bytes(buf)

        if buf:
            jpeg, _debug = extract_embedded_jpeg(bytes(buf))
            return jpeg if jpeg is not None else bytes(buf)
        return None

    def _keepalive_loop(self) -> None:
        while not self.stop_event.is_set():
            interval = self.keepalive_interval
            if self.stop_event.wait(keepalive_delay(interval)):
                return
            with self.lock:
                transport = self.transport
                if transport is None:
                    continue
                try:
                    response, _ = transport.request(
                        const.KEEPALIVE, {"timeout": interval, "active": True}
                    )
                    if not response.get("result"):
                        raise RuntimeError(f"Snapshot keepalive failed: {response}")
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self._close_locked()

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {
                "connected": self.connected,
                "connected_since": self.connected_since,
                "connection_attempts": self.connection_attempts,
                "successful_connections": self.successful_connections,
                "auth_failed": self.auth_failed,
                "last_error": self.last_error,
                "last_snapshot_time": self.last_snapshot_time,
                "last_snapshot_fragment_count": self.last_snapshot_fragment_count,
                "last_snapshot_bytes": self.last_snapshot_bytes,
            }

    def close(self) -> None:
        self.stop_event.set()
        if self.keepalive_thread is not None:
            self.keepalive_thread.join(timeout=3)
        with self.lock:
            self._close_locked()
