"""Research-only PIRCam detector-test buttons.

These are the only entities that write to the ARC: they toggle the
accessory's SensitivityTest flag and nothing else (no arm/disarm, no sirens).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub, Zone
from .const import CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
from .entity import DahuaArcEntity, zone_radio_device_info

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ArcHub],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.options.get(
        CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
    ):
        return

    hub = entry.runtime_data
    if hub.detector_test is None:
        return
    async_add_entities(
        DahuaArcDetectorTestButton(hub, entry, zone, start)
        for zone in hub.detector_test.targets.values()
        for start in (True, False)
    )


class DahuaArcDetectorTestButton(DahuaArcEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _unrecorded_attributes = frozenset(
        {
            "successful_style",
            "timer_active",
            "last_action",
            "last_success",
            "last_error",
            "last_factory_object",
            "last_destroy_result",
        }
    )

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        zone: Zone,
        start: bool,
    ) -> None:
        super().__init__(hub, entry)
        self.zone = zone
        self.start = start
        self._watched_indices = frozenset({zone.index})
        suffix = "start_detector_test" if start else "stop_detector_test"
        self._attr_unique_id = f"{self._uid}_pircam_{zone.index}_{suffix}"
        self._attr_translation_key = suffix
        self._attr_device_info = zone_radio_device_info(hub, self._uid, zone)

    @property
    def available(self) -> bool:
        return self.hub.available

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        status = self.hub.detector_test_status(self.zone.index)
        return {
            "target": status.get("target_name"),
            "enabled": status.get("enabled"),
            "successful_style": status.get("successful_style"),
            "auto_stop_seconds": status.get("auto_stop_seconds"),
            "timer_active": status.get("timer_active"),
            "last_action": status.get("last_action"),
            "last_success": status.get("last_success"),
            "last_error": status.get("last_error"),
            "last_factory_object": status.get("last_factory_object"),
            "last_destroy_result": status.get("last_destroy_result"),
        }

    async def async_press(self) -> None:
        action = (
            self.hub.start_detector_test if self.start else self.hub.stop_detector_test
        )
        try:
            await self.hass.async_add_executor_job(action, self.zone.index)
        except Exception as exc:
            raise HomeAssistantError(
                f"Dahua ARC detector-test command failed: {exc}"
            ) from exc
