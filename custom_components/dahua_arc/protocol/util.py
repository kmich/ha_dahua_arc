"""Small parsing helpers shared by the protocol modules."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def timestamp() -> str:
    """Return the current local time as a timezone-aware ISO-8601 string."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a value produced by :func:`timestamp`."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


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
    """Map the ARC raw alarm state to an HA binary state (1=Alarm, 5=Normal)."""
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


def keepalive_delay(interval: int) -> int:
    """Send keepalives slightly before the ARC session interval expires."""
    return interval - 2 if interval > 5 else max(1, interval)
