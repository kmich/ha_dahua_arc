from __future__ import annotations

from typing import override

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub, Zone
from .const import (
    CONF_ENABLE_RESEARCH_FEATURES,
    DEFAULT_ENABLE_RESEARCH_FEATURES,
    DOMAIN,
)
from .inventory import RadioDeviceInfo


def _radio_identifier(uid: str, device: RadioDeviceInfo) -> tuple[str, str]:
    return (DOMAIN, f"{uid}:{device.device_key}")


def _device_info_for_pircam(hub: ArcHub, uid: str, zone: Zone) -> DeviceInfo:
    radio = hub.radio_device_for_zone(zone)
    if radio is None:
        return DeviceInfo(identifiers={(DOMAIN, uid)})
    return DeviceInfo(
        identifiers={_radio_identifier(uid, radio)},
        name=radio.name,
        manufacturer="Dahua",
        model=radio.model or radio.sense_method or "PIR camera",
        serial_number=radio.serial,
    )


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
    cams = [
        DahuaArcPirCamera(hub, entry, zone)
        for zone in hub.primary_zones.values()
        if zone.sense_method == "PIRCam"
    ]
    async_add_entities(cams)


class DahuaArcPirCamera(Camera):
    """Latest still image produced by a Dahua ARD1731 PIR camera."""

    _attr_has_entity_name = True
    _attr_name = "Latest snapshot"
    _attr_should_poll = False
    _attr_is_streaming = False

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], zone: Zone) -> None:
        super().__init__()
        self.hub = hub
        self.zone = zone
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_pircam_{zone.index}_latest_snapshot"
        self._attr_device_info = _device_info_for_pircam(hub, uid, zone)
        self.content_type = "image/jpeg"

    @property
    def available(self) -> bool:
        return self.hub.available and self.zone.online_state not in (0,)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        meta = self.hub.pircam_snapshot(self.zone.index) or {}
        keep = (
            "captured_at",
            "picture_count",
            "upload_ready_at",
            "expected_length",
            "last_fetch_at",
            "last_fetch_method",
            "last_fetch_path",
            "last_fetch_bytes",
            "last_fetch_error",
            "last_fetch_fallback",
            "length_mismatch",
            "last_alarm_at",
            "last_alarm_media_kind",
            "last_alarm_video_count",
            "last_alarm_expected_length",
            "last_alarm_upload_ready_at",
        )
        return {key: meta[key] for key in keep if meta.get(key) is not None}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def listener(indices: set[int] | None) -> None:
            if indices is None or self.zone.index in indices:
                self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

        self.async_on_remove(self.hub.add_listener(listener))

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.hass.async_add_executor_job(
            self.hub.fetch_pircam_image, self.zone.index
        )
