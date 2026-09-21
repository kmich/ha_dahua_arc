from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub, Zone
from .const import DOMAIN
from .inventory import RadioDeviceInfo

_LOGGER = logging.getLogger(__name__)
_GLASS_BREAK_DEVICE_CLASS = getattr(
    BinarySensorDeviceClass, "GLASS_BREAK", BinarySensorDeviceClass.VIBRATION
)


def _device_class(zone: Zone) -> BinarySensorDeviceClass | None:
    name = zone.name.lower()
    cls = zone.classification
    if cls in {"motion", "radar"} or "pir" in name or "motion" in name:
        return BinarySensorDeviceClass.MOTION
    if cls == "smoke_or_fire" or "smoke" in name:
        return BinarySensorDeviceClass.SMOKE
    if cls == "flood_or_water" or "flood" in name or "water" in name:
        return BinarySensorDeviceClass.MOISTURE
    if cls == "glass_break" or "glass break" in name:
        return _GLASS_BREAK_DEVICE_CLASS
    if "garage door" in name:
        return BinarySensorDeviceClass.GARAGE_DOOR
    if "door" in name:
        return BinarySensorDeviceClass.DOOR
    if "window" in name or "shutter" in name:
        return BinarySensorDeviceClass.WINDOW
    return BinarySensorDeviceClass.OPENING


def _radio_identifier(uid: str, device: RadioDeviceInfo) -> tuple[str, str]:
    return (DOMAIN, f"{uid}:{device.device_key}")


def _device_info_for_radio(
    hub: ArcHub, uid: str, device: RadioDeviceInfo
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={_radio_identifier(uid, device)},
        name=device.name,
        manufacturer="Dahua",
        model=device.model or device.sense_method or "ARC radio device",
        serial_number=device.serial,
    )


def _device_info_for_zone(
    hub: ArcHub, uid: str, zone: Zone
) -> DeviceInfo | ChildDeviceInfo:
    child_id = getattr(hub, "child_device_ids", {}).get(zone.index)
    if zone.is_multiio and child_id:
        parent = getattr(hub, "multiio_parent_ids", {}).get(zone.level1)
        if parent:
            return ChildDeviceInfo(
                identifiers={(DOMAIN, f"{uid}:zone:{zone.index}")},
                name=zone.name,
                parent_device_id=parent,
            )
    radio = hub.radio_device_for_zone(zone)
    if radio is not None:
        return _device_info_for_radio(hub, uid, radio)
    return DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=hub.device_type,
        manufacturer="Dahua",
        model=hub.device_type,
        sw_version=hub.software_version,
        serial_number=hub.serial_number,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ArcHub],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[BinarySensorEntity] = [DahuaArcConnectivity(hub, entry)]

    # Primary alarm sensors only. Peripheral rows such as sirens, keyfobs,
    # keypad and repeater remain real HA devices but are not misrepresented as
    # generic opening sensors.
    entities.extend(
        DahuaArcZoneBinarySensor(hub, entry, zone)
        for zone in hub.primary_zones.values()
    )

    # Every paired physical radio device gets health entities, including
    # MultiIO modules, PIR/PIRCam, repeater, sirens, keyfob and keypad.
    for device in hub.radio_devices.values():
        entities.append(DahuaArcRadioConnectivity(hub, entry, device))
        entities.append(DahuaArcRadioLowBattery(hub, entry, device))
        entities.append(DahuaArcRadioTamper(hub, entry, device))

    _LOGGER.info(
        "Adding %s Dahua ARC binary sensors (%s primary alarm sensors, %s radio devices)",
        len(entities),
        len(hub.primary_zones),
        len(hub.radio_devices),
    )
    async_add_entities(entities)


class DahuaArcBase(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: ArcHub):
        self.hub = hub

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def listener(indices: set[int] | None) -> None:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

        self.async_on_remove(self.hub.add_listener(listener))


class DahuaArcConnectivity(DahuaArcBase):
    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub]):
        super().__init__(hub)
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            name=hub.device_type,
            manufacturer="Dahua",
            model=hub.device_type,
            sw_version=hub.software_version,
            serial_number=hub.serial_number,
            configuration_url=f"http://{hub.host}",
        )

    @property
    def is_on(self) -> bool:
        return self.hub.available


