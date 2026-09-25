"""Read-only LowRateWPAN research poller (research mode only)."""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from ..protocol.engine import StateEngine
from ..protocol.inventory import InventoryRpcClient, RadioDeviceInfo, redact_sensitive
from ..protocol.models import Zone
from ..protocol.realtime import ATTACH_TIMEOUT_SECONDS, liveness_timeout
from ..protocol.util import keepalive_delay, safe_int, timestamp
from ..vendor.dahua import DHIPTransport, const


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
        self.last_pircam_alarm_state: dict[int, int] = {}
        self.last_responses: dict[str, Any] = {}
        self.last_flat: dict[str, dict[str, Any]] = {}
        self.changes: list[dict[str, Any]] = []
        self.poll_count = 0
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.connected = False

        # Persistent LowRateWPAN subscriptions. Earlier research proved both
        # LowRateWPAN.attachAccessoryInfo({}) and LowRateWPAN.attach({})
        # return a valid SID, but a probe that closes the socket
        # immediately can never receive their notifications.
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
        while not stop.wait(keepalive_delay(keepalive_interval)):
            if self.stop_event.is_set():
                return
            try:
                request_id = transport.send_request(
                    const.KEEPALIVE,
                    {"timeout": keepalive_interval, "active": True},
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
                with contextlib.suppress(Exception):
                    transport.close()
                return

    def _subscription_run(self, method: str) -> None:
        """Keep one successful LowRateWPAN attach socket open and capture pushes."""
        while not self.stop_event.is_set():
            transport: DHIPTransport | None = None
            keepalive_stop = threading.Event()
            keepalive_thread: threading.Thread | None = None
            try:
                transport = DHIPTransport(
                    self.host, self.port, timeout=ATTACH_TIMEOUT_SECONDS
                )
                transport.connect()
                login = transport.login(self.username, self.password)
                login_params = login.get("params") or {}
                keepalive_interval = int(
                    login_params.get("keepAliveInterval", 60) or 60
                )

                response, _ = transport.request(method, {})
                if not response.get("result"):
                    raise RuntimeError(f"{method} failed: {response}")
                # Keepalive replies arrive at least once per interval, so a
                # longer silence means the socket is dead.
                transport.set_timeout(liveness_timeout(keepalive_interval))
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
                    with contextlib.suppress(Exception):
                        transport.close()

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
                    with contextlib.suppress(Exception):
                        client.close()
                    client = None
            if self.stop_event.wait(self.POLL_SECONDS):
                break

        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
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
            with contextlib.suppress(Exception):
                transport.close()

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
