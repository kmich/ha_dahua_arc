from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_ENABLE_RESEARCH_FEATURES,
    DEFAULT_ENABLE_RESEARCH_FEATURES,
    DOMAIN,
    ISSUE_RESEARCH_ENABLED,
)


class DisableResearchFeaturesRepairFlow(RepairsFlow):
    """Repair flow that disables experimental research features."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))

        entry_id = str((self.data or {}).get("entry_id") or "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        options = dict(entry.options)
        options[CONF_ENABLE_RESEARCH_FEATURES] = DEFAULT_ENABLE_RESEARCH_FEATURES
        self.hass.config_entries.async_update_entry(entry, options=options)
        await self.hass.config_entries.async_reload(entry.entry_id)
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for Dahua ARC issues."""
    if issue_id.endswith(ISSUE_RESEARCH_ENABLED):
        return DisableResearchFeaturesRepairFlow()
    raise ValueError(f"Unknown Dahua ARC repair issue: {issue_id}")
