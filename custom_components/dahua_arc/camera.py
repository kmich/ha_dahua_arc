"""Research-only PIRCam still-image camera."""

from __future__ import annotations

from typing import override

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:
    from homeassistant.helpers.entity_platform import (
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,  # type: ignore[attr-defined]
    )

from .client import ArcHub, Zone
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
    "last_alarm_time",
    "media_type",
    "file_path",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ArcHub],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
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
        attrs: dict[str, object] = {
            key: meta[key] for key in _ATTRIBUTES if meta.get(key) is not None
        }
        if "last_alarm_time" not in attrs and meta.get("last_alarm_at"):
            attrs["last_alarm_time"] = meta["last_alarm_at"]
        if "media_type" not in attrs:
            media_type = meta.get("last_alarm_media_kind") or meta.get("media_kind")
            if media_type:
                attrs["media_type"] = media_type
        if "file_path" not in attrs:
            file_path = (
                meta.get("last_alarm_temp_path")
                or meta.get("temp_path")
                or meta.get("file_path")
                or meta.get("last_alarm_file_path")
            )
            if file_path:
                attrs["file_path"] = file_path
        attrs["has_snapshot"] = bool(self.hub.fetch_pircam_image(self.zone.index))
        return attrs

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        raw = await self.hass.async_add_executor_job(
            self.hub.fetch_pircam_image, self.zone.index
        )
        if raw:
            return raw
        label = self.zone.name or f"PIR Camera {self.zone.index}"
        return await self.hass.async_add_executor_job(
            self.hub.pircam_placeholder_image, label
        )
