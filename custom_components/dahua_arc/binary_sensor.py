from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

try:
    from homeassistant.helpers.device_registry import ChildDeviceInfo
except ImportError:
    ChildDeviceInfo = DeviceInfo  # type: ignore[misc,assignment]
from homeassistant.helpers.entity import EntityCategory

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:
    from homeassistant.helpers.entity_platform import (
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,  # type: ignore[attr-defined]
    )

from .client import ArcHub, RadioDeviceInfo, Zone
from .const import DOMAIN
from .entity import (
    DahuaArcArmStateEntity,
    DahuaArcEntity,
    entry_uid,
    radio_device_info,
    root_device_info,
)
from .protocol.arming import ArmArea

_LOGGER = logging.getLogger(__name__)
_GLASS_BREAK_DEVICE_CLASS = getattr(
    BinarySensorDeviceClass, "GLASS_BREAK", BinarySensorDeviceClass.VIBRATION
)
PARALLEL_UPDATES = 0


def _device_class(zone: Zone) -> BinarySensorDeviceClass | None:
    name = zone.name.lower()
    cls = zone.classification
    if (
        cls in {"motion", "radar"}
        or "pir" in name
        or "motion" in name
        or "radar" in name
    ):
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


def _device_info_for_zone(
    hub: ArcHub,
    uid: str,
    zone: Zone,
    default_name: str | ConfigEntry | None = None,
) -> tuple[DeviceInfo | ChildDeviceInfo, bool]:
    child_device_ids = getattr(hub, "child_device_ids", {})
    multiio_parent_ids = getattr(hub, "multiio_parent_ids", {})
    child_id = child_device_ids.get(zone.index)
    if zone.is_multiio and child_id:
        parent = multiio_parent_ids.get(zone.level1)
        if parent:
            return (
                ChildDeviceInfo(
                    identifiers={(DOMAIN, f"{uid}:zone:{zone.index}")},
                    name=zone.name,
                    parent_device_id=parent,
                ),
                True,
            )
    radio = (
        hub.radio_device_for_zone(zone)
        if hasattr(hub, "radio_device_for_zone")
        else None
    )
    if radio is not None:
        pz = getattr(hub, "primary_zones", {})
        pz_list = list(pz.values()) if isinstance(pz, dict) else list(pz)
        other_primary_zones = [
            z
            for z in pz_list
            if z.index != zone.index
            and (
                (
                    hasattr(hub, "radio_device_for_zone")
                    and hub.radio_device_for_zone(z) == radio
                )
                or (getattr(z, "level1", None) is not None and z.level1 == zone.level1)
            )
        ]
        is_dedicated = len(other_primary_zones) == 0
        return (radio_device_info(uid, radio), is_dedicated)
    return (root_device_info(hub, uid, default_name), False)


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

    # Wired inputs have no radio health entities, so report line faults here.
    # Only on a device of their own, where "<device> Fault" names one input.
    entities.extend(
        DahuaArcZoneFault(hub, entry, zone)
        for zone in hub.primary_zones.values()
        if zone.is_multiio and _device_info_for_zone(hub, entry_uid(entry), zone)[1]
    )

    if hub.arming is not None:
        entities.append(DahuaArcSystemAlarm(hub, entry))
        entities.extend(
            DahuaArcAreaAlarm(hub, entry, area) for area in hub.arming.areas.values()
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


class DahuaArcConnectivity(DahuaArcEntity, BinarySensorEntity):
    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub]):
        super().__init__(hub, entry)
        self._attr_unique_id = f"{self._uid}_connection"
        self._attr_device_info = root_device_info(hub, self._uid, entry)

    @property
    def is_on(self) -> bool:
        return self.hub.available


def _alarm_zones(areas: list[ArmArea]) -> list[dict[str, object]]:
    return [
        {
            "area": zone["area"],
            "zone": zone["zone"],
            "alarm_type": zone["alarm_type"],
            "at": zone["at"],
        }
        for area in areas
        for zone in area.alarm_zones
    ]


