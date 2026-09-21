from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub, Zone
from .const import (
    CONF_ENABLE_RESEARCH_FEATURES,
    DEFAULT_ENABLE_RESEARCH_FEATURES,
    DOMAIN,
)
from .inventory import RadioDeviceInfo

_LOGGER = logging.getLogger(__name__)
_TARGET_NAME = "PIRCamera Staircase GF"


def _radio_identifier(uid: str, device: RadioDeviceInfo) -> tuple[str, str]:
    return (DOMAIN, f"{uid}:{device.device_key}")


def _device_info_for_zone(
    hub: ArcHub,
    uid: str,
    zone: Zone,
) -> DeviceInfo:
    radio = hub.radio_device_for_zone(zone)
    if radio is not None:
        return DeviceInfo(
            identifiers={_radio_identifier(uid, radio)},
            name=radio.name,
            manufacturer="Dahua",
            model=radio.model or radio.sense_method or "ARC radio device",
            serial_number=radio.serial,
        )
    return DeviceInfo(identifiers={(DOMAIN, uid)})


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
    target = next(
        (
            zone
            for zone in hub.zones.values()
            if zone.name == _TARGET_NAME and zone.sense_method == "PIRCam"
        ),
        None,
    )
    if target is None:
        _LOGGER.warning(
            "Detector-test research buttons not added: %s PIRCam not found",
            _TARGET_NAME,
        )
        return

    async_add_entities(
        [
            DahuaArcDetectorTestButton(hub, entry, target, True),
            DahuaArcDetectorTestButton(hub, entry, target, False),
        ]
    )


class DahuaArcDetectorTestButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        zone: Zone,
        start: bool,
    ) -> None:
        self.hub = hub
        self.zone = zone
        self.start = start

        uid = entry.unique_id or entry.entry_id
        suffix = "start_detector_test" if start else "stop_detector_test"
        self._attr_unique_id = f"{uid}_pircam_{zone.index}_{suffix}"
        self._attr_name = "Start detector test" if start else "Stop detector test"
        self._attr_device_info = _device_info_for_zone(hub, uid, zone)

    @property
    def available(self) -> bool:
        return self.hub.available

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        status = self.hub.detector_test_status()
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
        try:
            if self.start:
                await self.hass.async_add_executor_job(self.hub.start_detector_test)
            else:
                await self.hass.async_add_executor_job(self.hub.stop_detector_test)
        except Exception as exc:
            raise HomeAssistantError(
                f"Dahua ARC detector-test command failed: {exc}"
            ) from exc
