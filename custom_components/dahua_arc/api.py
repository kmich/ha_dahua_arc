"""ARC3800H local read-only state engine.

This module is derived from the hardware-validated standalone v2.4 harness:
CGI discovers the configured Alarm/MultiIO topology; one DHIP connection
provides authoritative AlarmRegion.getChannelsState snapshots and a second
DHIP connection streams AlarmInputSourceSignal events.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    build_opener,
)

from .inventory import (
    PRIMARY_SENSOR_CLASSES,
    EventCatalog,
    InventoryRpcClient,
    RadioDeviceInfo,
    classify_alarm_record,
    extract_radio_devices,
    extract_zone_area_hints,
    record_is_physical,
    redact_sensitive,
    summarize_alarm_records,
    summarize_rpc_inventory,
)
from .vendor.dahua import DHIPTransport, LoginError, const

_LOGGER = logging.getLogger(__name__)
EVENT_CODE = "AlarmInputSourceSignal"
PIRCAM_EVENT_CODE = "AlarmLocal"
PIRCAM_MOTION_HOLD_SECONDS = 5.0
SNAPSHOT_METHOD = "AlarmRegion.getChannelsState"
RECONNECT_DELAYS = (5, 10, 20, 30, 60)


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def raw_to_active(raw: int | None) -> bool | None:
    if raw == 1:
        return True
    if raw == 5:
        return False
    return None


def rpc_response_value(item: dict[str, Any] | None) -> Any:
    """Extract the useful Dahua RPC value from a safe_request result."""
    if not item or not item.get("ok"):
        return None
    response = item.get("response")
    if not isinstance(response, dict):
        return None
    params = response.get("params")
    if isinstance(params, dict):
        for key in (
            "type",
            "Type",
            "deviceType",
            "DeviceType",
            "version",
            "Version",
            "sn",
            "SN",
            "serialNo",
            "SerialNo",
        ):
            if key in params and params[key] not in (None, ""):
                return params[key]
        if len(params) == 1:
            return next(iter(params.values()))
    result = response.get("result")
    if result not in (None, True, False):
        return result
    return None


@dataclass(slots=True)
class Zone:
    index: int
    name: str = ""
    enabled: bool = False
    level1: int | None = None
    level2: int | None = None
    parent_name: str = ""
    sense_method: str = ""
    classification: str = ""
    is_multiio: bool = False
    sensor_type: str = ""
    termination: str = ""
    defence_area_type: str = ""
    area_hint: str = ""
    snapshot_index: int | None = None
    online_state: int | None = None
    raw_alarm_state: int | None = None
    tamper: int | None = None
    low_power: int | None = None
    active: bool | None = None
    parent_sn: str = ""
    event_exchannel: int | None = None
    last_source: str = ""
    last_action: str = ""
    last_changed: str = ""
    last_event_seq: int = 0
    raw_config: dict[str, str] | None = None


ALARM_LINE = re.compile(r"^table\.Alarm\[(\d+)\]\.(.+?)=(.*)$")


def fetch_alarm_config(
    host: str, http_port: int, username: str, password: str
) -> dict[int, dict[str, str]]:
    url = f"http://{host}:{http_port}/cgi-bin/configManager.cgi?action=getConfig&name=Alarm"
    mgr = HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{host}:{http_port}/", username, password)
    opener = build_opener(HTTPDigestAuthHandler(mgr))
    with opener.open(url, timeout=15) as response:
        text = response.read().decode("latin-1")
    if not text.startswith("table.Alarm["):
        raise RuntimeError(f"Unexpected CGI response: {text[:200]!r}")
    records: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        match = ALARM_LINE.match(line.strip())
        if match:
            records.setdefault(int(match.group(1)), {})[match.group(2)] = match.group(3)
    if not records:
        raise RuntimeError("Alarm configuration returned no records")
    return records


def discover_multiio_parents(
    records: dict[int, dict[str, str]],
) -> dict[int, dict[str, Any]]:
    parents: dict[int, dict[str, Any]] = {}
    for idx, cfg in records.items():
        if cfg.get("SenseMethod") != "MultiIOTransmitterP":
            continue
        level1, level2 = safe_int(cfg.get("Level1")), safe_int(cfg.get("Level2"))
        if level1 is not None and level1 >= 0 and level2 == 0:
            parents[level1] = {
                "index": idx,
                "name": cfg.get("Name", f"MultiIO-Level1-{level1}"),
                "config": dict(cfg),
            }
    return parents


def discover_alarm_points(
    records: dict[int, dict[str, str]],
    snapshot: list[dict[str, Any]],
    parents: dict[int, dict[str, Any]],
) -> dict[int, Zone]:
    """Discover real ARC AlarmIn-backed physical records.

    ARC3800H keeps 256 Alarm[] rows, but 202 of them can be unused template
    rows that still say Enable=true. A physical record has a concrete
    SenseMethod/topology address. We track parent/peripheral rows as well as
    sensor inputs so their online/tamper/battery state is available to HA.
    """
    snapshot_positions = {item["array_pos"] for item in snapshot}
    zones: dict[int, Zone] = {}

    for idx, cfg in records.items():
        if not record_is_physical(cfg):
            continue
        if idx not in snapshot_positions:
            continue

        classification = classify_alarm_record(cfg)
        sense_method = cfg.get("SenseMethod", "")
        level1, level2 = safe_int(cfg.get("Level1")), safe_int(cfg.get("Level2"))
        is_multiio_input = classification == "multiio_input"

        parent = parents.get(level1 or -1, {}) if is_multiio_input else {}
        zones[idx] = Zone(
            index=idx,
            name=cfg.get("Name", f"Alarm {idx}"),
            enabled=bool_value(cfg.get("Enable", "false")),
            level1=level1,
            level2=level2,
            parent_name=(
                parent.get("name", f"Level1-{level1}")
                if is_multiio_input
                else cfg.get("Name", "Dahua ARC device")
            ),
            sense_method=sense_method,
            classification=classification,
            is_multiio=is_multiio_input,
            sensor_type=cfg.get("SensorType", ""),
            termination=cfg.get("Termination", ""),
            defence_area_type=cfg.get("DefenceAreaType", ""),
            raw_config=dict(cfg),
        )
    return zones


def discover_multiio_zones(
    records: dict[int, dict[str, str]],
) -> tuple[dict[int, Zone], dict[int, dict[str, Any]]]:
    """Compatibility helper retained for external validation tooling.

    The HA runtime now calls :func:`discover_alarm_points` after acquiring an
    authoritative snapshot so it can safely expose non-MultiIO AlarmIn points.
    """
    parents = discover_multiio_parents(records)
    zones: dict[int, Zone] = {}
    for idx, cfg in records.items():
        if classify_alarm_record(cfg) != "multiio_input":
            continue
        level1, level2 = safe_int(cfg.get("Level1")), safe_int(cfg.get("Level2"))
        parent = parents.get(level1 or -1, {})
        zones[idx] = Zone(
            index=idx,
            name=cfg.get("Name", f"Alarm {idx}"),
            enabled=bool_value(cfg.get("Enable", "false")),
            level1=level1,
            level2=level2,
            parent_name=parent.get("name", f"Level1-{level1}"),
            sense_method=cfg.get("SenseMethod", ""),
            classification="multiio_input",
            is_multiio=True,
            sensor_type=cfg.get("SensorType", ""),
            termination=cfg.get("Termination", ""),
            defence_area_type=cfg.get("DefenceAreaType", ""),
            raw_config=dict(cfg),
        )
    return zones, parents


class SnapshotClient:
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

    def _close_locked(self) -> None:
        transport, self.transport = self.transport, None
        self.connected = False
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _connect_locked(self) -> None:
        self._close_locked()
        self.connection_attempts += 1
        transport = DHIPTransport(self.host, self.port, timeout=15)
        try:
            transport.connect()
            login_resp = transport.login(self.username, self.password)
        except Exception:
            try:
                transport.close()
            except Exception:
                pass
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

    def _recv_fragmented_json_locked(self, transport: DHIPTransport, request_id: int):
        chunks: list[bytes] = []
        expected_len: int | None = None
        expected_index = 0
        for fragment_number in range(64):
            hdr = transport._recv_exact(const.HEADER_SIZE)
            (
                size,
                magic,
                _session,
                response_id,
                package_len,
                package_index,
                message_len,
                data_len,
            ) = struct.unpack(const.HEADER_FMT, hdr)
            if size != const.HEADER_SIZE or magic != const.DHIP_MAGIC:
                raise RuntimeError("Invalid DHIP snapshot header")
            if response_id != request_id:
                raise RuntimeError(
                    f"Snapshot request id mismatch: expected {request_id}, got {response_id}"
                )
            if package_index != expected_index:
                raise RuntimeError(
                    f"Snapshot fragment order mismatch: expected {expected_index}, got {package_index}"
                )
            if data_len:
                raise RuntimeError(
                    "Unexpected binary data in getChannelsState response"
                )
            if expected_len is None:
                expected_len = message_len
            elif expected_len != message_len:
                raise RuntimeError("Snapshot message length changed between fragments")
            chunks.append(transport._recv_exact(package_len))
            raw = b"".join(chunks)
            if expected_len is not None and len(raw) >= expected_len:
                raw = raw[:expected_len]
                return json.loads(raw.decode("utf-8")), fragment_number + 1, len(raw)
            expected_index += 1
        raise RuntimeError("Snapshot response exceeded 64 fragments")

    @staticmethod
    def _alarm_state_to_raw(value: str | None) -> int | None:
        return 1 if value == "Alarm" else 5 if value == "Normal" else None

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                if self.transport is None:
                    self._connect_locked()
                transport = self.transport
                if transport is None:
                    raise RuntimeError("Snapshot transport unavailable")
                with transport._lock:
                    transport._id += 1
                    request_id = transport._id
                    transport._send_frame(
                        {
                            "method": SNAPSHOT_METHOD,
                            "id": request_id,
                            "params": {"Condition": {"Type": "AlarmIn"}},
                            "session": transport.session,
                        }
                    )
                    response, fragments, message_bytes = (
                        self._recv_fragmented_json_locked(transport, request_id)
                    )
                if not response.get("result"):
                    raise RuntimeError(
                        f"{SNAPSHOT_METHOD} failed: {response.get('error')}"
                    )
                states = (response.get("params") or {}).get("States")
                if not isinstance(states, list) or not states:
                    raise RuntimeError("Snapshot response has no States")
                result = []
                for state in states:
                    nindex = safe_int(state.get("Index"))
                    if nindex is None or nindex <= 0:
                        continue
                    sensor_state = state.get("SensorState") or {}
                    result.append(
                        {
                            "array_pos": nindex - 1,
                            "nIndex": nindex,
                            "online": safe_int(state.get("OnlineState")),
                            "alarm_state": self._alarm_state_to_raw(
                                state.get("AlarmState")
                            ),
                            "tamper": safe_int(sensor_state.get("Tamper")),
                            "low_power": safe_int(sensor_state.get("LowPowerState")),
                        }
                    )
                self.last_snapshot_time = timestamp()
                self.last_snapshot_fragment_count = fragments
                self.last_snapshot_bytes = message_bytes
                self.last_error = None
                return result
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._close_locked()
                raise

    @staticmethod
    def _extract_embedded_jpeg(raw: bytes) -> tuple[bytes | None, dict[str, Any]]:
        """Find and extract a complete JPEG embedded in a Dahua file payload."""
        info: dict[str, Any] = {
            "raw_bytes": len(raw),
            "head_hex": raw[:32].hex(),
            "tail_hex": raw[-32:].hex() if raw else "",
        }
        # JPEG SOI normally begins ff d8 ff, but accept ff d8 as well.
        soi = raw.find(b"\xff\xd8\xff")
        if soi < 0:
            soi = raw.find(b"\xff\xd8")
        info["jpeg_soi_offset"] = soi if soi >= 0 else None
        if soi < 0:
            return None, info

        eoi = raw.find(b"\xff\xd9", soi + 2)
        info["jpeg_eoi_offset"] = eoi if eoi >= 0 else None
        if eoi < 0:
            return None, info

        jpeg = raw[soi : eoi + 2]
        info["jpeg_bytes"] = len(jpeg)
        info["prefix_bytes"] = soi
        info["suffix_bytes"] = len(raw) - (eoi + 2)
        return jpeg, info

    @staticmethod
    def _extract_download_length(obj: dict[str, Any]) -> int | None:
        """Extract a file length from common Dahua download response fields."""
        fields = (
            "length",
            "fileLength",
            "fileSize",
            "size",
            "totalLength",
            "totalSize",
            "dataLen",
            "dataSize",
        )

        def scan(value: Any) -> int | None:
            if not isinstance(value, dict):
                return None
            for key in fields:
                raw = value.get(key)
                n = safe_int(raw)
                if n is not None and n > 0:
                    return n
            return None

        for candidate in (obj, obj.get("params"), obj.get("result")):
            length = scan(candidate)
            if length is not None:
                return length
        return None

    @staticmethod
    def _split_json_prefix(payload: bytes) -> tuple[dict[str, Any] | None, bytes]:
        """Split a leading JSON object from a raw DHIP payload if present."""
        stripped = payload.lstrip()
        offset = len(payload) - len(stripped)
        if not stripped.startswith(b"{"):
            return None, payload

        depth = 0
        in_string = False
        escaped = False
        for i, byte in enumerate(stripped):
            ch = chr(byte)
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = offset + i + 1
                    try:
                        obj = json.loads(payload[offset:end].decode("utf-8"))
                    except Exception:
                        return None, payload
                    return obj, payload[end:]
        return None, payload

    def download_file(
        self,
        remote_path: str,
        expected_length: int | None = None,
        timeout: float = 20.0,
    ) -> bytes | None:
        """Download a file locally from the ARC over DHIP.

        Dahua FileManager.downloadFile returns one or more DHIP frames. The
        response may contain JSON metadata, raw binary data, or a JSON prefix
        followed by binary data in the same frame.
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

                sock = transport.sock
                old_timeout = sock.gettimeout()
                sock.settimeout(timeout)
                try:
                    with transport._lock:
                        transport._id += 1
                        request_id = transport._id
                        transport._send_frame(
                            {
                                "method": "FileManager.downloadFile",
                                "magic": "0x1234",
                                "id": request_id,
                                "params": {"fileName": remote_path},
                                "session": transport.session,
                            }
                        )

                        buf = bytearray()
                        server_length = expected_length

                        # Read up to 256 DHIP frames, which is ample for PIRCam JPEGs.
                        for _ in range(256):
                            hdr = transport._recv_exact(const.HEADER_SIZE)
                            (
                                size,
                                magic,
                                _session,
                                response_id,
                                package_len,
                                _package_index,
                                _message_len,
                                _data_len,
                            ) = struct.unpack(const.HEADER_FMT, hdr)

                            if size != const.HEADER_SIZE or magic != const.DHIP_MAGIC:
                                raise RuntimeError("Invalid DHIP file-download header")

                            payload = (
                                transport._recv_exact(package_len)
                                if package_len
                                else b""
                            )

                            # Ignore unrelated responses, though none are expected on
                            # this dedicated snapshot/download connection.
                            if response_id not in (0, request_id):
                                continue

                            # Pure JSON response?
                            try:
                                obj = (
                                    json.loads(payload.decode("utf-8"))
                                    if payload
                                    else {}
                                )
                            except Exception:
                                obj = None

                            if isinstance(obj, dict):
                                if obj.get("error"):
                                    raise RuntimeError(
                                        f"FileManager.downloadFile failed: {obj.get('error')}"
                                    )
                                found = self._extract_download_length(obj)
                                if found:
                                    server_length = found
                                continue

                            # Some Dahua firmware returns JSON metadata and binary in
                            # the same DHIP payload.
                            prefix, binary = self._split_json_prefix(payload)
                            if prefix is not None:
                                if prefix.get("error"):
                                    raise RuntimeError(
                                        f"FileManager.downloadFile failed: {prefix.get('error')}"
                                    )
                                found = self._extract_download_length(prefix)
                                if found:
                                    server_length = found
                                if binary:
                                    buf.extend(binary)
                            else:
                                buf.extend(payload)

                            # Prefer an actual complete JPEG over trusting the
                            # advertised file length. Some ARC firmware wraps the
                            # file bytes in a small binary envelope, so stopping
                            # at expected_length can truncate the JPEG tail.
                            if len(buf) >= 4:
                                jpeg, _debug = self._extract_embedded_jpeg(bytes(buf))
                                if jpeg is not None:
                                    return jpeg

                            # Once we've received at least the advertised file
                            # length, allow additional wrapper/tail bytes rather
                            # than truncating immediately. A complete JPEG above
                            # will return as soon as its EOI marker arrives.
                            if (
                                server_length is not None
                                and len(buf) >= server_length + 65536
                            ):
                                return bytes(buf)

                        if buf:
                            jpeg, _debug = self._extract_embedded_jpeg(bytes(buf))
                            return jpeg if jpeg is not None else bytes(buf)
                        return None
                finally:
                    try:
                        sock.settimeout(old_timeout)
                    except Exception:
                        pass
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                # A failed file transfer can leave the DHIP framing ambiguous.
                # Drop only this dedicated transport so the next snapshot/fetch
                # reconnects cleanly.
                self._close_locked()
                raise

    def _keepalive_loop(self) -> None:
        while not self.stop_event.is_set():
            interval = self.keepalive_interval
            delay = interval - 2 if interval > 5 else max(1, interval)
            if self.stop_event.wait(delay):
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


