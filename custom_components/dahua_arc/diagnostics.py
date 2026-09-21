from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import DahuaArcConfigEntry
from .const import CONF_ARC_SERIAL, CONF_ZONE_AREA_OWNERSHIP


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DahuaArcConfigEntry
) -> dict[str, Any]:
    # Refresh the extra reverse-engineering tables only when the user explicitly
    # enabled the research features. The production path keeps diagnostics cheap.
    if getattr(entry.runtime_data, "enable_research_features", False):
        await hass.async_add_executor_job(entry.runtime_data.refresh_research_inventory)
    return {
        "entry": async_redact_data(
            dict(entry.data), {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_ARC_SERIAL}
        ),
        "area_ownership": dict(entry.options.get(CONF_ZONE_AREA_OWNERSHIP, {})),
        "runtime": entry.runtime_data.diagnostics(),
    }
