"""CGI discovery of the ARC ``Alarm[]`` topology table."""

from __future__ import annotations

import re
from typing import Any
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    OpenerDirector,
    build_opener,
)

from .inventory import classify_alarm_record, record_is_physical
from .models import Zone
from .util import bool_value, safe_int

ALARM_LINE = re.compile(r"^table\.Alarm\[(\d+)\]\.(.+?)=(.*)$")


def digest_opener(
    host: str, http_port: int, username: str, password: str
) -> OpenerDirector:
    """Build a urllib opener that answers the ARC's HTTP Digest challenge."""
    mgr = HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{host}:{http_port}/", username, password)
    return build_opener(HTTPDigestAuthHandler(mgr))


def decode_cgi_text(raw: bytes) -> str:
    """Decode a CGI response.

    Current Dahua firmware returns UTF-8, which matters for non-Latin zone
    names (for example Greek). Fall back to Latin-1, which never fails, for
    older firmware.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_alarm_config(text: str) -> dict[int, dict[str, str]]:
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


def fetch_alarm_config(
    host: str, http_port: int, username: str, password: str
) -> dict[int, dict[str, str]]:
    url = f"http://{host}:{http_port}/cgi-bin/configManager.cgi?action=getConfig&name=Alarm"
    opener = digest_opener(host, http_port, username, password)
    with opener.open(url, timeout=15) as response:
        text = decode_cgi_text(response.read())
    return parse_alarm_config(text)


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