class StateEngine:
    def __init__(
        self,
        zones: dict[int, Zone],
        change_callback: Callable[[set[int]], None] | None = None,
    ):
        self.zones = zones
        self.change_callback = change_callback
        self.lock = threading.RLock()
        self.event_queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
        self.stop_event = threading.Event()
        self.current_generation = 0
        self.processor_thread: threading.Thread | None = None
        self.last_snapshot_time: str | None = None
        self.last_snapshot_reason: str | None = None
        self.snapshot_count = 0
        self.event_sequence = 0
        self.realtime_events_received = 0
        self.realtime_state_changes = 0
        self.duplicate_events = 0
        self.stale_generation_events = 0
        self.snapshot_corrections = 0
        self.reconnect_corrections = 0
        self.periodic_corrections = 0
        self.stale_snapshot_rejects = 0
        self.wpan_motion_updates = 0
        self.wpan_motion_detections = 0
        # ARD1731 PIRCam emits AlarmLocal Start/Stop almost back-to-back.
        # Hold the HA motion state briefly so the pulse is observable and
        # automations cannot lose it due to event-loop coalescing.
        self._pircam_off_timers: dict[int, threading.Timer] = {}

    def begin_generation(self, generation: int) -> None:
        with self.lock:
            self.current_generation = generation

    def enqueue_event(self, generation: int, event: dict[str, Any]) -> None:
        self.event_queue.put((generation, event))

    def snapshot_watermark(self) -> int:
        with self.lock:
            return self.event_sequence

    def apply_snapshot(
        self,
        snapshot: list[dict[str, Any]],
        source: str,
        source_kind: str,
        event_watermark: int,
    ):
        changed: list[tuple[Zone, bool | None, bool | None]] = []
        skipped: list[Zone] = []
        changed_indices: set[int] = set()
        by_pos = {item["array_pos"]: item for item in snapshot}
        with self.lock:
            for idx, zone in self.zones.items():
                item = by_pos.get(idx)
                if item is None:
                    continue
                zone.snapshot_index = item["nIndex"]
                zone.online_state = item["online"]
                zone.tamper = item.get("tamper")
                zone.low_power = item.get("low_power")
                if zone.last_event_seq > event_watermark:
                    self.stale_snapshot_rejects += 1
                    skipped.append(zone)
                    continue
                old_active = zone.active
                new_active = raw_to_active(item["alarm_state"])
                zone.raw_alarm_state = item["alarm_state"]
                zone.active = new_active
                zone.last_source = source
                if old_active != new_active:
                    if old_active is not None:
                        changed.append((zone, old_active, new_active))
                    zone.last_changed = timestamp()
                    changed_indices.add(idx)
            self.last_snapshot_time = timestamp()
            self.last_snapshot_reason = source
            self.snapshot_count += 1
            corrections = len(changed)
            self.snapshot_corrections += corrections
            if source_kind == "reconnect":
                self.reconnect_corrections += corrections
            elif source_kind == "periodic":
                self.periodic_corrections += corrections
        if changed_indices and self.change_callback:
            self.change_callback(changed_indices)
        return changed, skipped

    def validate_index_mapping(
        self, snapshot: list[dict[str, Any]]
    ) -> tuple[int, list[tuple]]:
        by_pos = {item["array_pos"]: item for item in snapshot}
        mismatches, checked = [], 0
        for idx in sorted(self.zones):
            item = by_pos.get(idx)
            if item is None:
                mismatches.append((idx, None, "snapshot position missing"))
                continue
            checked += 1
            if item["nIndex"] != idx + 1:
                mismatches.append((idx, item["nIndex"], idx + 1))
        return checked, mismatches

    def start(self) -> None:
        self.processor_thread = threading.Thread(
            target=self._process_events, name="dahua-arc-events", daemon=True
        )
        self.processor_thread.start()

    def _process_events(self) -> None:
        while not self.stop_event.is_set():
            try:
                generation, event = self.event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.lock:
                if generation != self.current_generation:
                    self.stale_generation_events += 1
                    continue
            try:
                self._apply_event(event)
            except Exception:
                _LOGGER.exception("Failed processing ARC event")

    def _apply_event(self, event: dict[str, Any]) -> None:
        code = str(event.get("Code") or "")
        if code not in (EVENT_CODE, PIRCAM_EVENT_CODE):
            return

        idx, action = safe_int(event.get("Index")), event.get("Action")
        if idx is None or action not in ("Start", "Stop"):
            return

        data = event.get("Data") or {}
        if not isinstance(data, dict):
            data = {}

        # ARD1731-W2 reports actual armed intrusion/motion as AlarmLocal,
        # not AlarmInputSourceSignal. Do not generalize AlarmLocal to every
        # ARC event type until those devices are separately validated.
        if code == PIRCAM_EVENT_CODE:
            if str(data.get("DevType") or data.get("SenseMethod") or "") != "PIRCam":
                return

        changed_indices: set[int] = set()

        # PIRCam Stop usually follows Start within the same second. If we
        # immediately apply both, Home Assistant can observe only the final
        # Clear state. Record the Stop event now but defer the actual OFF
        # transition for a short, deterministic motion pulse.
        if code == PIRCAM_EVENT_CODE and action == "Stop":
            with self.lock:
                self.realtime_events_received += 1
                zone = self.zones.get(idx)
                if zone is None:
                    return
                self.event_sequence += 1
                zone.last_event_seq = self.event_sequence
                zone.last_source = "DHIP AlarmLocal realtime"
                zone.last_action = action
                if data.get("SN"):
                    zone.parent_sn = str(data["SN"])

                old_timer = self._pircam_off_timers.pop(idx, None)
                if old_timer is not None:
                    old_timer.cancel()

                timer = threading.Timer(
                    PIRCAM_MOTION_HOLD_SECONDS,
                    self._finish_pircam_motion,
                    args=(idx, self.event_sequence),
                )
                timer.daemon = True
                self._pircam_off_timers[idx] = timer
                timer.start()
            return

        new_active = action == "Start"
        with self.lock:
            self.realtime_events_received += 1
            zone = self.zones.get(idx)
            if zone is None:
                return

            if code == PIRCAM_EVENT_CODE:
                old_timer = self._pircam_off_timers.pop(idx, None)
                if old_timer is not None:
                    old_timer.cancel()

            old_active = zone.active
            self.event_sequence += 1
            zone.last_event_seq = self.event_sequence
            zone.active = new_active
            zone.raw_alarm_state = 1 if new_active else 5
            zone.last_source = (
                "DHIP AlarmLocal realtime"
                if code == PIRCAM_EVENT_CODE
                else "DHIP realtime"
            )
            zone.last_action = action
            zone.last_changed = timestamp()
            if data.get("SN"):
                zone.parent_sn = str(data["SN"])
            exchannel = safe_int(data.get("ExChannel"))
            if exchannel is not None:
                zone.event_exchannel = exchannel
            if old_active != new_active:
                self.realtime_state_changes += 1
                changed_indices.add(idx)
            else:
                self.duplicate_events += 1

        if changed_indices and self.change_callback:
            self.change_callback(changed_indices)

    def apply_wpan_motion(self, idx: int, active: bool, source: str) -> None:
        """Apply a validated LowRateWPAN PIR-camera alarm-state transition.

        Research builds use this only when getAccessoryStatus reports a
        non-zero AlarmState for a configured PIRCam. FindMe is deliberately
        ignored because live diagnostics show it follows the radio heartbeat,
        not detector motion.
        """
        changed = False
        with self.lock:
            zone = self.zones.get(idx)
            if zone is None or zone.sense_method != "PIRCam":
                return
            old_active = zone.active
            self.event_sequence += 1
            zone.last_event_seq = self.event_sequence
            zone.active = bool(active)
            zone.raw_alarm_state = 1 if active else 5
            zone.last_source = source
            zone.last_action = "WPAN-Start" if active else "WPAN-Stop"
            if old_active != zone.active:
                zone.last_changed = timestamp()
                self.wpan_motion_updates += 1
                if active:
                    self.wpan_motion_detections += 1
                changed = True
        if changed and self.change_callback:
            self.change_callback({idx})

    def _finish_pircam_motion(self, idx: int, stop_event_seq: int) -> None:
        """Finish a short ARD1731 motion pulse after its immediate Stop."""
        changed = False
        with self.lock:
            timer = self._pircam_off_timers.get(idx)
            if timer is None:
                return
            self._pircam_off_timers.pop(idx, None)

            zone = self.zones.get(idx)
            if zone is None:
                return

            # A newer event supersedes this delayed Stop.
            if zone.last_event_seq != stop_event_seq or zone.last_action != "Stop":
                return

            if zone.active is not False:
                zone.active = False
                zone.raw_alarm_state = 5
                zone.last_source = (
                    f"DHIP AlarmLocal realtime "
                    f"({PIRCAM_MOTION_HOLD_SECONDS:g}s motion hold)"
                )
                zone.last_changed = timestamp()
                self.realtime_state_changes += 1
                changed = True

        if changed and self.change_callback:
            self.change_callback({idx})

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            return {
                "current_generation": self.current_generation,
                "queued_events": self.event_queue.qsize(),
                "snapshot_count": self.snapshot_count,
                "last_snapshot_time": self.last_snapshot_time,
                "last_snapshot_reason": self.last_snapshot_reason,
                "event_sequence": self.event_sequence,
                "realtime_events_received": self.realtime_events_received,
                "realtime_state_changes": self.realtime_state_changes,
                "duplicate_events": self.duplicate_events,
                "stale_generation_events": self.stale_generation_events,
                "snapshot_corrections": self.snapshot_corrections,
                "reconnect_corrections": self.reconnect_corrections,
                "periodic_corrections": self.periodic_corrections,
                "stale_snapshot_rejects": self.stale_snapshot_rejects,
                "wpan_motion_updates": self.wpan_motion_updates,
                "wpan_motion_detections": self.wpan_motion_detections,
            }

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            timers = list(self._pircam_off_timers.values())
            self._pircam_off_timers.clear()
        for timer in timers:
            timer.cancel()
        if self.processor_thread is not None:
            self.processor_thread.join(timeout=2)


