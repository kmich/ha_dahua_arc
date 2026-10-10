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


# -- arm control options ------------------------------------------------------

ARM_FORM = {
    "code_disarm_required": True,
    "code_arm_required": False,
    "arm_modes": ["armed_home", "armed_away"],
    "clear_arm_code": False,
}


def _options_entry(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ARC-TEST-001",
        data=CONNECTION,
        options=options or {},
        version=5,
    )
    entry.add_to_hass(hass)
    return entry


async def _to_arm_step(hass: HomeAssistant, entry) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BEHAVIOR, "enable_arm_control": True}
    )
    assert result["step_id"] == "arm_control"
    return result


def test_first_time_setup_does_not_offer_arm_control() -> None:
    from custom_components.dahua_arc.config_flow import _behavior_schema

    first = {str(key) for key in _behavior_schema({}).schema}
    options = {
        str(key) for key in _behavior_schema({}, include_arm_control=True).schema
    }
    assert "enable_arm_control" not in first
    assert options - first == {"enable_arm_control"}


async def test_options_offer_arm_control_off_by_default(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_defaults(form)["enable_arm_control"] is False
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], BEHAVIOR
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["enable_arm_control"] is False
    assert "arm_code_hash" not in result["data"]


async def test_enabling_arm_control_requires_acknowledgement_and_a_valid_code(
    hass: HomeAssistant,
) -> None:
    entry = _options_entry(hass)
    result = await _to_arm_step(hass, entry)
    flow_id = result["flow_id"]

    result = await hass.config_entries.options.async_configure(flow_id, ARM_FORM)
    assert result["step_id"] == "arm_control"
    assert result["errors"] == {"base": "acknowledge_required"}

    bad = {**ARM_FORM, "arm_code": "12", "acknowledge_control": True}
    result = await hass.config_entries.options.async_configure(flow_id, bad)
    assert result["errors"] == {"arm_code": "invalid_code_format"}

    bad = {**ARM_FORM, "arm_modes": [], "acknowledge_control": True}
    result = await hass.config_entries.options.async_configure(flow_id, bad)
    assert result["errors"] == {"arm_modes": "no_arm_modes"}
    assert "arm_code" not in _schema_defaults(result)  # nothing is pre-filled
    assert entry.options == {}


async def test_the_code_is_stored_only_as_a_hash(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_arm_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**ARM_FORM, "arm_code": "4321", "acknowledge_control": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    stored = data["arm_code_hash"]
    from custom_components.dahua_arc import arm_code

    assert arm_code.verify_code("4321", stored)
    assert "4321" not in str(data)
    for field in ("arm_code", "clear_arm_code", "acknowledge_control"):
        assert field not in data
    assert data["enable_arm_control"] is True
    assert data["arm_control_acknowledged"] is True
    assert data["code_disarm_required"] is True
    assert data["code_arm_required"] is False
    assert data["arm_modes"] == ["armed_home", "armed_away"]


async def test_setting_a_code_clears_the_no_code_acknowledgement(
    hass: HomeAssistant,
) -> None:
    entry = _options_entry(
        hass, {"arm_control_acknowledged": True, "arm_code_ack_no_code": True}
    )
    result = await _to_arm_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ARM_FORM, "arm_code": "4321"}
    )
    assert "arm_code_ack_no_code" not in result["data"]


async def test_blank_code_keeps_the_hash_and_clear_removes_it(
    hass: HomeAssistant,
) -> None:
    from custom_components.dahua_arc import arm_code

    stored = arm_code.hash_code("4321")
    entry = _options_entry(
        hass,
        {
            "enable_arm_control": True,
            "arm_control_acknowledged": True,
            "arm_code_hash": stored,
        },
    )
    result = await _to_arm_step(hass, entry)
    # Already acknowledged: no checkbox, and the hash is never shown.
    assert "acknowledge_control" not in str(result["data_schema"].schema)
    assert stored not in str(result)
    kept = await hass.config_entries.options.async_configure(
        result["flow_id"], ARM_FORM
    )
    assert kept["data"]["arm_code_hash"] == stored

    result = await _to_arm_step(hass, entry)
    cleared = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ARM_FORM, "clear_arm_code": True}
    )
    assert "arm_code_hash" not in cleared["data"]


async def test_turning_arm_control_off_skips_its_screen(hass: HomeAssistant) -> None:
    entry = _options_entry(
        hass, {"enable_arm_control": True, "arm_control_acknowledged": True}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_defaults(result)["enable_arm_control"] is True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BEHAVIOR, "enable_arm_control": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["enable_arm_control"] is False


async def test_arm_control_step_chains_into_area_matching(
    hass: HomeAssistant,
) -> None:
    ar.async_get(hass).async_create("Kitchen")
    entry = _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**BEHAVIOR, CONF_AUTO_AREA_MATCH: True, "enable_arm_control": True},
    )
    assert result["step_id"] == "arm_control"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ARM_FORM, "acknowledge_control": True}
    )
    assert result["step_id"] == "area_match"
