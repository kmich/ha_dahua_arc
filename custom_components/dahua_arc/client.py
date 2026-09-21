"""Stable client boundary for the Dahua ARC Home Assistant adapter.

The reverse-engineered DHIP/CGI implementation still lives in ``api.py`` for
now.  HA platforms import from this module so the protocol implementation can
be moved to a standalone library later without touching entity code.
"""

from __future__ import annotations

from .api import ArcHub, Zone, probe_connection

__all__ = ["ArcHub", "Zone", "probe_connection"]