class Reconciler:
    def __init__(self, snapshot_client: SnapshotClient, engine: StateEngine):
        self.snapshot_client, self.engine = snapshot_client, engine
        self.lock = threading.RLock()

    def run(
        self, reason: str, source_kind: str
    ) -> list[tuple[Zone, bool | None, bool | None]]:
        with self.lock:
            watermark = self.engine.snapshot_watermark()
            snapshot = self.snapshot_client.snapshot()
            checked, mismatches = self.engine.validate_index_mapping(snapshot)
            if mismatches:
                raise RuntimeError(
                    f"ARC snapshot index mapping failed: {mismatches[:5]}"
                )
            if checked != len(self.engine.zones):
                raise RuntimeError(
                    f"Snapshot mapped {checked}/{len(self.engine.zones)} configured zones"
                )
            changed, skipped = self.engine.apply_snapshot(
                snapshot, reason, source_kind, watermark
            )
        if skipped:
            _LOGGER.debug("Rejected %d stale snapshot values", len(skipped))
        if changed:
            _LOGGER.info("%s corrected %d ARC zone state(s)", source_kind, len(changed))
        return changed


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
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.transport: DHIPTransport | None = None
        self.transport_lock = threading.RLock()
        self.connected = False
        self.generation = 0
        self.connected_since: str | None = None
        self.last_frame_time: str | None = None
        self.last_alarm_event_time: str | None = None
        self.last_disconnect_time: str | None = None
        self.last_error: str | None = None
        self.keepalive_interval = 60
        self.keepalive_request_id: int | None = None
        self.last_keepalive_sent: str | None = None
        self.last_keepalive_reply: str | None = None
        self.reconnect_count = 0
        self.connection_attempts = 0

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
        transport = DHIPTransport(self.host, self.port, timeout=3600)
        transport.connect()
        login_resp = transport.login(self.username, self.password)
        params = login_resp.get("params") or {}
        self.keepalive_interval = int(params.get("keepAliveInterval", 60) or 60)
        resp, _ = transport.request(const.EVENT_ATTACH, {"codes": ["All"]}, extra={})
        if not resp.get("result"):
            raise RuntimeError(f"eventManager.attach failed: {resp}")
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
                if transport.sock is not None:
                    try:
                        transport.sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
            finally:
                try:
                    transport.close()
                except Exception:
                    pass

    def _send_keepalive(self, transport: DHIPTransport) -> None:
        with transport._lock:
            transport._id += 1
            request_id = transport._id
            transport._send_frame(
                {
                    "method": const.KEEPALIVE,
                    "id": request_id,
                    "params": {"timeout": self.keepalive_interval, "active": True},
                    "session": transport.session,
                }
            )
        self.keepalive_request_id = request_id
        self.last_keepalive_sent = timestamp()

    def _keepalive_loop(self, transport: DHIPTransport, stop: threading.Event) -> None:
        delay = (
            self.keepalive_interval - 2
            if self.keepalive_interval > 5
            else max(1, self.keepalive_interval)
        )
        while not stop.wait(delay):
            if self.stop_event.is_set():
                return
            try:
                self._send_keepalive(transport)
            except Exception:
                self._close_current_transport()
                return

    def _read_events_forever(self, transport: DHIPTransport, generation: int) -> None:
        while not self.stop_event.is_set():
            obj, _, _ = transport.recv_frame()
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
                self.engine.enqueue_event(generation, event)
                self._notify_health()

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
                keepalive_stop = threading.Event()
                keepalive_thread = threading.Thread(
                    target=self._keepalive_loop,
                    args=(transport, keepalive_stop),
                    name="dahua-arc-realtime-keepalive",
                    daemon=True,
                )
                keepalive_thread.start()
                self.generation += 1
                generation = self.generation
                self.engine.begin_generation(generation)
                self.last_error = None
                self.keepalive_request_id = None
                self.last_keepalive_sent = None
                self.last_keepalive_reply = None
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
                self.connected = False
                self.last_disconnect_time = timestamp()
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.ready_event.set()
                self._notify_health()
                _LOGGER.error("ARC login failed; automatic reconnect stopped: %s", exc)
                break
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self.connected = False
                self.last_disconnect_time = timestamp()
                self.last_error = f"{type(exc).__name__}: {exc}"
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
                    try:
                        transport.close()
                    except Exception:
                        pass
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
            "generation": self.generation,
            "connection_attempts": self.connection_attempts,
            "successful_reconnects": self.reconnect_count,
            "connected_since": self.connected_since,
            "last_frame_time": self.last_frame_time,
            "last_alarm_event_time": self.last_alarm_event_time,
            "last_disconnect_time": self.last_disconnect_time,
            "last_error": self.last_error,
            "keepalive_interval": self.keepalive_interval,
            "last_keepalive_sent": self.last_keepalive_sent,
            "last_keepalive_reply": self.last_keepalive_reply,
        }

    def stop(self) -> None:
        self.stop_event.set()
        self._close_current_transport()
        if self.thread is not None:
            self.thread.join(timeout=5)


