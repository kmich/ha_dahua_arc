"""Read-only Dahua ARC discovery and inventory helpers.

The production state path intentionally remains conservative.  This module is
used to learn the complete ARC object model without silently discarding
non-MultiIO devices.  It performs only read-only RPC calls and records all
realtime event codes observed by the integration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import struct
import threading
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .vendor.dahua import DHIPTransport, const

_LOGGER = logging.getLogger(__name__)

CONFIG_MEMBER_METHOD = "configManager.getMemberNames"
CONFIG_GET_METHOD = "configManager.getConfig"
CHANNEL_STATE_METHOD = "AlarmRegion.getChannelsState"
SERVICE_LIST_METHOD = "system.listService"

# v0.4.1 research build: discover the local RPC surface used by Dahua
# AirShield PIR-cameras without executing any unknown methods. Public NetSDK
# structures show that a PIR-camera snapshot test is an accessory-control
# operation and that completion is reported through a ManualTest event. The
# exact DHIP/RPC2 method name is firmware-specific, so we enumerate method
# names/signatures read-only rather than guessing.
_METHOD_KEYWORDS = (
    "snap",
    "photo",
    "image",
    "picture",
    "capture",
    "pic",
    "media",
    "file",
    "accessory",
    "wpan",
    "wireless",
    "airfly",
    "rfhd",
    "manualtest",
    "lowrate",
    "visual",
    "verify",
)
_RESEARCH_CONFIG_NAMES = {
    "_AirFlyDeviceMap_",
    "AirFly",
    "AlarmRecord",
    "ARCEventsRecord",
    "_TapedEventManager_",
    "_CameraDevList_",
    "MediaFileReaderGlobal",
    "ReadFileKeepAlive",
    "Record",
    "RecordEx",
    "UploadFileDCloud",
    "AreaArmMode",
    "DefenceStatus",
    "DefenceStatusOut",
}

# Only read configuration families that can reasonably describe alarm hub
# devices.  We intentionally do not dump the entire configuration database.
_CONFIG_KEYWORDS = (
    "alarm",
    "siren",
    "wireless",
    "sensor",
    "detector",
    "zone",
    "panic",
    "remote",
    "keyfob",
    "smoke",
    "flood",
    "radar",
    "rf",
)
_EXPLICIT_CONFIG_NAMES = {"Alarm", "AlarmIn", "AlarmOut", "CommGlobal"}

# Fields that can uniquely identify a real installation.  Diagnostics keep
# topology/name information because it is needed for reverse engineering, but
# serial-like values are redacted.
_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "serial",
    "serialno",
    "serialnumber",
    "sn",
    "uuid",
    "mac",
    "macaddress",
    "imei",
    "imsi",
    "key",
    "userid",
    "parentnodeid",
    "nodeid",
    "airflyid",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def redact_sensitive(value: Any, key: str | None = None) -> Any:
    """Recursively redact hardware/account identifiers while preserving shape."""
    normalized_key = key.replace("_", "").lower() if key is not None else None
    if normalized_key in _SENSITIVE_KEYS:
        return "**REDACTED**"
    # Event OperatorInfo can contain a phone number, nickname and cloud user id.
    # Keep the object shape but never include personal operator identity in
    # downloadable diagnostics.
    if normalized_key == "operatorinfo" and isinstance(value, dict):
        return {str(k): "**REDACTED**" for k in value}
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def record_is_physical(cfg: dict[str, str]) -> bool:
    """Return True for a real paired/configured ARC device or zone.

    ARC3800H exposes 256 Alarm[] rows. Unused rows are misleadingly marked
    Enable=true and named ZoneXX, but have SenseMethod/SensorType=Default and
    Level1/Level2/Slot=-1. A real device has a concrete SenseMethod and radio
    address/topology fields.
    """
    sense = str(cfg.get("SenseMethod", "")).strip()
    name = str(cfg.get("Name", "")).strip()
    level1 = _safe_int(cfg.get("Level1"), -1)
    level2 = _safe_int(cfg.get("Level2"), -1)
    slot = _safe_int(cfg.get("Slot"), -1)
    topology_present = any(
        value is not None and value >= 0 for value in (level1, level2, slot)
    )

    if sense not in {"", "Default", "None", "Unknown"}:
        return topology_present

    # Some ARC variants may expose local/built-in inputs with SenseMethod=Default.
    # Accept them only when they have real topology and are not untouched ZoneXX
    # template rows. This remains conservative on the ARC3800H while allowing
    # other Dahua ARC families to surface legitimately configured local inputs.
    generic_name = bool(re.fullmatch(r"Zone\d+", name, flags=re.IGNORECASE))
    return (
        bool(_bool_value(cfg.get("Enable", False)))
        and topology_present
        and bool(name)
        and not generic_name
    )


def record_is_meaningful(cfg: dict[str, str]) -> bool:
    """Compatibility alias: meaningful now means a real physical record."""
    return record_is_physical(cfg)


def classify_alarm_record(cfg: dict[str, str]) -> str:
    """Classify a physical ARC record using protocol fields before label text."""
    sense = str(cfg.get("SenseMethod", ""))
    level2 = _safe_int(cfg.get("Level2"))
    text = " ".join(
        str(cfg.get(key, ""))
        for key in ("Name", "SenseMethod", "SensorType", "DefenceAreaType")
    ).casefold()

    if sense == "MultiIOTransmitterP":
        return "multiio_parent" if level2 == 0 else "multiio_input"
    if sense in {"PIRCam", "PassiveInfrared"}:
        return "motion"
    if sense == "ProRepeater":
        return "repeater"
    if sense == "AlarmBell":
        return "siren"
    if sense == "RemoteControl":
        return "keyfob"
    if sense == "LEDKeypad":
        return "keypad"
    if "smoke" in text or "fire" in text:
        return "smoke_or_fire"
    if "flood" in text or "water" in text or "leak" in text:
        return "flood_or_water"
    if "radar" in text:
        return "radar"
    if "pir" in text or "motion" in text:
        return "motion"
    if "magnetic" in text or "contact" in text:
        return "contact"
    if "glass" in text and "break" in text:
        return "glass_break"
    if "siren" in text or "bell" in text:
        return "siren"
    if "keyfob" in text or "remote" in text or "panic" in text:
        return "keyfob"
    if "keypad" in text:
        return "keypad"
    if "repeater" in text:
        return "repeater"
    return "unclassified_physical" if record_is_physical(cfg) else "placeholder"


PRIMARY_SENSOR_CLASSES = frozenset(
    {
        "multiio_input",
        "motion",
        "radar",
        "contact",
        "smoke_or_fire",
        "flood_or_water",
        "glass_break",
    }
)


def is_primary_sensor_record(cfg: dict[str, str]) -> bool:
    return classify_alarm_record(cfg) in PRIMARY_SENSOR_CLASSES


def summarize_alarm_records(records: dict[int, dict[str, str]]) -> dict[str, Any]:
    physical = {idx: cfg for idx, cfg in records.items() if record_is_physical(cfg)}
    categories = Counter(classify_alarm_record(cfg) for cfg in physical.values())
    sense_methods = Counter(
        str(cfg.get("SenseMethod", "") or "<blank>") for cfg in physical.values()
    )
    sensor_types = Counter(
        str(cfg.get("SensorType", "") or "<blank>") for cfg in physical.values()
    )
    return {
        "alarm_table_records": len(records),
        "configured_alarm_records": len(physical),
        "placeholder_alarm_records": len(records) - len(physical),
        # Retain key for compatibility with v0.3 diagnostics/UI.
        "meaningful_alarm_records": len(physical),
        "categories": dict(sorted(categories.items())),
        "sense_methods": dict(sorted(sense_methods.items())),
        "sensor_types": dict(sorted(sensor_types.items())),
    }


def _extract_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _extract_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _extract_strings(item)


def extract_config_member_names(response: dict[str, Any]) -> list[str]:
    """Best-effort member-name extraction across Dahua firmware variants."""
    params = response.get("params") or {}
    candidates: set[str] = set()
    for item in _extract_strings(params):
        text = item.strip()
        if not text or len(text) > 128 or any(ch.isspace() for ch in text):
            continue
        # Config member names are identifier-like. Avoid harvesting arbitrary
        # string values from nested metadata.
        if all(ch.isalnum() or ch in "_-." for ch in text):
            candidates.add(text)
    return sorted(candidates, key=str.casefold)


@dataclass(slots=True)
class RadioDeviceInfo:
    """One paired ARC radio/peripheral device, correlated to Alarm[] + AirFly."""

    level1: int
    alarm_index: int
    name: str
    sense_method: str
    classification: str
    model: str | None = None
    serial: str | None = None
    serial_hash: str | None = None
    parent_level1: int | None = None
    area_hint: str | None = None

    @property
    def is_multiio(self) -> bool:
        return self.classification == "multiio_parent"

    @property
    def device_key(self) -> str:
        """Stable HA device key, preserving the v0.1-v0.3 MultiIO IDs."""
        if self.is_multiio:
            return f"multiio:{self.level1}"
        return f"radio:{self.serial_hash or f'level1-{self.level1}'}"


def _candidate_table(inventory: dict[str, Any], name: str) -> Any:
    item = (inventory.get("candidate_configs") or {}).get(name) or {}
    if not item.get("ok"):
        return None
    return ((item.get("response") or {}).get("params") or {}).get("table")


def extract_zone_area_hints(inventory: dict[str, Any]) -> dict[int, str]:
    """Map Alarm[] config index -> configured Dahua subsystem/area name."""
    table = _candidate_table(inventory, "AlarmSubSystem")
    result: dict[int, str] = {}
    if not isinstance(table, list):
        return result
    for subsystem in table:
        if not isinstance(subsystem, dict) or not subsystem.get("Enable"):
            continue
        name = str(subsystem.get("Name") or "").strip()
        if not name:
            continue
        for idx in subsystem.get("Zone") or []:
            parsed = _safe_int(idx)
            if parsed is not None and parsed >= 0:
                result.setdefault(parsed, name)
    return result


def extract_radio_devices(
    inventory: dict[str, Any],
    records: dict[int, dict[str, str]],
    area_hints: dict[int, str] | None = None,
) -> dict[int, RadioDeviceInfo]:
    """Correlate paired ARC devices using Level1 == AirFly short address.

    On ARC3800H the parent/peripheral Alarm[] records have Level2=0 and Level1
    1..N. _AirFlyDeviceMap_.DeviceInfo uses ShotAddr with the same values, and
    carries model, serial and repeater parent linkage.
    """
    area_hints = area_hints or {}
    parents: dict[int, tuple[int, dict[str, str]]] = {}
    for idx, cfg in records.items():
        if not record_is_physical(cfg):
            continue
        level1 = _safe_int(cfg.get("Level1"))
        level2 = _safe_int(cfg.get("Level2"))
        if level1 is not None and level1 > 0 and level2 == 0:
            parents[level1] = (idx, cfg)

    device_map = _candidate_table(inventory, "_AirFlyDeviceMap_")
    by_addr: dict[int, dict[str, Any]] = {}
    if isinstance(device_map, dict):
        for item in device_map.get("DeviceInfo") or []:
            if not isinstance(item, dict) or not item.get("State"):
                continue
            addr = _safe_int(item.get("ShotAddr"))
            if addr is not None and addr > 0:
                by_addr[addr] = item

    serial_to_level: dict[str, int] = {}
    for level1, item in by_addr.items():
        serial = str(item.get("SN") or "").strip()
        if serial and serial != "**REDACTED**":
            serial_to_level[serial] = level1

    result: dict[int, RadioDeviceInfo] = {}
    for level1, (alarm_index, cfg) in parents.items():
        meta = by_addr.get(level1, {})
        serial = str(meta.get("SN") or "").strip() or None
        if serial == "**REDACTED**":
            serial = None
        serial_hash = (
            hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16] if serial else None
        )
        parent_serial = str(meta.get("ParentNodeId") or "").strip()
        parent_level1 = serial_to_level.get(parent_serial) if parent_serial else None
        result[level1] = RadioDeviceInfo(
            level1=level1,
            alarm_index=alarm_index,
            name=str(cfg.get("Name") or f"ARC device {level1}"),
            sense_method=str(cfg.get("SenseMethod") or ""),
            classification=classify_alarm_record(cfg),
            model=(str(meta.get("ModelName") or "").strip() or None),
            serial=serial,
            serial_hash=serial_hash,
            parent_level1=parent_level1,
            area_hint=area_hints.get(alarm_index),
        )
    return result


def extract_service_names(response: dict[str, Any]) -> list[str]:
    """Extract service names from system.listService."""
    params = response.get("params") or {}
    raw = params.get("service") if isinstance(params, dict) else None
    if not isinstance(raw, list):
        return []
    return sorted(
        {str(item).strip() for item in raw if str(item).strip()},
        key=str.casefold,
    )


def extract_method_names(response: dict[str, Any], service: str) -> list[str]:
    """Extract fully-qualified method names from <service>.listMethod."""
    params = response.get("params") or {}
    raw = params.get("method") if isinstance(params, dict) else None
    if not isinstance(raw, list):
        return []
    result: set[str] = set()
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        result.add(name if "." in name else f"{service}.{name}")
    return sorted(result, key=str.casefold)


def method_is_research_candidate(method: str) -> bool:
    lower = method.casefold()
    return any(keyword in lower for keyword in _METHOD_KEYWORDS)


@dataclass(slots=True)
class EventCodeInfo:
    count: int = 0
    actions: set[str] = field(default_factory=set)
    indexes: set[int] = field(default_factory=set)
    data_keys: set[str] = field(default_factory=set)
    last_seen: str | None = None
    sample: dict[str, Any] | None = None


class EventCatalog:
    """Thread-safe catalogue of every event emitted by eventManager.attach(All)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.codes: dict[str, EventCodeInfo] = {}
        self.total_events = 0
        self.created_at = _now()
        # Keep complete event ordering around Snapshot Test. Per-code samples
        # alone are not enough because a PIR-camera transfer may emit several
        # related events using the same code.
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=1000)

    def observe(self, event: dict[str, Any]) -> None:
        code = str(event.get("Code") or "<no-code>")
        action = str(event.get("Action") or "")
        index = _safe_int(event.get("Index"))
        data = event.get("Data") or event.get("data") or {}
        with self.lock:
            info = self.codes.setdefault(code, EventCodeInfo())
            self.total_events += 1
            info.count += 1
            if action:
                info.actions.add(action)
            if index is not None:
                info.indexes.add(index)
            if isinstance(data, dict):
                info.data_keys.update(str(key) for key in data)
            observed_at = _now()
            info.last_seen = observed_at
            redacted = redact_sensitive(event)
            info.sample = redacted
            self.recent_events.append(
                {
                    "observed_at": observed_at,
                    "event": redacted,
                }
            )

    def summary(self) -> dict[str, Any]:
        with self.lock:
            pircam_trace: list[dict[str, Any]] = []
            for item in self.recent_events:
                event = item.get("event") or {}
                data = event.get("Data") or event.get("data") or {}
                if not isinstance(data, dict):
                    data = {}
                dev_type = str(data.get("DevType") or "")
                sense_method = str(data.get("SenseMethod") or "")
                name = str(data.get("Name") or "")
                if (
                    dev_type in ("PIRCam", "PassiveInfrared")
                    or sense_method in ("PIRCam", "PassiveInfrared")
                    or "PIRCamera" in name
                ):
                    pircam_trace.append(item)

            return {
                "catalog_started_at": self.created_at,
                "total_events": self.total_events,
                "event_codes_observed": len(self.codes),
                "recent_events": list(self.recent_events),
                "pircam_trace": pircam_trace[-250:],
                "codes": {
                    code: {
                        "count": info.count,
                        "actions": sorted(info.actions),
                        "indexes": sorted(info.indexes),
                        "data_keys": sorted(info.data_keys),
                        "last_seen": info.last_seen,
                        "sample": info.sample,
                    }
                    for code, info in sorted(self.codes.items())
                },
            }


