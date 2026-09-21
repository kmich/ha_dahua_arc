"""Public data models used by Home Assistant entity platforms."""

from __future__ import annotations

from .api import Zone
from .inventory import RadioDeviceInfo

__all__ = ["RadioDeviceInfo", "Zone"]