class WPANResearchPoller:
    """Read-only LowRateWPAN sampler for Detector Test reverse engineering.

    Detector Test activity does not appear on eventManager.attach(["All"]) on
    the tested ARC3800H firmware. This temporary research poller discovers a
    working parameter shape for read-only WPAN status methods, then samples
    them at low frequency and records field-level changes for diagnostics.
    """

    METHODS = (
        "LowRateWPAN.getAccessoryStatus",
        "LowRateWPAN.getAccessoryInfo",
        "LowRateWPAN.getWirelessDevStatus",
    )
    POLL_SECONDS = 1.0

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        zones: dict[int, Zone],
        radio_devices: dict[int, RadioDeviceInfo],
        engine: StateEngine | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.zones = {
            idx: zone
            for idx, zone in zones.items()
            if not zone.is_multiio
            and zone.classification == "motion"
            and zone.level1 is not None
        }
        self.radio_devices = radio_devices
        self.engine = engine
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.RLock()
        self.query_styles: dict[str, str] = {}
        self.discovery_exhausted: set[str] = set()
        self.discovery_attempts: list[dict[str, Any]] = []
        self.attach_probe_results: list[dict[str, Any]] = []
        self.attach_probe_complete = False
        self.last_pircam_alarm_state: dict[int, int] = {}
        self.last_responses: dict[str, Any] = {}
        self.last_flat: dict[str, dict[str, Any]] = {}
        self.changes: list[dict[str, Any]] = []
        self.poll_count = 0
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.connected = False

        # v0.4.10: persistent LowRateWPAN subscriptions. v0.4.9 proved both
        # LowRateWPAN.attachAccessoryInfo({}) and LowRateWPAN.attach({})
        # return a valid SID, but that research build closed the socket
        # immediately and therefore could never receive their notifications.
        self.subscription_methods = (
            "LowRateWPAN.attachAccessoryInfo",
            "LowRateWPAN.attach",
        )
        self.subscription_threads: dict[str, threading.Thread] = {}
        self.subscription_transports: dict[str, DHIPTransport] = {}
        self.subscription_status: dict[str, dict[str, Any]] = {
            method: {
                "connected": False,
                "sid": None,
                "connected_at": None,
                "last_frame_at": None,
                "frames_received": 0,
                "last_error": None,
                "keepalive_interval": None,
                "last_keepalive_sent": None,
                "last_keepalive_reply": None,
                "keepalive_request_id": None,
            }
            for method in self.subscription_methods
        }
        self.subscription_recent_frames: list[dict[str, Any]] = []
        self.subscription_motion_candidates: list[dict[str, Any]] = []

    @staticmethod
    def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        volatile = {
            "id",
            "session",
            "UTC",
            "RealUTC",
            "LocaleTime",
            "timestamp",
            "time",
            "Time",
        }

        def walk(item: Any, path: str) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in volatile:
                        continue
                    child_path = f"{path}.{key}" if path else str(key)
                    walk(child, child_path)
                return
            if isinstance(item, list):
                # Lists can be large. Keep scalar members and recurse into a
                # bounded number of structured entries.
                for i, child in enumerate(item[:32]):
                    child_path = f"{path}[{i}]"
                    walk(child, child_path)
                return
            out[path] = item

        walk(value, prefix)
        return out

    def _params(self, style: str, zone: Zone) -> dict[str, Any]:
        addr = int(zone.level1 or 0)
        idx = int(zone.index)
        radio = self.radio_devices.get(addr)
        serial = radio.serial if radio is not None else ""

        if style == "empty":
            return {}
        if style == "ShortAddr":
            return {"ShortAddr": addr}
        if style == "shortAddr":
            return {"shortAddr": addr}
        if style == "nShortAddr":
            return {"nShortAddr": addr}
        if style == "Address":
            return {"Address": addr}
        if style == "Addr":
            return {"Addr": addr}
        if style == "IndexAlarm":
            return {"Index": idx}
        if style == "IndexZeroBased":
            return {"Index": max(0, addr - 1)}
        if style == "ChannelZeroBased":
            return {"Channel": max(0, addr - 1)}
        if style == "ConditionShortAddr":
            return {"Condition": {"ShortAddr": addr}}
        if style == "ConditionNShortAddr":
            return {"Condition": {"nShortAddr": addr}}
        if style == "AccessoryShortAddr":
            return {"Accessory": {"ShortAddr": addr}}
        if style == "InfoNShortAddr":
            return {"Info": {"nShortAddr": addr}}
        if style == "SN" and serial:
            return {"SN": serial}
        return {}

    def _candidate_styles(self, zone: Zone) -> list[str]:
        styles = [
            "empty",
            "ShortAddr",
            "shortAddr",
            "nShortAddr",
            "Address",
            "Addr",
            "IndexAlarm",
            "IndexZeroBased",
            "ChannelZeroBased",
            "ConditionShortAddr",
            "ConditionNShortAddr",
            "AccessoryShortAddr",
            "InfoNShortAddr",
        ]
        radio = self.radio_devices.get(int(zone.level1 or 0))
        if radio is not None and radio.serial:
            styles.append("SN")
        return styles

    @staticmethod
    def _error_summary(result: dict[str, Any]) -> str:
        response = result.get("response") or {}
        error = response.get("error") or {}
        if error:
            return f"{error.get('code')}: {error.get('message') or ''}".strip()
        return str(result.get("error") or "failed")

    def _discover(self, client: InventoryRpcClient) -> None:
        if not self.zones:
            return
        target = next(iter(self.zones.values()))
        for method in self.METHODS:
            if method in self.query_styles or method in self.discovery_exhausted:
                continue
            found = False
            for style in self._candidate_styles(target):
                result = client.safe_request(method, self._params(style, target))
                with self.lock:
                    self.discovery_attempts.append(
                        {
                            "at": timestamp(),
                            "method": method,
                            "style": style,
                            "ok": bool(result.get("ok")),
                            "error": None
                            if result.get("ok")
                            else self._error_summary(result),
                        }
                    )
                    self.discovery_attempts = self.discovery_attempts[-120:]
                if result.get("ok"):
                    with self.lock:
                        self.query_styles[method] = style
                    self._capture(method, target.index, result)
                    found = True
                    break
            if not found:
                with self.lock:
                    self.discovery_exhausted.add(method)

    def _probe_attach_methods(self) -> None:
        """Probe subscription entry points without leaving a subscription open.

        These calls are attach/read-subscription operations only. Each probe
        uses a fresh DHIP session and closes it immediately after the reply.
        """
        if self.attach_probe_complete or not self.zones:
            return
        target = next(
            (z for z in self.zones.values() if z.sense_method == "PIRCam"),
            next(iter(self.zones.values())),
        )
        addr = int(target.level1 or 0)
        radio = self.radio_devices.get(addr)
        serial = radio.serial if radio is not None else ""

        candidates = [
            ("empty", {}),
            ("codes_all", {"codes": ["All"]}),
            ("ShortAddr", {"ShortAddr": addr}),
            ("nShortAddr", {"nShortAddr": addr}),
            ("Index", {"Index": int(target.index)}),
        ]
        if serial:
            candidates.append(("SN", {"SN": serial}))

        methods = (
            "LowRateWPAN.attachAccessoryInfo",
            "LowRateWPAN.attach",
            "RemoteLowRateWPAN.attach",
        )

        for method in methods:
            for style, params in candidates:
                client = InventoryRpcClient(
                    self.host, self.port, self.username, self.password
                )
                try:
                    client.connect()
                    result = client.safe_request(method, params)
                    item = {
                        "at": timestamp(),
                        "method": method,
                        "style": style,
                        "ok": bool(result.get("ok")),
                        "response": (
                            redact_sensitive(result.get("response") or {})
                            if result.get("ok")
                            else None
                        ),
                        "error": (
                            None if result.get("ok") else self._error_summary(result)
                        ),
                    }
                    with self.lock:
                        self.attach_probe_results.append(item)
                    if result.get("ok"):
                        break
                except Exception as exc:
                    with self.lock:
                        self.attach_probe_results.append(
                            {
                                "at": timestamp(),
                                "method": method,
                                "style": style,
                                "ok": False,
                                "response": None,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
        self.attach_probe_complete = True

    def _apply_pircam_status(self, response: dict[str, Any]) -> None:
        """Map aggregate LowRateWPAN status to configured PIRCam zones."""
        params = response.get("params") or {}
        statuses = params.get("Status") if isinstance(params, dict) else None
        if not isinstance(statuses, list):
            return

        # Camera names are exact on the tested ARC and are safer than assuming
        # list position if device enrollment order ever changes.
        by_name = {
            str(item.get("Name") or ""): item
            for item in statuses
            if isinstance(item, dict)
        }

        for idx, zone in self.zones.items():
            if zone.sense_method != "PIRCam":
                continue
            item = by_name.get(zone.name)
            if not isinstance(item, dict):
                # Fallback: ARC3800H currently orders Status by short address.
                pos = int(zone.level1 or 0) - 1
                if 0 <= pos < len(statuses) and isinstance(statuses[pos], dict):
                    item = statuses[pos]
                else:
                    continue

            alarm_state = safe_int(item.get("AlarmState"), 0) or 0
            previous = self.last_pircam_alarm_state.get(idx)
            self.last_pircam_alarm_state[idx] = alarm_state

            # Public Dahua accessory semantics use zero for normal and
            # non-zero for an active alarm condition. Only transition the HA
            # PIRCam entity when this field itself changes; FindMe is ignored.
            if previous is not None and previous != alarm_state and self.engine:
                self.engine.apply_wpan_motion(
                    idx,
                    alarm_state != 0,
                    "LowRateWPAN.getAccessoryStatus research",
                )

    def _capture(
        self,
        method: str,
        zone_index: int | str,
        result: dict[str, Any],
    ) -> None:
        if not result.get("ok"):
            return
        raw_response = result.get("response") or {}
        if method == "LowRateWPAN.getAccessoryStatus":
            self._apply_pircam_status(raw_response)
        response = redact_sensitive(raw_response)
        key = f"{method}|{zone_index}"
        flat = self._flatten(response)
        with self.lock:
            previous = self.last_flat.get(key)
            if previous is not None:
                changed: dict[str, dict[str, Any]] = {}
                for path in sorted(set(previous) | set(flat)):
                    before = previous.get(path)
                    after = flat.get(path)
                    if before != after:
                        changed[path] = {"before": before, "after": after}
                        if len(changed) >= 80:
                            break
                if changed:
                    self.changes.append(
                        {
                            "at": timestamp(),
                            "method": method,
                            "zone_index": zone_index,
                            "changes": changed,
                        }
                    )
                    self.changes = self.changes[-300:]
            self.last_flat[key] = flat
            self.last_responses[key] = response
            # Bound diagnostics if a method returns a large aggregate table.
            if len(self.last_responses) > 16:
                oldest = next(iter(self.last_responses))
                self.last_responses.pop(oldest, None)
                self.last_flat.pop(oldest, None)

    def _poll_once(self, client: InventoryRpcClient) -> None:
        self._discover(client)
        with self.lock:
            query_styles = dict(self.query_styles)

        for method, style in query_styles.items():
            # If no parameters are needed, a single aggregate call is enough.
            if style == "empty":
                result = client.safe_request(method, {})
                if result.get("ok"):
                    self._capture(method, "aggregate", result)
                continue

            for idx, zone in self.zones.items():
                result = client.safe_request(method, self._params(style, zone))
                if result.get("ok"):
                    self._capture(method, idx, result)

        with self.lock:
            self.poll_count += 1
            self.last_poll_at = timestamp()

    @staticmethod
    def _iter_dict_nodes(value: Any, path: str = ""):
        """Yield every dictionary node and its path from an RPC notification."""
        if isinstance(value, dict):
            yield path, value
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield from WPANResearchPoller._iter_dict_nodes(child, child_path)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                child_path = f"{path}[{i}]"
                yield from WPANResearchPoller._iter_dict_nodes(child, child_path)

    @staticmethod
    def _parse_alarm_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return int(value) != 0
        text = str(value or "").strip().casefold()
        if text in {"1", "true", "on", "start", "active", "alarm", "alarming"}:
            return True
        if text in {"0", "false", "off", "stop", "normal", "clear", "idle"}:
            return False
        return None

    def _analyze_subscription_motion(
        self,
        subscription_method: str,
        frame: dict[str, Any],
    ) -> None:
        """Find strongly identified PIRCam alarm-state notifications.

        We only drive HA when the frame identifies a configured PIR camera by
        exact name, serial number, or wireless short address AND contains a
        clear AlarmState/AlarmStatus or Start/Stop signal. Generic State fields
        and FindMe are intentionally ignored.
        """
        nodes = list(self._iter_dict_nodes(frame))
        if not nodes:
            return

        for idx, zone in self.zones.items():
            if zone.sense_method != "PIRCam":
                continue

            addr = int(zone.level1 or 0)
            radio = self.radio_devices.get(addr)
            serial = str(radio.serial or "") if radio is not None else ""

            matched_path: str | None = None
            matched_by: str | None = None
            matched_node: dict[str, Any] | None = None

            for path, node in nodes:
                for key, value in node.items():
                    k = str(key).casefold()
                    if (
                        k == "name"
                        and zone.name
                        and str(value).strip().casefold()
                        == zone.name.strip().casefold()
                    ):
                        matched_path, matched_by, matched_node = path, "Name", node
                        break
                    if (
                        k in {"sn", "serial", "serialnumber"}
                        and serial
                        and str(value) == serial
                    ):
                        matched_path, matched_by, matched_node = path, "SN", node
                        break
                    if (
                        k in {"shortaddr", "shotaddr", "nshortaddr"}
                        and safe_int(value) == addr
                    ):
                        matched_path, matched_by, matched_node = path, key, node
                        break
                if matched_node is not None:
                    break

            if matched_node is None:
                continue

            signal_path: str | None = None
            signal_name: str | None = None
            signal_value: Any = None
            active: bool | None = None

            # Prefer a signal in the same accessory object.
            for key, value in matched_node.items():
                k = str(key).casefold()
                if k in {"alarmstate", "alarmstatus"}:
                    parsed = self._parse_alarm_bool(value)
                    if parsed is not None:
                        signal_path = (
                            f"{matched_path}.{key}" if matched_path else str(key)
                        )
                        signal_name, signal_value, active = str(key), value, parsed
                        break
                if k == "action":
                    parsed = self._parse_alarm_bool(value)
                    if parsed is not None and str(value).strip().casefold() in {
                        "start",
                        "stop",
                    }:
                        signal_path = (
                            f"{matched_path}.{key}" if matched_path else str(key)
                        )
                        signal_name, signal_value, active = str(key), value, parsed
                        break

            # Some Dahua notifications put identity and signal in sibling
            # dictionaries. Search the complete frame only for explicit alarm
            # fields, never for generic State.
            if active is None:
                for path, node in nodes:
                    for key, value in node.items():
                        k = str(key).casefold()
                        if k in {"alarmstate", "alarmstatus"}:
                            parsed = self._parse_alarm_bool(value)
                            if parsed is not None:
                                signal_path = f"{path}.{key}" if path else str(key)
                                signal_name, signal_value, active = (
                                    str(key),
                                    value,
                                    parsed,
                                )
                                break
                        if k == "action":
                            v = str(value).strip().casefold()
                            if v in {"start", "stop"}:
                                signal_path = f"{path}.{key}" if path else str(key)
                                signal_name, signal_value, active = (
                                    str(key),
                                    value,
                                    v == "start",
                                )
                                break
                    if active is not None:
                        break

            candidate = {
                "at": timestamp(),
                "subscription": subscription_method,
                "zone_index": idx,
                "zone_name": zone.name,
                "matched_by": matched_by,
                "matched_path": matched_path,
                "signal_path": signal_path,
                "signal_name": signal_name,
                "signal_value": signal_value,
                "active": active,
            }
            with self.lock:
                self.subscription_motion_candidates.append(redact_sensitive(candidate))
                self.subscription_motion_candidates = (
                    self.subscription_motion_candidates[-200:]
                )

            if active is not None and self.engine is not None:
                self.engine.apply_wpan_motion(
                    idx,
                    active,
                    f"{subscription_method} notification",
                )

    def _record_subscription_frame(
        self,
        method: str,
        obj: dict[str, Any],
        data: bytes,
        meta: dict[str, Any],
    ) -> None:
        now = timestamp()
        self._analyze_subscription_motion(method, obj)

        record = {
            "at": now,
            "subscription": method,
            "frame": redact_sensitive(obj),
            "binary_length": len(data or b""),
            "meta": {
                "request_id": meta.get("request_id"),
                "package_index": meta.get("package_index"),
                "data_length": meta.get("data_length"),
            },
        }
        with self.lock:
            status = self.subscription_status[method]
            status["last_frame_at"] = now
            status["frames_received"] = int(status.get("frames_received") or 0) + 1
            self.subscription_recent_frames.append(record)
            self.subscription_recent_frames = self.subscription_recent_frames[-200:]

    def _subscription_keepalive_loop(
        self,
        method: str,
        transport: DHIPTransport,
        keepalive_interval: int,
        stop: threading.Event,
    ) -> None:
        delay = (
            keepalive_interval - 2
            if keepalive_interval > 5
            else max(1, keepalive_interval)
        )
        while not stop.wait(delay):
            if self.stop_event.is_set():
                return
            try:
                with transport._lock:
                    transport._id += 1
                    request_id = transport._id
                    transport._send_frame(
                        {
                            "method": const.KEEPALIVE,
                            "id": request_id,
                            "params": {
                                "timeout": keepalive_interval,
                                "active": True,
                            },
                            "session": transport.session,
                        }
                    )
                with self.lock:
                    status = self.subscription_status[method]
                    status["keepalive_request_id"] = request_id
                    status["last_keepalive_sent"] = timestamp()
            except Exception as exc:
                with self.lock:
                    self.subscription_status[method]["last_error"] = (
                        f"keepalive {type(exc).__name__}: {exc}"
                    )
                try:
                    transport.close()
                except Exception:
                    pass
                return

    def _subscription_run(self, method: str) -> None:
        """Keep one successful LowRateWPAN attach socket open and capture pushes."""
        while not self.stop_event.is_set():
            transport: DHIPTransport | None = None
            keepalive_stop = threading.Event()
            keepalive_thread: threading.Thread | None = None
            try:
                transport = DHIPTransport(self.host, self.port, timeout=3600)
                transport.connect()
                login = transport.login(self.username, self.password)
                login_params = login.get("params") or {}
                keepalive_interval = int(
                    login_params.get("keepAliveInterval", 60) or 60
                )

                response, _ = transport.request(method, {})
                if not response.get("result"):
                    raise RuntimeError(f"{method} failed: {response}")
                sid = (response.get("params") or {}).get("SID")

                with self.lock:
                    self.subscription_transports[method] = transport
                    status = self.subscription_status[method]
                    status.update(
                        {
                            "connected": True,
                            "sid": sid,
                            "connected_at": timestamp(),
                            "last_error": None,
                            "keepalive_interval": keepalive_interval,
                        }
                    )

                keepalive_thread = threading.Thread(
                    target=self._subscription_keepalive_loop,
                    args=(
                        method,
                        transport,
                        keepalive_interval,
                        keepalive_stop,
                    ),
                    name=f"dahua-arc-{method.split('.')[-1]}-keepalive",
                    daemon=True,
                )
                keepalive_thread.start()

                while not self.stop_event.is_set():
                    obj, data, meta = transport.recv_frame()

                    # Keepalive replies share this socket with notifications.
                    with self.lock:
                        keepalive_request_id = self.subscription_status[method].get(
                            "keepalive_request_id"
                        )
                    if (
                        keepalive_request_id is not None
                        and obj.get("id") == keepalive_request_id
                        and "result" in obj
                    ):
                        with self.lock:
                            self.subscription_status[method]["last_keepalive_reply"] = (
                                timestamp()
                            )
                        continue

                    self._record_subscription_frame(method, obj, data, meta)

            except Exception as exc:
                if not self.stop_event.is_set():
                    with self.lock:
                        self.subscription_status[method]["last_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
            finally:
                keepalive_stop.set()
                if keepalive_thread is not None:
                    keepalive_thread.join(timeout=1)
                with self.lock:
                    current = self.subscription_transports.get(method)
                    if current is transport:
                        self.subscription_transports.pop(method, None)
                    self.subscription_status[method]["connected"] = False
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass

            if not self.stop_event.wait(2):
                continue
            break

    def _run(self) -> None:
        client: InventoryRpcClient | None = None
        while not self.stop_event.is_set():
            try:
                if client is None:
                    client = InventoryRpcClient(
                        self.host, self.port, self.username, self.password
                    )
                    client.connect()
                    with self.lock:
                        self.connected = True
                        self.last_error = None
                self._poll_once(client)
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.last_error = f"{type(exc).__name__}: {exc}"
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = None
            if self.stop_event.wait(self.POLL_SECONDS):
                break

        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        with self.lock:
            self.connected = False

    def start(self) -> None:
        if not self.zones or self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._run,
            name="dahua-arc-wpan-research",
            daemon=True,
        )
        self.thread.start()

        for method in self.subscription_methods:
            sub_thread = threading.Thread(
                target=self._subscription_run,
                args=(method,),
                name=f"dahua-arc-{method.split('.')[-1]}",
                daemon=True,
            )
            self.subscription_threads[method] = sub_thread
            sub_thread.start()

    def stop(self) -> None:
        self.stop_event.set()

        # Closing sockets unblocks recv_frame() immediately.
        with self.lock:
            transports = list(self.subscription_transports.values())
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass

        if self.thread is not None:
            self.thread.join(timeout=3)
        for sub_thread in self.subscription_threads.values():
            sub_thread.join(timeout=3)

    def diagnostics(self) -> dict[str, Any]:
        with self.lock:
            return {
                "read_only": True,
                "poll_interval_seconds": self.POLL_SECONDS,
                "connected": self.connected,
                "last_error": self.last_error,
                "poll_count": self.poll_count,
                "last_poll_at": self.last_poll_at,
                "targets": {
                    idx: {
                        "name": zone.name,
                        "alarm_index": zone.index,
                        "short_addr": zone.level1,
                        "sense_method": zone.sense_method,
                    }
                    for idx, zone in sorted(self.zones.items())
                },
                "query_styles": dict(self.query_styles),
                "discovery_exhausted": sorted(self.discovery_exhausted),
                "discovery_attempts": list(self.discovery_attempts),
                "attach_probe_complete": self.attach_probe_complete,
                "attach_probe_results": list(self.attach_probe_results),
                "persistent_subscriptions": {
                    method: dict(status)
                    for method, status in self.subscription_status.items()
                },
                "subscription_recent_frames": list(self.subscription_recent_frames),
                "subscription_motion_candidates": list(
                    self.subscription_motion_candidates
                ),
                "last_pircam_alarm_state": dict(self.last_pircam_alarm_state),
                "last_responses": dict(self.last_responses),
                "changes": list(self.changes),
            }


class ArcHub:
    """Own the two DHIP connections and the state engine."""

    def __init__(
        self,
        host: str,
        http_port: int,
        dhip_port: int,
        username: str,
        password: str,
        periodic_resync_seconds: int = 300,
        enable_research_features: bool = False,
    ):
        self.host, self.http_port, self.dhip_port = host, http_port, dhip_port
        self.username, self.password = username, password
        self.periodic_resync_seconds = periodic_resync_seconds
        self.enable_research_features = enable_research_features
        self.zones: dict[int, Zone] = {}
        self.parents: dict[int, dict[str, Any]] = {}
        self.alarm_records: dict[int, dict[str, str]] = {}
        self.initial_alarm_snapshot: list[dict[str, Any]] = []
        self.rpc_inventory: dict[str, Any] = {}
        self.research_refresh_inventory: dict[str, Any] = {}
        self.inventory_error: str | None = None
        self.area_hints: dict[int, str] = {}
        self.radio_devices: dict[int, RadioDeviceInfo] = {}
        self.event_catalog = EventCatalog()
        # Latest PIR-camera snapshot metadata, keyed by Alarm[] index.
        # Populated from ManualTest / SpecialFileDelayUpload events.
        self.pircam_snapshots: dict[int, dict[str, Any]] = {}
        # Last successfully retrieved JPEG, kept in memory because ARC
        # /var/tmp files are short-lived.
        self._pircam_image_cache: dict[int, bytes] = {}
        self._listeners: set[Callable[[set[int] | None], None]] = set()
        self.snapshot_client: SnapshotClient | None = None
        self.engine: StateEngine | None = None
        self.reconciler: Reconciler | None = None
        self.realtime: RealtimeClient | None = None
        self._periodic_stop = threading.Event()
        self._periodic_thread: threading.Thread | None = None
        self.wpan_research: WPANResearchPoller | None = None

        # v0.4.12 research: tightly-scoped ARD1731 detector-test control.
        # Only PIRCamera Staircase GF is exposed. This never arms/disarms the
        # ARC and never touches siren/output methods.
        self._detector_test_lock = threading.RLock()
        self._detector_test_timer: threading.Timer | None = None
        self._detector_test_style: str | None = None
        self._detector_test_control: dict[str, Any] = {
            "target_name": "PIRCamera Staircase GF",
            "target_index": None,
            "target_short_addr": None,
            "enabled": False,
            "successful_style": None,
            "auto_stop_seconds": 120,
            "last_requested_at": None,
            "last_action": None,
            "last_success": None,
            "last_error": None,
            "attempts": [],
            "factory_attempts": [],
            "last_factory_object": None,
            "last_destroy_result": None,
        }

    def add_listener(
        self, callback: Callable[[set[int] | None], None]
    ) -> Callable[[], None]:
        self._listeners.add(callback)

        def remove() -> None:
            self._listeners.discard(callback)

        return remove

    def _notify(self, indices: set[int] | None = None) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(indices)
            except Exception:
                _LOGGER.exception("ARC listener failed")

    def _process_aux_event(self, event: dict[str, Any]) -> None:
        """Track ARD1731 snapshot and armed-alarm media lifecycles.

        Manual Snapshot Test:
            ManualTest -> JPEG -> SpecialFileDelayUpload

        Real armed PIR intrusion:
            AlarmLocal Start/Stop -> MP4 -> SpecialFileDelayUpload

        The two media flows are deliberately kept separate so an alarm video
        never replaces/corrupts the Home Assistant still-image camera entity.
        """
        code = str(event.get("Code") or "")
        data = event.get("Data") or {}
        if not isinstance(data, dict):
            return

        if code == "ManualTest" and str(data.get("DevType") or "") == "PIRCam":
            idx = safe_int(event.get("Index"))
            if idx is None:
                return
            entry = self.pircam_snapshots.setdefault(idx, {})
            entry.update(
                {
                    "index": idx,
                    "name": str(
                        data.get("Name")
                        or self.zones.get(idx, Zone(idx)).name
                        or f"PIRCam {idx}"
                    ),
                    "model": str(data.get("Model") or ""),
                    "alarm_id": str(data.get("AlarmId") or ""),
                    "delay_upload_seq": str(data.get("DelayUploadSeq") or ""),
                    "file_path": str(data.get("FilePath") or ""),
                    "picture_count": safe_int(
                        (data.get("FilesInfo") or {}).get("PictureCount")
                    ),
                    "captured_at": str(data.get("LocaleTime") or timestamp()),
                    "last_event": code,
                    "media_kind": "snapshot_jpeg",
                    # Clear transient transfer metadata from the prior snapshot.
                    "temp_path": "",
                    "expected_length": None,
                    "unique_id": "",
                    "upload_ready_at": None,
                }
            )
            self._notify({idx})
            return

        if (
            code == PIRCAM_EVENT_CODE
            and event.get("Action") == "Start"
            and str(data.get("DevType") or data.get("SenseMethod") or "") == "PIRCam"
        ):
            idx = safe_int(event.get("Index"))
            if idx is None:
                return
            entry = self.pircam_snapshots.setdefault(idx, {})
            entry.update(
                {
                    "index": idx,
                    "name": str(
                        data.get("Name")
                        or self.zones.get(idx, Zone(idx)).name
                        or f"PIRCam {idx}"
                    ),
                    "model": str(data.get("Model") or ""),
                    "last_alarm_id": str(data.get("AlarmId") or ""),
                    "last_alarm_at": str(data.get("LocaleTime") or timestamp()),
                    "last_alarm_media_kind": (
                        "video_mp4"
                        if safe_int(data.get("FileType")) == 2
                        else "unknown"
                    ),
                    "last_alarm_file_path": str(data.get("FilePath") or ""),
                    "last_alarm_file_type": safe_int(data.get("FileType")),
                    "last_alarm_video_count": safe_int(
                        (data.get("FilesInfo") or {}).get("VideoCount")
                    ),
                    "last_alarm_delay_upload_seq": str(
                        data.get("DelayUploadSeq") or ""
                    ),
                    "last_alarm_event": code,
                }
            )
            self._notify({idx})
            return

        if code == "SpecialFileDelayUpload":
            upload_seq = str(data.get("UploadSeq") or "")
            files = data.get("Files") or []
            if not upload_seq or not isinstance(files, list):
                return
            first = files[0] if files and isinstance(files[0], dict) else {}

            for idx, entry in self.pircam_snapshots.items():
                if entry.get("delay_upload_seq") == upload_seq:
                    # Snapshot Test JPEG transfer.
                    entry.update(
                        {
                            "temp_path": str(first.get("FilePath") or ""),
                            "expected_length": safe_int(first.get("Length")),
                            "unique_id": str(first.get("UniqueID") or upload_seq),
                            "upload_ready_at": str(
                                data.get("LocaleTime") or timestamp()
                            ),
                            "last_event": code,
                        }
                    )
                    self._notify({idx})
                    return

                if entry.get("last_alarm_delay_upload_seq") == upload_seq:
                    # Armed intrusion media transfer. Current ARD1731 firmware
                    # reports this as a short MP4, not a JPEG snapshot.
                    entry.update(
                        {
                            "last_alarm_temp_path": str(first.get("FilePath") or ""),
                            "last_alarm_expected_length": safe_int(first.get("Length")),
                            "last_alarm_unique_id": str(
                                first.get("UniqueID") or upload_seq
                            ),
                            "last_alarm_upload_ready_at": str(
                                data.get("LocaleTime") or timestamp()
                            ),
                            "last_alarm_transfer_format": safe_int(first.get("Format")),
                        }
                    )
                    self._notify({idx})
                    return

    def pircam_snapshot(self, index: int) -> dict[str, Any] | None:
        item = self.pircam_snapshots.get(index)
        return dict(item) if item else None

    def fetch_pircam_image(self, index: int) -> bytes | None:
        """Fetch the latest PIR-camera JPEG locally from the ARC.

        Preferred path: DHIP FileManager.downloadFile using the exact file
        paths reported by ManualTest / SpecialFileDelayUpload.
        HTTP is retained only as a fallback for firmware variants that expose
        the persistent backup path directly.
        """
        item = self.pircam_snapshots.get(index)
        if not item:
            return None

        expected = safe_int(item.get("expected_length"))
        candidates: list[tuple[str, str]] = []
        temp_path = str(item.get("temp_path") or "")
        file_path = str(item.get("file_path") or "")
        if temp_path.startswith("/"):
            candidates.append(("dhip-temp", temp_path))
        if file_path.startswith("/") and file_path != temp_path:
            candidates.append(("dhip-backup", file_path))

        errors: list[str] = []

        # Primary method: Dahua's local DHIP FileManager.downloadFile.
        if self.snapshot_client is not None:
            for method_name, remote_path in candidates:
                try:
                    raw = self.snapshot_client.download_file(
                        remote_path,
                        expected_length=expected,
                        timeout=20.0,
                    )
                    if not raw:
                        errors.append(f"{method_name}: empty response")
                        continue

                    jpeg, payload_debug = self.snapshot_client._extract_embedded_jpeg(
                        raw
                    )
                    item["last_payload_debug"] = payload_debug
                    if jpeg is None:
                        errors.append(
                            f"{method_name}: no complete JPEG in payload "
                            f"({len(raw)} bytes; SOI={payload_debug.get('jpeg_soi_offset')}; "
                            f"EOI={payload_debug.get('jpeg_eoi_offset')})"
                        )
                        continue
                    raw = jpeg

                    item["last_fetch_method"] = method_name
                    item["last_fetch_path"] = remote_path
                    item["last_fetch_bytes"] = len(raw)
                    item["last_fetch_at"] = timestamp()
                    item["last_fetch_error"] = None
                    item["last_fetch_fallback"] = None
                    self._pircam_image_cache[index] = raw
                    if expected and expected != len(raw):
                        item["length_mismatch"] = f"expected={expected}, got={len(raw)}"
                    else:
                        item["length_mismatch"] = None
                    return raw
                except Exception as exc:
                    errors.append(f"{method_name}: {type(exc).__name__}: {exc}")

        # Fallback: exact persistent path over HTTP Digest. Some Dahua models
        # expose it directly, while others require FileManager.downloadFile.
        if file_path.startswith("/"):
            url = f"http://{self.host}:{self.http_port}{file_path}"
            mgr = HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(
                None,
                f"http://{self.host}:{self.http_port}/",
                self.username,
                self.password,
            )
            opener = build_opener(HTTPDigestAuthHandler(mgr))
            try:
                with opener.open(url, timeout=12) as response:
                    raw = response.read(5 * 1024 * 1024)
                if len(raw) >= 4 and raw[:2] == b"\\xff\\xd8":
                    item["last_fetch_method"] = "http-backup"
                    item["last_fetch_path"] = file_path
                    item["last_fetch_bytes"] = len(raw)
                    item["last_fetch_at"] = timestamp()
                    item["last_fetch_error"] = None
                    item["last_fetch_fallback"] = None
                    self._pircam_image_cache[index] = raw
                    item["length_mismatch"] = (
                        f"expected={expected}, got={len(raw)}"
                        if expected and expected != len(raw)
                        else None
                    )
                    return raw
                errors.append(f"http-backup: non-JPEG payload ({len(raw)} bytes)")
            except Exception as exc:
                errors.append(f"http-backup: {type(exc).__name__}: {exc}")

        item["last_fetch_at"] = timestamp()
        item["last_fetch_bytes"] = None
        item["last_fetch_error"] = (
            " | ".join(errors) or "No usable PIR-camera file path"
        )

        cached = self._pircam_image_cache.get(index)
        if cached:
            item["last_fetch_fallback"] = "memory-cache"
            item["last_fetch_bytes"] = len(cached)
            return cached

        item["last_fetch_fallback"] = None
        return None

    def _detector_test_target(self) -> Zone:
        """Return the single PIRCam allowed for the v0.4.12 write experiment."""
        target_name = str(self._detector_test_control["target_name"])
        matches = [
            zone
            for zone in self.zones.values()
            if zone.name == target_name and zone.sense_method == "PIRCam"
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one {target_name!r} PIRCam, found {len(matches)}"
            )
        zone = matches[0]
        if zone.level1 is None or zone.level1 <= 0:
            raise RuntimeError("Detector-test target has no wireless short address")
        return zone

    @staticmethod
    def _detector_test_payload(
        style: str,
        short_addr: int,
        enabled: bool,
    ) -> dict[str, Any]:
        """Build one bounded candidate representation of the same operation.

        Public NetSDK maps the operation to NET_WPAN_ACCESSORY_INFO fields
        nShortAddr + bySensitivityTest. Dahua DHIP commonly strips NetSDK type
        prefixes and exposes PascalCase JSON, but this firmware's exact JSON
        spelling is undocumented. Every candidate below therefore expresses
        only the same two semantic fields: target address and SensitivityTest.
        """
        value = 1 if enabled else 0
        if style == "InfoPascal":
            return {
                "Info": {
                    "ShortAddr": short_addr,
                    "SensitivityTest": value,
                }
            }
        if style == "InfoSdkNames":
            return {
                "Info": {
                    "nShortAddr": short_addr,
                    "bySensitivityTest": value,
                }
            }
        if style == "StuInfoSdkNames":
            return {
                "stuInfo": {
                    "nShortAddr": short_addr,
                    "bySensitivityTest": value,
                }
            }
        raise ValueError(f"Unknown detector-test payload style: {style}")

    @staticmethod
    def _rpc_result_summary(result: dict[str, Any]) -> dict[str, Any]:
        response = result.get("response")
        error_text = result.get("error")
        if isinstance(response, dict):
            error = response.get("error")
            return {
                "ok": bool(result.get("ok")),
                "result": response.get("result"),
                "error_code": error.get("code") if isinstance(error, dict) else None,
                "error_message": (
                    error.get("message") if isinstance(error, dict) else None
                ),
            }
        return {
            "ok": bool(result.get("ok")),
            "result": None,
            "error_code": None,
            "error_message": str(error_text or ""),
        }

    @staticmethod
    def _raw_dhip_call(
        transport: DHIPTransport,
        method: str,
        params: Any,
        *,
        object_id: int | None = None,
    ) -> dict[str, Any]:
        """Send an exact DHIP RPC, preserving params=None and optional object."""
        with transport._lock:
            transport._id += 1
            request_id = transport._id
            payload: dict[str, Any] = {
                "method": method,
                "id": request_id,
                "params": params,
                "session": transport.session,
            }
            if object_id is not None:
                payload["object"] = int(object_id)
            transport._send_frame(payload)
            obj, _data, _meta = transport.recv_frame()
            if obj.get("id") != request_id:
                raise RuntimeError(
                    f"{method} response id mismatch: "
                    f"expected {request_id}, got {obj.get('id')}"
                )
            return obj

    @staticmethod
    def _extract_factory_object(response: dict[str, Any]) -> int | None:
        """Extract a Dahua factory object id from known RPC response shapes."""
        params = response.get("params")
        # Dahua instance services return the newly-created object ID directly
        # in the top-level "result" field. This is not merely a boolean.
        # Example used by working Dahua integrations:
        #   object_id = command("<service>.factory.instance")["result"]
        candidates: list[Any] = [
            response.get("result"),
            response.get("object"),
            response.get("Object"),
        ]
        if isinstance(params, dict):
            candidates.extend(
                [
                    params.get("object"),
                    params.get("Object"),
                    params.get("objectId"),
                    params.get("ObjectID"),
                ]
            )
        for value in candidates:
            if isinstance(value, bool):
                continue
            try:
                obj = int(value)
            except TypeError, ValueError:
                continue
            if obj > 0:
                return obj
        return None

    @classmethod
    def _response_summary(cls, response: dict[str, Any]) -> dict[str, Any]:
        error = response.get("error")
        return {
            "result": response.get("result"),
            "error_code": error.get("code") if isinstance(error, dict) else None,
            "error_message": (
                error.get("message") if isinstance(error, dict) else None
            ),
            "object": cls._extract_factory_object(response),
        }

    def _detector_test_rpc(
        self,
        enabled: bool,
        *,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Execute the Dahua instance-service form of setAccessoryParam.

        v0.4.12 proved that the direct service-level call is rejected. Dahua
        RPC tooling and integrations use:
          service.factory.instance -> service.method(object=...) -> service.destroy
        for methods belonging to an instance service. LowRateWPAN advertises
        factory.instance, so v0.4.13 follows that exact pattern.
        """
        zone = self._detector_test_target()
        short_addr = int(zone.level1 or 0)

        with self._detector_test_lock:
            preferred = self._detector_test_style

        styles = [preferred] if preferred else []
        for candidate in ("InfoPascal", "InfoSdkNames", "StuInfoSdkNames"):
            if candidate not in styles:
                styles.append(candidate)

        transport = DHIPTransport(self.host, self.dhip_port, timeout=12)
        attempts: list[dict[str, Any]] = []
        factory_attempts: list[dict[str, Any]] = []
        successful_style: str | None = None
        successful_result: dict[str, Any] | None = None
        object_id: int | None = None
        destroy_summary: dict[str, Any] | None = None

        try:
            transport.connect()
            transport.login(self.username, self.password)

            # Dahua tooling normalizes empty factory params to JSON null.
            # Try null first, then {} only if this firmware rejects null.
            for factory_style, factory_params in (
                ("null", None),
                ("empty_object", {}),
            ):
                response = self._raw_dhip_call(
                    transport,
                    "LowRateWPAN.factory.instance",
                    factory_params,
                )
                summary = {
                    "at": timestamp(),
                    "style": factory_style,
                    **self._response_summary(response),
                }
                factory_attempts.append(summary)
                object_id = self._extract_factory_object(response)
                if object_id is not None:
                    break

            if object_id is None:
                raise RuntimeError(
                    "LowRateWPAN.factory.instance did not return a usable object"
                )

            for style in styles:
                params = self._detector_test_payload(style, short_addr, enabled)
                response = self._raw_dhip_call(
                    transport,
                    "LowRateWPAN.setAccessoryParam",
                    params,
                    object_id=object_id,
                )
                summary = {
                    "at": timestamp(),
                    "action": "start" if enabled else "stop",
                    "automatic": automatic,
                    "style": style,
                    "object": object_id,
                    **self._response_summary(response),
                }
                attempts.append(summary)
                if response.get("result"):
                    successful_style = style
                    successful_result = summary
                    break

        finally:
            # Destroy the short-lived LowRateWPAN instance whenever one was
            # successfully created. Failure to destroy is recorded but does
            # not overwrite the primary command result.
            if object_id is not None and transport.sock is not None:
                try:
                    response = self._raw_dhip_call(
                        transport,
                        "LowRateWPAN.destroy",
                        None,
                        object_id=object_id,
                    )
                    destroy_summary = self._response_summary(response)
                except Exception as exc:
                    destroy_summary = {
                        "result": False,
                        "error_code": None,
                        "error_message": f"{type(exc).__name__}: {exc}",
                        "object": object_id,
                    }
            transport.close()

        now = timestamp()
        with self._detector_test_lock:
            self._detector_test_control["target_index"] = zone.index
            self._detector_test_control["target_short_addr"] = short_addr
            self._detector_test_control["last_requested_at"] = now
            self._detector_test_control["last_action"] = (
                "auto-stop"
                if automatic and not enabled
                else ("start" if enabled else "stop")
            )
            self._detector_test_control["factory_attempts"] = factory_attempts[-10:]
            self._detector_test_control["last_factory_object"] = object_id
            self._detector_test_control["last_destroy_result"] = destroy_summary

            history = list(self._detector_test_control.get("attempts") or [])
            history.extend(attempts)
            self._detector_test_control["attempts"] = history[-40:]

            if successful_style is None:
                self._detector_test_control["last_success"] = False
                if object_id is None:
                    detail = "factory.instance returned no object"
                elif attempts:
                    errors = ", ".join(
                        f"{a['style']}={a.get('error_code')}:{a.get('error_message')}"
                        for a in attempts
                    )
                    detail = f"instance setAccessoryParam rejected: {errors}"
                else:
                    detail = "instance setAccessoryParam produced no result"
                self._detector_test_control["last_error"] = detail
                self._notify({zone.index})
                raise RuntimeError(detail)

            self._detector_test_style = successful_style
            self._detector_test_control["successful_style"] = successful_style
            self._detector_test_control["enabled"] = enabled
            self._detector_test_control["last_success"] = True
            self._detector_test_control["last_error"] = None

        self._notify({zone.index})
        return dict(successful_result or {})

    def start_detector_test(self) -> dict[str, Any]:
        """Start the local ARD1731 sensitivity/detector test for 120 seconds."""
        result = self._detector_test_rpc(True)

        with self._detector_test_lock:
            if self._detector_test_timer is not None:
                self._detector_test_timer.cancel()

            timer = threading.Timer(
                float(self._detector_test_control["auto_stop_seconds"]),
                self._auto_stop_detector_test,
            )
            timer.daemon = True
            self._detector_test_timer = timer
            timer.start()

        return result

    def _auto_stop_detector_test(self) -> None:
        try:
            self._detector_test_rpc(False, automatic=True)
        except Exception as exc:
            with self._detector_test_lock:
                self._detector_test_control["last_error"] = (
                    f"Automatic detector-test stop failed: {type(exc).__name__}: {exc}"
                )
        finally:
            with self._detector_test_lock:
                self._detector_test_timer = None

    def stop_detector_test(self) -> dict[str, Any]:
        """Stop the local ARD1731 sensitivity/detector test immediately."""
        with self._detector_test_lock:
            timer = self._detector_test_timer
            self._detector_test_timer = None
        if timer is not None:
            timer.cancel()

        return self._detector_test_rpc(False)

    def detector_test_status(self) -> dict[str, Any]:
        with self._detector_test_lock:
            data = dict(self._detector_test_control)
            data["timer_active"] = bool(
                self._detector_test_timer and self._detector_test_timer.is_alive()
            )
            return data

    @property
    def available(self) -> bool:
        # A cached snapshot socket can remain connected after event delivery has
        # stopped. Expose zone state only while the realtime path is healthy;
        # its reconnect path takes a fresh authoritative snapshot first.
        return bool(self.realtime and self.realtime.connected)

    def _system_rpc_value(self, method: str) -> Any:
        return rpc_response_value((self.rpc_inventory.get("system") or {}).get(method))

    @property
    def device_type(self) -> str:
        value = self._system_rpc_value("magicBox.getDeviceType")
        return str(value) if value else "Dahua ARC alarm hub"

    @property
    def software_version(self) -> str | None:
        value = self._system_rpc_value("magicBox.getSoftwareVersion")
        return str(value) if value else None

    @property
    def serial_number(self) -> str | None:
        value = self._system_rpc_value("magicBox.getSerialNo")
        return str(value) if value else None

    @property
    def primary_zones(self) -> dict[int, Zone]:
        """Physical inputs that have meaningful HA binary-sensor semantics."""
        return {
            idx: zone
            for idx, zone in self.zones.items()
            if zone.classification in PRIMARY_SENSOR_CLASSES
        }

    def radio_device_for_zone(self, zone: Zone) -> RadioDeviceInfo | None:
        if zone.level1 is None or zone.level1 <= 0:
            return None
        return self.radio_devices.get(zone.level1)

    def start(self) -> None:
        # CGI Alarm[] remains the authoritative topology source for the proven
        # AlarmIn path, but v0.3 keeps every meaningful record for inventory.
        self.alarm_records = fetch_alarm_config(
            self.host, self.http_port, self.username, self.password
        )
        self.parents = discover_multiio_parents(self.alarm_records)

        # Acquire one authoritative snapshot before constructing entities.
        # This lets us expose non-MultiIO Alarm[] records only when the ARC
        # itself confirms that the same index is a real AlarmIn state.
        self.snapshot_client = SnapshotClient(
            self.host, self.dhip_port, self.username, self.password
        )
        self.snapshot_client.connect()
        self.initial_alarm_snapshot = self.snapshot_client.snapshot()
        self.zones = discover_alarm_points(
            self.alarm_records, self.initial_alarm_snapshot, self.parents
        )

        # Full read-only inventory discovery is deliberately isolated from the
        # production state engine.  Unsupported methods/config tables are
        # recorded as discovery failures and never prevent normal operation.
        inventory_client = InventoryRpcClient(
            self.host, self.dhip_port, self.username, self.password
        )
        try:
            self.rpc_inventory = inventory_client.collect(
                include_method_catalog=self.enable_research_features
            )
            self.inventory_error = None
        except Exception as exc:
            self.inventory_error = f"{type(exc).__name__}: {exc}"
            self.rpc_inventory = {"error": self.inventory_error}
            _LOGGER.warning("ARC extended inventory discovery failed: %s", exc)
        finally:
            inventory_client.close()

        # Correlate authoritative Alarm[] rows with Dahua subsystem areas and
        # the AirFly radio-device map. This gives real physical devices, model
        # numbers, stable serials and repeater parent relationships without
        # changing the proven AlarmIn state engine.
        self.area_hints = extract_zone_area_hints(self.rpc_inventory)
        for idx, zone in self.zones.items():
            zone.area_hint = self.area_hints.get(idx, "")
        self.radio_devices = extract_radio_devices(
            self.rpc_inventory, self.alarm_records, self.area_hints
        )

        self.engine = StateEngine(self.zones, self._notify)
        self.engine.start()
        self.reconciler = Reconciler(self.snapshot_client, self.engine)
        self.realtime = RealtimeClient(
            self.host,
            self.dhip_port,
            self.username,
            self.password,
            self.engine,
            self.reconciler,
            self.event_catalog,
            lambda: self._notify(None),
            self._process_aux_event,
        )
        self.realtime.start()
        if not self.realtime.ready_event.wait(30):
            raise TimeoutError(
                "ARC realtime DHIP attach/resync did not complete within 30 seconds"
            )
        if self.realtime.last_error and not self.realtime.connected:
            raise RuntimeError(self.realtime.last_error)
        self._periodic_thread = threading.Thread(
            target=self._periodic_loop,
            name="dahua-arc-periodic",
            daemon=True,
        )
        self._periodic_thread.start()

        if self.enable_research_features:
            # Temporary research-only read path for Detector Test. This performs
            # only LowRateWPAN get* calls and never modifies accessory state.
            self.wpan_research = WPANResearchPoller(
                self.host,
                self.dhip_port,
                self.username,
                self.password,
                self.zones,
                self.radio_devices,
                self.engine,
            )
            self.wpan_research.start()

        _LOGGER.info(
            "Dahua ARC discovery: %d physical AlarmIn records, %d primary sensors, "
            "%d paired radio devices (%d MultiIO inputs)",
            len(self.zones),
            len(self.primary_zones),
            len(self.radio_devices),
            sum(1 for zone in self.primary_zones.values() if zone.is_multiio),
        )

    def refresh_research_inventory(self) -> None:
        """Refresh dynamic read-only research tables for diagnostics download.

        Service/method introspection is collected once at startup. This refresh
        deliberately skips the expensive listMethod pass and re-reads current
        config/event-state tables after the user has performed a PIR-camera
        Snapshot Test in DMSS. It does not change entities or runtime state.
        """
        client = InventoryRpcClient(
            self.host, self.dhip_port, self.username, self.password
        )
        try:
            client.connect()
            self.research_refresh_inventory = client.collect(
                include_method_catalog=False
            )
        except Exception as exc:
            self.research_refresh_inventory = {
                "collected_at": datetime.now().isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _LOGGER.warning("ARC research inventory refresh failed: %s", exc)
        finally:
            client.close()

    def _periodic_loop(self) -> None:
        while not self._periodic_stop.wait(self.periodic_resync_seconds):
            try:
                if self.reconciler:
                    self.reconciler.run("periodic pure-DHIP sanity resync", "periodic")
                    self._notify(None)
            except Exception as exc:
                _LOGGER.warning("ARC periodic snapshot failed: %s", exc)
                self._notify(None)

    def inventory_summary(self) -> dict[str, Any]:
        alarm_summary = summarize_alarm_records(self.alarm_records)
        rpc_summary = summarize_rpc_inventory(self.rpc_inventory)
        event_summary = self.event_catalog.summary()
        return {
            **alarm_summary,
            "tracked_physical_alarm_records": len(self.zones),
            "exposed_alarm_inputs": len(self.primary_zones),
            "paired_radio_devices": len(self.radio_devices),
            "multiio_parents": len(self.parents),
            "multiio_inputs": sum(
                1 for z in self.primary_zones.values() if z.is_multiio
            ),
            "wireless_primary_sensors": sum(
                1 for z in self.primary_zones.values() if not z.is_multiio
            ),
            "non_multiio_alarm_inputs": sum(
                1 for z in self.primary_zones.values() if not z.is_multiio
            ),
            **rpc_summary,
            "event_codes_observed": event_summary["event_codes_observed"],
            "all_events_observed": event_summary["total_events"],
            "inventory_error": self.inventory_error,
        }

    def diagnostics(self) -> dict[str, Any]:
        meaningful_records = {
            idx: {
                "classification": classify_alarm_record(cfg),
                "config": redact_sensitive(cfg),
            }
            for idx, cfg in self.alarm_records.items()
            if record_is_physical(cfg)
        }
        return {
            "inventory_summary": self.inventory_summary(),
            "available": self.available,
            "enable_research_features": self.enable_research_features,
            "realtime": self.realtime.health() if self.realtime else None,
            "snapshot": self.snapshot_client.health() if self.snapshot_client else None,
            "engine": self.engine.metrics() if self.engine else None,
            "alarm_records": meaningful_records,
            "extended_rpc_inventory": redact_sensitive(self.rpc_inventory),
            "research_refresh_inventory": redact_sensitive(
                self.research_refresh_inventory
            ),
            "event_catalog": self.event_catalog.summary(),
            "wpan_research": (
                self.wpan_research.diagnostics()
                if self.wpan_research is not None
                else None
            ),
            "detector_test_control": self.detector_test_status(),
            "pircam_snapshots": {
                idx: redact_sensitive(dict(meta))
                for idx, meta in sorted(self.pircam_snapshots.items())
            },
            "area_hints": dict(sorted(self.area_hints.items())),
            "area_decisions": dict(getattr(self, "area_decisions", {})),
            "multiio_child_device_count": len(getattr(self, "child_device_ids", {})),
            "radio_devices": {
                level1: {
                    "alarm_index": d.alarm_index,
                    "name": d.name,
                    "sense_method": d.sense_method,
                    "classification": d.classification,
                    "model": d.model,
                    "serial": "**REDACTED**" if d.serial else None,
                    "serial_hash": d.serial_hash,
                    "parent_level1": d.parent_level1,
                    "area_hint": d.area_hint,
                }
                for level1, d in sorted(self.radio_devices.items())
            },
            "zone_states": {
                idx: {
                    "name": z.name,
                    "classification": z.classification,
                    "is_multiio": z.is_multiio,
                    "sense_method": z.sense_method,
                    "area_hint": z.area_hint or None,
                    "level1": z.level1,
                    "level2": z.level2,
                    "online_state": z.online_state,
                    "raw_alarm_state": z.raw_alarm_state,
                    "active": z.active,
                    "tamper": z.tamper,
                    "low_power": z.low_power,
                    "last_source": z.last_source,
                    "last_action": z.last_action,
                }
                for idx, z in self.zones.items()
            },
        }

    def stop(self) -> None:
        self._periodic_stop.set()
        with self._detector_test_lock:
            detector_timer = self._detector_test_timer
            self._detector_test_timer = None
        if detector_timer is not None:
            detector_timer.cancel()
        if self._periodic_thread is not None:
            self._periodic_thread.join(timeout=2)
        if self.wpan_research is not None:
            self.wpan_research.stop()
        if self.realtime is not None:
            self.realtime.stop()
        if self.engine is not None:
            self.engine.stop()
        if self.snapshot_client is not None:
            self.snapshot_client.close()


def probe_connection(
    host: str, http_port: int, dhip_port: int, username: str, password: str
) -> dict[str, Any]:
    records = fetch_alarm_config(host, http_port, username, password)
    parents = discover_multiio_parents(records)
    client = SnapshotClient(host, dhip_port, username, password)
    try:
        client.connect()
        snapshot = client.snapshot()
        zones = discover_alarm_points(records, snapshot, parents)
        by_pos = {x["array_pos"] for x in snapshot}
        missing_multiio = [
            idx
            for idx, cfg in records.items()
            if classify_alarm_record(cfg) == "multiio_input" and idx not in by_pos
        ]
        if missing_multiio:
            raise RuntimeError(
                f"Snapshot missing configured MultiIO zone indexes: {missing_multiio[:10]}"
            )
        # Fetch only AlarmSubSystem for config-flow area preview. This is a
        # small read-only request and makes the preview use the same Dahua
        # room assignment that runtime smart matching will use.
        area_hints: dict[int, str] = {}
        inventory_client = InventoryRpcClient(host, dhip_port, username, password)
        serial_number: str | None = None
        try:
            inventory_client.connect()
            serial_raw = rpc_response_value(
                inventory_client.safe_request("magicBox.getSerialNo")
            )
            serial_number = str(serial_raw) if serial_raw else None
            subsystem_result = inventory_client.safe_request(
                "configManager.getConfig", {"name": "AlarmSubSystem"}
            )
            area_hints = extract_zone_area_hints(
                {"candidate_configs": {"AlarmSubSystem": subsystem_result}}
            )
        except Exception:
            # Area intelligence is optional and must never make connection
            # validation fail. Sensor-name matching remains a safe fallback.
            area_hints = {}
        finally:
            inventory_client.close()

        summary = summarize_alarm_records(records)
        return {
            "records": len(records),
            "zones": sum(
                1
                for zone in zones.values()
                if zone.classification in PRIMARY_SENSOR_CLASSES
            ),
            "tracked_physical_records": len(zones),
            "serial_number": serial_number,
            "parents": len(parents),
            "snapshot_records": len(snapshot),
            "zone_names": [
                zones[idx].name
                for idx in sorted(zones)
                if zones[idx].classification in PRIMARY_SENSOR_CLASSES
            ],
            "area_match_items": [
                {
                    "index": idx,
                    "name": zones[idx].name,
                    "area_hint": area_hints.get(idx),
                }
                for idx in sorted(zones)
                if zones[idx].classification in PRIMARY_SENSOR_CLASSES
            ],
            "multiio_zones": sum(1 for zone in zones.values() if zone.is_multiio),
            "non_multiio_zones": sum(
                1
                for zone in zones.values()
                if not zone.is_multiio and zone.classification in PRIMARY_SENSOR_CLASSES
            ),
            "meaningful_alarm_records": summary["meaningful_alarm_records"],
        }
    finally:
        client.close()
