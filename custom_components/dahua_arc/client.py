"""Stable client boundary for the Dahua ARC Home Assistant adapter.

Home Assistant platforms import protocol types from this module only, so the
HA-independent :mod:`.protocol` package can later move to a standalone
library without touching entity code.
"""

from __future__ import annotations

from .hub import ArcHub, probe_connection
from .protocol.inventory import RadioDeviceInfo
from .protocol.models import Zone

__all__ = ["ArcHub", "RadioDeviceInfo", "Zone", "probe_connection"]
