from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import DahuaArcConfigEntry
from .const import (
    CONF_ARC_SERIAL,
    CONF_ARM_CODE_HASH,
    CONF_ARM_MODES,
    CONF_CODE_ARM_REQUIRED,
    CONF_CODE_DISARM_REQUIRED,
    CONF_ENABLE_ARM_CONTROL,
    CONF_ZONE_AREA_OWNERSHIP,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DahuaArcConfigEntry
) -> dict[str, Any]:
    # Refresh the extra reverse-engineering tables only when the user explicitly
    # enabled the research features. The production path keeps diagnostics cheap.
    if getattr(entry.runtime_data, "enable_research_features", False):
        await hass.async_add_executor_job(entry.runtime_data.refresh_research_inventory)
    # A few read-only reads that capture the arm-state tables as they are at
    # download time, so a disarmed/armed pair of downloads can be compared.
    await hass.async_add_executor_job(entry.runtime_data.refresh_arm_state_probe)
    return {
        "entry": async_redact_data(
            dict(entry.data), {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_ARC_SERIAL}
        ),
        # The code and its hash are never exported, only whether one is set.
        "arm_control_options": {
            "enabled": bool(entry.options.get(CONF_ENABLE_ARM_CONTROL)),
            "code_set": bool(entry.options.get(CONF_ARM_CODE_HASH)),
            "code_disarm_required": entry.options.get(CONF_CODE_DISARM_REQUIRED),
            "code_arm_required": entry.options.get(CONF_CODE_ARM_REQUIRED),
            "modes": entry.options.get(CONF_ARM_MODES),
        },
        "area_ownership": dict(entry.options.get(CONF_ZONE_AREA_OWNERSHIP, {})),
        "runtime": entry.runtime_data.diagnostics(),
    }
