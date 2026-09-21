"""Dahua ARC integration exceptions."""

from __future__ import annotations


class DahuaArcError(Exception):
    """Base exception for Dahua ARC integration errors."""


class DahuaArcCannotConnect(DahuaArcError):
    """Raised when the ARC hub cannot be reached."""


class DahuaArcAuthError(DahuaArcError):
    """Raised when the ARC hub rejects credentials."""
