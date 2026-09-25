"""Research-only PIRCam still-image camera."""

from __future__ import annotations

from typing import override

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub, Zone
from .const import CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
from .entity import DahuaArcEntity, zone_radio_device_info

PARALLEL_UPDATES = 1

_ATTRIBUTES = (
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
    async_add_entities(
        DahuaArcPirCamera(hub, entry, zone)
        for zone in hub.primary_zones.values()
        if zone.sense_method == "PIRCam"
    )


class DahuaArcPirCamera(DahuaArcEntity, Camera):
    """Latest still image produced by a Dahua ARD1731 PIR camera."""

    _attr_translation_key = "latest_snapshot"
    _attr_is_streaming = False
    # Fetch diagnostics change on every image request.
    _unrecorded_attributes = frozenset(_ATTRIBUTES)

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], zone: Zone) -> None:
        DahuaArcEntity.__init__(self, hub, entry)
        Camera.__init__(self)
        self.zone = zone
        self._watched_indices = frozenset({zone.index})
        self._attr_unique_id = f"{self._uid}_pircam_{zone.index}_latest_snapshot"
        self._attr_device_info = zone_radio_device_info(hub, self._uid, zone)
        self.content_type = "image/jpeg"

    @property
    def available(self) -> bool:
        return self.hub.available and self.zone.online_state != 0

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        meta = self.hub.pircam_snapshot(self.zone.index) or {}
        return {key: meta[key] for key in _ATTRIBUTES if meta.get(key) is not None}

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.hass.async_add_executor_job(
            self.hub.fetch_pircam_image, self.zone.index
        )
