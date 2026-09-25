"""Home Assistant fixture setup for integration runtime tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def custom_integrations_enabled(enable_custom_integrations: None) -> None:
    """Load the repository integration in the HA test instance."""