class DahuaArcSystemAlarm(DahuaArcArmStateEntity, BinarySensorEntity):
    """On while any Dahua area is in alarm (read-only, from ARC events)."""

    _attr_translation_key = "alarm"
    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub]):
        super().__init__(hub, entry)
        self._attr_unique_id = f"{self._uid}_alarm"

    @property
    def is_on(self) -> bool | None:
        return self.hub.arming.alarm_state()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        arming = self.hub.arming
        areas = arming.alarm_areas()
        last = arming.last_alarm
        return {
            "alarm_areas": [area.name for area in areas],
            "alarm_zones": _alarm_zones(areas),
            "last_alarm": last["at"] if last else None,
            "last_alarm_zone": last["zone"] if last else None,
            "last_alarm_areas": last["areas"] if last else None,
        }


class DahuaArcAreaAlarm(DahuaArcArmStateEntity, BinarySensorEntity):
    """On while one Dahua area is in alarm, until it is disarmed."""

    _attr_translation_key = "area_alarm"
    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _unrecorded_attributes = frozenset({"area_id"})

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], area: ArmArea):
        super().__init__(hub, entry)
        self.area = area
        self._attr_unique_id = f"{self._uid}_area_{area.area_id}_alarm"
        self._attr_translation_placeholders = {"area": area.name}

    @property
    def is_on(self) -> bool | None:
        return self.area.alarm

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        area = self.area
        return {
            "area_id": area.area_id,
            "alarm_zones": _alarm_zones([area]),
            "alarm_started": area.alarm_started,
            "alarm_ended": area.alarm_ended,
        }


class DahuaArcZoneBinarySensor(DahuaArcEntity, BinarySensorEntity):
    # Volatile or static diagnostic attributes that must not create a new
    # recorder row on every change.
    _unrecorded_attributes = frozenset(
        {
            "last_source",
            "last_action",
            "last_changed",
            "raw_alarm_state",
            "parent_serial",
            "smart_area_match",
            "smart_area_match_score",
            "smart_area_match_reason",
        }
    )

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], zone: Zone):
        super().__init__(hub, entry)
        self.zone = zone
        self._watched_indices = frozenset({zone.index})
        decision = hub.area_decisions.get(str(zone.index), {})
        self._area_match_name = decision.get("area_id")
        self._area_match_score = decision.get("score")
        self._area_match_reason = decision.get("reason")
        self._attr_unique_id = f"{self._uid}_zone_{zone.index}"
        self._attr_device_class = _device_class(zone)
        device_info, is_dedicated = _device_info_for_zone(
            hub, self._uid, zone, default_name=entry
        )
        self._attr_device_info = device_info
        self._attr_name = None if is_dedicated else zone.name

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


class DahuaArcZoneFault(DahuaArcEntity, BinarySensorEntity):
    """Line or tamper fault on one wired input, from SensorAbnormal events.

    Only events report this, so it reads off until a fault is seen, and a
    fault already active when Home Assistant connects is not visible.
    """

    _attr_translation_key = "sensor_fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], zone: Zone):
        super().__init__(hub, entry)
        self.zone = zone
        self._watched_indices = frozenset({zone.index})
        self._attr_unique_id = f"{self._uid}_zone_{zone.index}_fault"
        self._attr_device_info = _device_info_for_zone(
            hub, self._uid, zone, default_name=entry
        )[0]

    @property
    def is_on(self) -> bool | None:
        fault = self.zone.sensor_fault
        return None if fault is None else bool(fault)

    @property
    def available(self) -> bool:
        return self.hub.available

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "alarm_index": self.zone.index,
            "status": self.zone.sensor_fault or None,
        }


class DahuaArcRadioBase(DahuaArcEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        device: RadioDeviceInfo,
        suffix: str,
    ):
        super().__init__(hub, entry)
        self.device = device
        self.zone = hub.zones.get(device.alarm_index)
        self._watched_indices = frozenset({device.alarm_index})
        self._attr_unique_id = (
            f"{self._uid}_{device.device_key.replace(':', '_')}_{suffix}"
        )
        self._attr_device_info = radio_device_info(self._uid, device)

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
    _attr_translation_key = "radio_connectivity"
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
    _attr_translation_key = "low_battery"
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
    _attr_translation_key = "tamper"
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
