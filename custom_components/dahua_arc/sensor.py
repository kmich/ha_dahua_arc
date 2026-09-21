from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub
from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class ArcSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ArcHub], Any]


DESCRIPTIONS = (
    ArcSensorDescription(
        key="configured_zones",
        name="Exposed alarm inputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: len(h.primary_zones),
    ),
    ArcSensorDescription(
        key="meaningful_alarm_records",
        name="Physical alarm records",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("meaningful_alarm_records"),
    ),
    ArcSensorDescription(
        key="placeholder_alarm_records",
        name="Unused alarm table rows",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("placeholder_alarm_records"),
    ),
    ArcSensorDescription(
        key="paired_radio_devices",
        name="Paired radio devices",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("paired_radio_devices"),
    ),
    ArcSensorDescription(
        key="multiio_inputs",
        name="MultiIO wired inputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("multiio_inputs"),
    ),
    ArcSensorDescription(
        key="non_multiio_alarm_inputs",
        name="Wireless primary sensors",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("non_multiio_alarm_inputs"),
    ),
    ArcSensorDescription(
        key="config_namespaces",
        name="Discovered config namespaces",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("config_namespaces"),
    ),
    ArcSensorDescription(
        key="inventory_configs",
        name="Alarm-related config tables",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("candidate_configs_successful"),
    ),
    ArcSensorDescription(
        key="rpc_services",
        name="RPC services discovered",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("rpc_services"),
    ),
    ArcSensorDescription(
        key="rpc_services_with_methods",
        name="RPC services enumerated",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("rpc_services_with_method_lists"),
    ),
    ArcSensorDescription(
        key="pircam_candidate_methods",
        name="PIR-camera candidate RPC methods",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("pircam_candidate_methods"),
    ),
    ArcSensorDescription(
        key="alarm_input_slots",
        name="Alarm input slots",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("alarm_input_slots"),
    ),
    ArcSensorDescription(
        key="alarm_output_slots",
        name="Alarm output slots",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("alarm_output_slots"),
    ),
    ArcSensorDescription(
        key="alarm_output_state_records",
        name="Alarm output state records",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("alarm_output_state_records"),
    ),
    ArcSensorDescription(
        key="event_codes_observed",
        name="Event codes observed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("event_codes_observed"),
    ),
    ArcSensorDescription(
        key="all_events_observed",
        name="All events observed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.inventory_summary().get("all_events_observed"),
    ),
    ArcSensorDescription(
        key="dhip_generation",
        name="DHIP generation",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.realtime.generation if h.realtime else None,
    ),
    ArcSensorDescription(
        key="reconnects",
        name="DHIP reconnects",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.realtime.reconnect_count if h.realtime else None,
    ),
    ArcSensorDescription(
        key="realtime_events",
        name="Realtime events received",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.realtime_events_received if h.engine else None,
    ),
    ArcSensorDescription(
        key="duplicate_events",
        name="Duplicate events",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.duplicate_events if h.engine else None,
    ),
    ArcSensorDescription(
        key="snapshot_corrections",
        name="Snapshot corrections",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.snapshot_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="reconnect_corrections",
        name="Reconnect corrections",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.reconnect_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="periodic_corrections",
        name="Periodic corrections",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.periodic_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="stale_snapshot_rejects",
        name="Stale snapshot rejects",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.engine.stale_snapshot_rejects if h.engine else None,
    ),
    ArcSensorDescription(
        key="last_alarm_event_time",
        name="Last alarm event time",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.realtime.last_alarm_event_time if h.realtime else None,
    ),
    ArcSensorDescription(
        key="last_frame_time",
        name="Last DHIP frame time",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.realtime.last_frame_time if h.realtime else None,
    ),
    ArcSensorDescription(
        key="last_realtime_error",
        name="Last realtime error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.realtime.last_error if h.realtime else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ArcHub],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        DahuaArcDiagnosticSensor(entry.runtime_data, entry, description)
        for description in DESCRIPTIONS
    )


class DahuaArcDiagnosticSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        description: ArcSensorDescription,
    ):
        self.hub = hub
        self.entity_description = description
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            name=hub.device_type,
            manufacturer="Dahua",
            model=hub.device_type,
            sw_version=hub.software_version,
            serial_number=hub.serial_number,
        )

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.hub)

    @property
    def available(self) -> bool:
        return self.hub.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def listener(indices: set[int] | None) -> None:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

        self.async_on_remove(self.hub.add_listener(listener))
