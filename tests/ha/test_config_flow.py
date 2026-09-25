"""Exercise real Home Assistant flow plumbing with the hardware probe mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.dahua_arc.const import (
    CONF_ARC_SERIAL,
    CONF_AREA_MATCH_AREAS,
    CONF_AREA_MATCH_THRESHOLD,
    CONF_AUTO_AREA_MATCH,
    CONF_DHIP_PORT,
    CONF_ENABLE_RESEARCH_FEATURES,
    CONF_HTTP_PORT,
    CONF_PERIODIC_RESYNC,
    CONF_REMATCH_EXISTING,
    CONF_ZONE_AREA_DECISIONS,
    DOMAIN,
)
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from pytest_homeassistant_custom_component.common import MockConfigEntry

CONNECTION = {
    CONF_HOST: "192.0.2.10",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "test-only",
    CONF_HTTP_PORT: 80,
    CONF_DHIP_PORT: 5000,
}
PROBE = {"serial_number": "ARC-TEST-001", "area_match_items": []}
BEHAVIOR = {
    CONF_PERIODIC_RESYNC: 300,
    CONF_AUTO_AREA_MATCH: False,
    CONF_ENABLE_RESEARCH_FEATURES: False,
}


@pytest.fixture(autouse=True)
def mock_hub_setup():
    """Config-flow tests must never open a real ARC socket on entry creation."""
    with patch(
        "custom_components.dahua_arc.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        yield


async def _start_user(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [(OSError("offline"), "cannot_connect"), (LoginError("bad auth"), "invalid_auth")],
)
async def test_connection_errors(
    hass: HomeAssistant, error: Exception, code: str
) -> None:
    form = await _start_user(hass)
    with patch(
        "custom_components.dahua_arc.config_flow._validate",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], CONNECTION
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": code}


async def test_user_setup_serial_and_duplicate(hass: HomeAssistant) -> None:
    form = await _start_user(hass)
    with patch(
        "custom_components.dahua_arc.config_flow._validate",
        AsyncMock(return_value=PROBE),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], CONNECTION
        )
        assert result["step_id"] == "behavior"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BEHAVIOR
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ARC_SERIAL] == "ARC-TEST-001"
        assert result["options"][CONF_ENABLE_RESEARCH_FEATURES] is False
        again = await _start_user(hass)
        duplicate = await hass.config_entries.flow.async_configure(
            again["flow_id"], CONNECTION
        )
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"


async def test_serial_required_for_new_entry(hass: HomeAssistant) -> None:
    form = await _start_user(hass)
    with patch(
        "custom_components.dahua_arc.config_flow._validate", AsyncMock(return_value={})
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], CONNECTION
        )
    assert result["errors"] == {"base": "serial_unavailable"}


async def test_legacy_reauth_preserves_unique_id(hass: HomeAssistant) -> None:
    legacy = MockConfigEntry(
        domain=DOMAIN, unique_id="192.0.2.10:5000", data=CONNECTION, version=4
    )
    legacy.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": legacy.entry_id},
        data=legacy.data,
    )
    with patch(
        "custom_components.dahua_arc.config_flow._validate",
        AsyncMock(return_value=PROBE),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "new-test-only"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert legacy.unique_id == "192.0.2.10:5000"
    assert legacy.data[CONF_ARC_SERIAL] == "ARC-TEST-001"
    assert legacy.data[CONF_PASSWORD] == "new-test-only"


async def test_reconfigure_refuses_wrong_serial(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ARC-TEST-001",
        data={**CONNECTION, CONF_ARC_SERIAL: "ARC-TEST-001"},
        version=5,
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    with patch(
        "custom_components.dahua_arc.config_flow._validate",
        AsyncMock(return_value={"serial_number": "WRONG"}),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], CONNECTION
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ARC_SERIAL] == "ARC-TEST-001"


async def test_options_flow_defaults_research_off(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="ARC-TEST-001", data=CONNECTION, version=5
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], BEHAVIOR
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENABLE_RESEARCH_FEATURES] is False


def _schema_defaults(result: dict) -> dict:
    return {
        str(key): key.default()
        for key in result["data_schema"].schema
        if callable(getattr(key, "default", None))
    }


async def test_reconfigure_never_echoes_password_and_blank_keeps_it(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ARC-TEST-001",
        data={**CONNECTION, CONF_ARC_SERIAL: "ARC-TEST-001"},
        version=5,
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    defaults = _schema_defaults(form)
    assert defaults[CONF_HOST] == "192.0.2.10"
    assert CONF_PASSWORD not in defaults
    assert "test-only" not in str(form)

    new_host = {**CONNECTION, CONF_HOST: "192.0.2.20"}
    del new_host[CONF_PASSWORD]
    with patch(
        "custom_components.dahua_arc.config_flow._validate",
        AsyncMock(return_value=PROBE),
    ) as validate:
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], new_host
        )
    assert result["reason"] == "reconfigure_successful"
    assert validate.await_args.args[1][CONF_PASSWORD] == "test-only"
    assert entry.data[CONF_HOST] == "192.0.2.20"
    assert entry.data[CONF_PASSWORD] == "test-only"


async def test_options_area_match_keeps_existing_decisions_by_default(
    hass: HomeAssistant,
) -> None:
    kitchen = ar.async_get(hass).async_create("Kitchen")
    office = ar.async_get(hass).async_create("Office")
    stored = {
        "7": {
            "area_id": kitchen.id,
            "score": 100,
            "reason": "manual review",
            "zone_name": "Office Window",
        }
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ARC-TEST-001",
        data=CONNECTION,
        options={CONF_ZONE_AREA_DECISIONS: stored},
        version=5,
    )
    entry.add_to_hass(hass)

    async def run(rematch: bool) -> dict:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**BEHAVIOR, CONF_AUTO_AREA_MATCH: True}
        )
        assert result["step_id"] == "area_match"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_AREA_MATCH_AREAS: [kitchen.id, office.id],
                CONF_AREA_MATCH_THRESHOLD: 90,
                CONF_REMATCH_EXISTING: rematch,
            },
        )
        assert result["step_id"] == "area_preview"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        return result["data"]

    # Entry not loaded: stored decisions are neither wiped nor rematched.
    data = await run(rematch=False)
    assert data[CONF_ZONE_AREA_DECISIONS]["7"]["area_id"] == kitchen.id
    assert CONF_REMATCH_EXISTING not in data

    # Explicit re-evaluation recomputes from the recorded zone name.
    data = await run(rematch=True)
    assert data[CONF_ZONE_AREA_DECISIONS]["7"]["area_id"] == office.id