class DahuaArcZoneBinarySensor(DahuaArcBase):
    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], zone: Zone):
        super().__init__(hub)
        self.zone = zone
        self.config_entry = entry
        decision = getattr(hub, "area_decisions", {}).get(str(zone.index), {})
        self._area_match_name = decision.get("area_id")
        self._area_match_score = decision.get("score")
        self._area_match_reason = decision.get("reason")
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_zone_{zone.index}"
        self._attr_name = zone.name
        self._attr_device_class = _device_class(zone)
        self._attr_device_info = _device_info_for_zone(hub, uid, zone)

    @property
    def is_on(self) -> bool | None:
        return self.zone.active

    @property
    def available(self) -> bool:
        return self.hub.available and self.zone.online_state != 0

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "alarm_index": self.zone.index,
            "multiio_input": self.zone.level2 if self.zone.is_multiio else None,
            "multiio_level1": self.zone.level1 if self.zone.is_multiio else None,
            "parent": self.zone.parent_name,
            "parent_serial": self.zone.parent_sn or None,
            "sense_method": self.zone.sense_method or None,
            "classification": self.zone.classification or None,
            "is_multiio": self.zone.is_multiio,
            "sensor_type": self.zone.sensor_type or None,
            "termination": self.zone.termination or None,
            "dahua_area": self.zone.area_hint or None,
            "online_state": self.zone.online_state,
            "raw_alarm_state": self.zone.raw_alarm_state,
            "tamper": self.zone.tamper,
            "low_power": self.zone.low_power,
            "last_source": self.zone.last_source or None,
            "last_action": self.zone.last_action or None,
            "last_changed": self.zone.last_changed or None,
            "smart_area_match": self._area_match_name,
            "smart_area_match_score": self._area_match_score,
            "smart_area_match_reason": self._area_match_reason,
        }


class DahuaArcRadioBase(DahuaArcBase):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        device: RadioDeviceInfo,
        suffix: str,
    ):
        super().__init__(hub)
        self.device = device
        self.zone = hub.zones.get(device.alarm_index)
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_{device.device_key.replace(':', '_')}_{suffix}"
        self._attr_device_info = _device_info_for_radio(hub, uid, device)

    @property
    def available(self) -> bool:
        return self.hub.available and self.zone is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "alarm_index": self.device.alarm_index,
            "level1": self.device.level1,
            "sense_method": self.device.sense_method,
            "classification": self.device.classification,
            "model": self.device.model,
            "dahua_area": self.device.area_hint,
            "via_level1": self.device.parent_level1,
        }


class DahuaArcRadioConnectivity(DahuaArcRadioBase):
    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self, hub: ArcHub, entry: ConfigEntry[ArcHub], device: RadioDeviceInfo
    ):
        super().__init__(hub, entry, device, "connectivity")

    @property
    def is_on(self) -> bool | None:
        return (
            None
            if self.zone is None or self.zone.online_state is None
            else self.zone.online_state != 0
        )


class DahuaArcRadioLowBattery(DahuaArcRadioBase):
    _attr_name = "Low battery"
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(
        self, hub: ArcHub, entry: ConfigEntry[ArcHub], device: RadioDeviceInfo
    ):
        super().__init__(hub, entry, device, "low_battery")

    @property
    def is_on(self) -> bool | None:
        return (
            None
            if self.zone is None or self.zone.low_power is None
            else bool(self.zone.low_power)
        )


class DahuaArcRadioTamper(DahuaArcRadioBase):
    _attr_name = "Tamper"
    _attr_device_class = BinarySensorDeviceClass.TAMPER

    def __init__(
        self, hub: ArcHub, entry: ConfigEntry[ArcHub], device: RadioDeviceInfo
    ):
        super().__init__(hub, entry, device, "tamper")

    @property
    def is_on(self) -> bool | None:
        return (
            None
            if self.zone is None or self.zone.tamper is None
            else bool(self.zone.tamper)
        )
