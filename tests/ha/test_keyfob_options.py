"""The options flow for the keyfob arming backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.dahua_arc import arm_code
from custom_components.dahua_arc.config_flow import _arm_keyfob_schema
from custom_components.dahua_arc.const import (
    CONF_ARM_BACKEND,
    CONF_AUTO_AREA_MATCH,
    CONF_KEYFOB_ALLOW_DISARM,
    CONF_KEYFOB_AWAY_ENTITY,
    CONF_KEYFOB_DISARM_ENTITY,
    CONF_KEYFOB_HOME_ENTITY,
    CONF_KEYFOB_PRESS_MS,
    CONF_KEYFOB_WAIT_SECONDS,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar
from tests.ha.test_config_flow import (
    ARM_FORM,
    BEHAVIOR,
    _options_entry,
    _schema_defaults,
    _to_arm_step,
)

KEYFOB_FORM = {**ARM_FORM, CONF_ARM_BACKEND: "keyfob", "acknowledge_control": True}
RELAYS = {
    CONF_KEYFOB_HOME_ENTITY: "button.keyfob_home",
    CONF_KEYFOB_AWAY_ENTITY: "button.keyfob_away",
    CONF_KEYFOB_ALLOW_DISARM: False,
    CONF_KEYFOB_PRESS_MS: 400,
    CONF_KEYFOB_WAIT_SECONDS: 15,
}


@pytest.fixture(autouse=True)
def mock_hub_setup():
    """Saving options reloads the entry; never open a real ARC socket."""
    with patch(
        "custom_components.dahua_arc.async_setup_entry", AsyncMock(return_value=True)
    ):
        yield


async def _to_keyfob_step(hass: HomeAssistant, entry, form: dict | None = None):
    result = await _to_arm_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form or KEYFOB_FORM
    )
    assert result["step_id"] == "arm_keyfob"
    return result


async def test_the_keyfob_backend_leads_to_the_relay_screen(
    hass: HomeAssistant,
) -> None:
    entry = _options_entry(hass)
    result = await _to_arm_step(hass, entry)
    assert _schema_defaults(result)[CONF_ARM_BACKEND] == "dhip"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], KEYFOB_FORM
    )
    assert result["step_id"] == "arm_keyfob"
    defaults = _schema_defaults(result)
    assert defaults[CONF_KEYFOB_ALLOW_DISARM] is False
    assert defaults[CONF_KEYFOB_PRESS_MS] == 400
    assert defaults[CONF_KEYFOB_WAIT_SECONDS] == 15


def test_there_is_no_field_for_the_sos_button() -> None:
    fields = {str(key) for key in _arm_keyfob_schema({}).schema}
    assert fields == {
        CONF_KEYFOB_HOME_ENTITY,
        CONF_KEYFOB_AWAY_ENTITY,
        CONF_KEYFOB_DISARM_ENTITY,
        CONF_KEYFOB_ALLOW_DISARM,
        CONF_KEYFOB_PRESS_MS,
        CONF_KEYFOB_WAIT_SECONDS,
    }
    assert not any("sos" in field.lower() for field in fields)


async def test_the_default_backend_skips_the_relay_screen(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_arm_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ARM_FORM, "acknowledge_control": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ARM_BACKEND] == "dhip"


async def test_every_offered_mode_needs_its_relay_entity(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry)
    form = {**RELAYS, CONF_KEYFOB_HOME_ENTITY: None}
    form.pop(CONF_KEYFOB_HOME_ENTITY)
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["step_id"] == "arm_keyfob"
    assert result["errors"] == {CONF_KEYFOB_HOME_ENTITY: "keyfob_entity_required"}
    assert entry.options == {}


async def test_a_mode_that_is_not_offered_needs_no_entity(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    form = {**KEYFOB_FORM, "arm_modes": ["armed_home"]}
    result = await _to_keyfob_step(hass, entry, form)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {k: v for k, v in RELAYS.items() if k != CONF_KEYFOB_AWAY_ENTITY},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_KEYFOB_AWAY_ENTITY not in result["data"]


async def test_allowing_disarm_needs_a_disarm_entity(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**RELAYS, CONF_KEYFOB_ALLOW_DISARM: True}
    )
    assert result["errors"] == {
        CONF_KEYFOB_DISARM_ENTITY: "keyfob_disarm_entity_required"
    }


async def test_the_relay_options_are_stored(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **RELAYS,
            CONF_KEYFOB_ALLOW_DISARM: True,
            CONF_KEYFOB_DISARM_ENTITY: "script.keyfob_disarm",
            CONF_KEYFOB_PRESS_MS: 250,
            CONF_KEYFOB_WAIT_SECONDS: 20,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_ARM_BACKEND] == "keyfob"
    assert data[CONF_KEYFOB_HOME_ENTITY] == "button.keyfob_home"
    assert data[CONF_KEYFOB_AWAY_ENTITY] == "button.keyfob_away"
    assert data[CONF_KEYFOB_DISARM_ENTITY] == "script.keyfob_disarm"
    assert data[CONF_KEYFOB_ALLOW_DISARM] is True
    assert data[CONF_KEYFOB_PRESS_MS] == 250
    assert data[CONF_KEYFOB_WAIT_SECONDS] == 20


async def test_a_disarm_entity_is_forgotten_when_disarm_is_not_allowed(
    hass: HomeAssistant,
) -> None:
    entry = _options_entry(hass, {CONF_KEYFOB_DISARM_ENTITY: "button.old_disarm"})
    result = await _to_keyfob_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**RELAYS, CONF_KEYFOB_DISARM_ENTITY: "button.keyfob_disarm"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_KEYFOB_DISARM_ENTITY not in result["data"]
    assert result["data"][CONF_KEYFOB_ALLOW_DISARM] is False


async def test_only_pressable_entity_domains_are_accepted(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**RELAYS, CONF_KEYFOB_HOME_ENTITY: "light.kitchen"}
        )


async def test_press_length_and_wait_are_bounded(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry)
    for key, value in (
        (CONF_KEYFOB_PRESS_MS, 50),
        (CONF_KEYFOB_PRESS_MS, 5000),
        (CONF_KEYFOB_WAIT_SECONDS, 2),
        (CONF_KEYFOB_WAIT_SECONDS, 300),
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {**RELAYS, key: value}
            )


async def test_the_relay_screen_chains_into_area_matching(hass: HomeAssistant) -> None:
    ar.async_get(hass).async_create("Kitchen")
    entry = _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**BEHAVIOR, CONF_AUTO_AREA_MATCH: True, "enable_arm_control": True},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], KEYFOB_FORM
    )
    assert result["step_id"] == "arm_keyfob"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], RELAYS
    )
    assert result["step_id"] == "area_match"


async def test_the_code_is_still_hashed_on_the_keyfob_path(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await _to_keyfob_step(hass, entry, {**KEYFOB_FORM, "arm_code": "4321"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], RELAYS
    )
    data = result["data"]
    assert arm_code.verify_code("4321", data["arm_code_hash"])
    assert "4321" not in str(data)