class InventoryRpcClient:
    """Short-lived, read-only DHIP client for capability/config discovery."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.transport: DHIPTransport | None = None

    def connect(self) -> None:
        transport = DHIPTransport(self.host, self.port, timeout=12)
        transport.connect()
        transport.login(self.username, self.password)
        self.transport = transport

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            finally:
                self.transport = None

    def _recv_fragmented_json(self, request_id: int) -> dict[str, Any]:
        transport = self.transport
        if transport is None:
            raise RuntimeError("Inventory DHIP transport unavailable")
        chunks: list[bytes] = []
        expected_len: int | None = None
        expected_index = 0
        for _ in range(128):
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
                raise RuntimeError("Invalid DHIP inventory response header")
            if response_id != request_id:
                raise RuntimeError(
                    f"Inventory request id mismatch: expected {request_id}, got {response_id}"
                )
            if package_index != expected_index:
                raise RuntimeError(
                    f"Inventory fragment order mismatch: expected {expected_index}, got {package_index}"
                )
            if data_len:
                raise RuntimeError(
                    "Unexpected binary payload in inventory RPC response"
                )
            if expected_len is None:
                expected_len = message_len
            elif expected_len != message_len:
                raise RuntimeError(
                    "Inventory response length changed between fragments"
                )
            chunks.append(transport._recv_exact(package_len))
            raw = b"".join(chunks)
            if expected_len is not None and len(raw) >= expected_len:
                return json.loads(raw[:expected_len].decode("utf-8"))
            expected_index += 1
        raise RuntimeError("Inventory RPC response exceeded 128 fragments")

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        transport = self.transport
        if transport is None:
            raise RuntimeError("Inventory DHIP transport unavailable")
        with transport._lock:
            transport._id += 1
            request_id = transport._id
            payload: dict[str, Any] = {
                "method": method,
                "id": request_id,
                "params": params or {},
                "session": transport.session,
            }
            transport._send_frame(payload)
            return self._recv_fragmented_json(request_id)

    def safe_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self.request(method, params)
            return {"ok": bool(response.get("result")), "response": response}
        except Exception as exc:  # Discovery should never break the core integration.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def collect(self, include_method_catalog: bool = True) -> dict[str, Any]:
        if self.transport is None:
            self.connect()

        result: dict[str, Any] = {
            "collected_at": _now(),
            "system": {},
            "alarm_rpc": {},
            "config_members": [],
            "candidate_configs": {},
            "service_catalog": {
                "services": [],
                "method_lists": {},
                "interesting_methods": [],
                "method_signatures": {},
                "method_help": {},
            },
        }

        for method in (
            "magicBox.getSystemInfo",
            "magicBox.getDeviceType",
            "magicBox.getSoftwareVersion",
            "magicBox.getHardwareVersion",
            "magicBox.getSerialNo",
        ):
            result["system"][method] = self.safe_request(method)

        # Read-only RPC introspection. This is intentionally exhaustive in the
        # research build so we can find the firmware's local equivalent of the
        # NetSDK low-rate WPAN accessory operations. We never call any discovered
        # write/control method here.
        #
        # ARC3800H exposes LowRateWPAN and RemoteLowRateWPAN late in the service
        # list (beyond the original 180-service research cap), so explicitly
        # prioritize them.
        priority_services = ("LowRateWPAN", "RemoteLowRateWPAN")
        result["service_catalog"]["priority_services"] = list(priority_services)
        result["service_catalog"]["priority_method_metadata"] = {}
        service_result = self.safe_request(SERVICE_LIST_METHOD)
        result["service_catalog"]["list_service"] = service_result
        services: list[str] = []
        if service_result.get("ok"):
            services = extract_service_names(service_result["response"])
            result["service_catalog"]["services"] = services

        if include_method_catalog:
            interesting: set[str] = set()
            services_to_scan = list(services[:180])
            for priority_service in priority_services:
                if (
                    priority_service in services
                    and priority_service not in services_to_scan
                ):
                    services_to_scan.append(priority_service)

            priority_methods: list[str] = []
            for service in services_to_scan:
                listing = self.safe_request(f"{service}.listMethod")
                if listing.get("ok"):
                    methods = extract_method_names(listing["response"], service)
                    result["service_catalog"]["method_lists"][service] = methods
                    if service in priority_services:
                        priority_methods.extend(methods)
                        interesting.update(methods)
                    else:
                        interesting.update(
                            method
                            for method in methods
                            if method_is_research_candidate(method)
                        )

            interesting_list = sorted(interesting, key=str.casefold)
            result["service_catalog"]["interesting_methods"] = interesting_list

            # Ask the hub itself for signatures/help. These are metadata calls
            # only and do not execute the candidate methods. Probe the wireless
            # accessory services first and retain failures too.
            metadata_order: list[str] = []
            for method in priority_methods + interesting_list:
                if method not in metadata_order:
                    metadata_order.append(method)

            for method in metadata_order[:180]:
                sig = self.safe_request(
                    "system.methodSignature", {"method_name": method}
                )
                help_info = self.safe_request(
                    "system.methodHelp", {"method_name": method}
                )
                if method in priority_methods:
                    result["service_catalog"]["priority_method_metadata"][method] = {
                        "signature": sig,
                        "help": help_info,
                    }
                if sig.get("ok"):
                    result["service_catalog"]["method_signatures"][method] = sig
                if help_info.get("ok"):
                    result["service_catalog"]["method_help"][method] = help_info

        for method, params in (
            ("alarm.getInSlots", {}),
            ("alarm.getOutSlots", {}),
            ("alarm.getOutState", {}),
            ("alarm.getAlarmCaps", {}),
            ("AlarmRegion.getAccessoryInfo", {}),
            (CHANNEL_STATE_METHOD, {"Condition": {"Type": "AlarmOut"}}),
        ):
            result["alarm_rpc"][method] = self.safe_request(method, params)

        member_response = self.safe_request(CONFIG_MEMBER_METHOD, {"name": ""})
        result["config_member_response"] = member_response
        if member_response.get("ok"):
            members = extract_config_member_names(member_response["response"])
            result["config_members"] = members
        else:
            members = []

        # Explicit research tables are always requested first. Keyword-based
        # discovery is additive and bounded, so a large config namespace can
        # never crowd out PIR-camera/event/media tables we specifically need.
        explicit = set(_EXPLICIT_CONFIG_NAMES) | set(_RESEARCH_CONFIG_NAMES)
        keyword_selected: set[str] = set()
        for name in members:
            lower = name.casefold()
            if any(keyword in lower for keyword in _CONFIG_KEYWORDS):
                keyword_selected.add(name)

        ordered = sorted(explicit, key=str.casefold)
        ordered += [
            name
            for name in sorted(keyword_selected, key=str.casefold)
            if name not in explicit
        ][:40]
        for name in ordered:
            result["candidate_configs"][name] = self.safe_request(
                CONFIG_GET_METHOD, {"name": name}
            )

        return result


def _first_int_payload(item: dict[str, Any]) -> int | None:
    if not item.get("ok"):
        return None
    response = item.get("response") or {}
    result = response.get("result")
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    params = response.get("params")

    def walk(value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, dict):
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        if isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    return walk(params)


def _state_record_count(item: dict[str, Any]) -> int | None:
    if not item.get("ok"):
        return None
    response = item.get("response") or {}
    params = response.get("params") or {}
    if isinstance(params, dict):
        states = params.get("States")
        if isinstance(states, list):
            return len(states)
    return None


def summarize_rpc_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    candidate_configs = inventory.get("candidate_configs") or {}
    successful_configs = sum(
        1 for response in candidate_configs.values() if response.get("ok")
    )
    alarm_rpc = inventory.get("alarm_rpc") or {}
    alarm_out_snapshot = alarm_rpc.get(CHANNEL_STATE_METHOD) or {}
    service_catalog = inventory.get("service_catalog") or {}
    return {
        "config_namespaces": len(inventory.get("config_members") or []),
        "candidate_configs_requested": len(candidate_configs),
        "candidate_configs_successful": successful_configs,
        "rpc_services": len(service_catalog.get("services") or []),
        "rpc_services_with_method_lists": len(
            service_catalog.get("method_lists") or {}
        ),
        "pircam_candidate_methods": len(
            service_catalog.get("interesting_methods") or []
        ),
        "alarm_rpc_successes": sum(1 for item in alarm_rpc.values() if item.get("ok")),
        "alarm_input_slots": _first_int_payload(
            alarm_rpc.get("alarm.getInSlots") or {}
        ),
        "alarm_output_slots": _first_int_payload(
            alarm_rpc.get("alarm.getOutSlots") or {}
        ),
        "alarm_output_state_records": _state_record_count(alarm_out_snapshot),
        "alarm_out_state_supported": bool(
            (alarm_rpc.get("alarm.getOutState") or {}).get("ok")
        ),
        "alarm_related_config_names": sorted(
            name for name, item in candidate_configs.items() if item.get("ok")
        ),
    }
