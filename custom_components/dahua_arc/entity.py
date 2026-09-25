"""Shared entity plumbing for Dahua ARC platforms."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import ArcHub, RadioDeviceInfo, Zone
from .const import DOMAIN


def entry_uid(entry: ConfigEntry) -> str:
    """Identity prefix for devices/entities (legacy entries use the entry id)."""
    return entry.unique_id or entry.entry_id


def radio_identifier(uid: str, device: RadioDeviceInfo) -> tuple[str, str]:
    return (DOMAIN, f"{uid}:{device.device_key}")


def root_device_info(hub: ArcHub, uid: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=hub.device_type,
        manufacturer="Dahua",
        model=hub.device_type,
        sw_version=hub.software_version,
        serial_number=hub.serial_number,
        configuration_url=hub.configuration_url,
    )


def radio_device_info(uid: str, device: RadioDeviceInfo) -> DeviceInfo:
    return DeviceInfo(
        identifiers={radio_identifier(uid, device)},
        name=device.name,
        manufacturer="Dahua",
        model=device.model or device.sense_method or "ARC radio device",
        serial_number=device.serial,
    )


def zone_radio_device_info(hub: ArcHub, uid: str, zone: Zone) -> DeviceInfo:
    """The physical radio device of a zone, falling back to the ARC itself."""
    radio = hub.radio_device_for_zone(zone)
    if radio is None:
        return DeviceInfo(identifiers={(DOMAIN, uid)})
    return radio_device_info(uid, radio)


class DahuaArcEntity(Entity):
    """Push-updated entity bound to one :class:`ArcHub`.

    Hub callbacks arrive on worker threads with the changed Alarm[] indexes,
    or ``None`` for hub-wide availability/health changes. An entity redraws
    only for hub-wide changes and for the indexes in ``_watched_indices``.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _watched_indices: frozenset[int] = frozenset()

    def __init__(self, hub: ArcHub, entry: ConfigEntry) -> None:
        self.hub = hub
        self._uid = entry_uid(entry)
        self._listening = False

    def _is_relevant(self, indices: set[int] | None) -> bool:
        return indices is None or not self._watched_indices.isdisjoint(indices)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listening = True

        def listener(indices: set[int] | None) -> None:
            if self._listening and self._is_relevant(indices):
                self.hass.loop.call_soon_threadsafe(self._async_hub_updated)

        self.async_on_remove(self.hub.add_listener(listener))

    async def async_will_remove_from_hass(self) -> None:
        self._listening = False
        await super().async_will_remove_from_hass()

    @callback
    def _async_hub_updated(self) -> None:
        # The entity may have been removed between scheduling and running.
        if self._listening:
            self.async_write_ha_state()
