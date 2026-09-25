from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArcHub
from .entity import DahuaArcEntity, root_device_info
from .protocol.util import parse_timestamp

# Diagnostic counters are read from memory. Polling them on a fixed interval
# keeps them current without redrawing every sensor on every ARC event.
SCAN_INTERVAL = timedelta(seconds=30)
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ArcSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ArcHub], Any]
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    # Connection diagnostics stay readable while the realtime stream is down,
    # which is exactly when they are needed.
    always_available: bool = False


def _summary(key: str) -> Callable[[ArcHub], Any]:
    return lambda h: h.inventory_summary().get(key)


# Research-oriented counters are disabled by default; users and contributors
# can enable them from the entity settings when investigating a firmware.
DESCRIPTIONS = (
    ArcSensorDescription(
        key="configured_zones",
        translation_key="configured_zones",
        value_fn=lambda h: len(h.primary_zones),
    ),
    ArcSensorDescription(
        key="meaningful_alarm_records",
        translation_key="meaningful_alarm_records",
        value_fn=_summary("meaningful_alarm_records"),
    ),
    ArcSensorDescription(
        key="placeholder_alarm_records",
        translation_key="placeholder_alarm_records",
        entity_registry_enabled_default=False,
        value_fn=_summary("placeholder_alarm_records"),
    ),
    ArcSensorDescription(
        key="paired_radio_devices",
        translation_key="paired_radio_devices",
        value_fn=_summary("paired_radio_devices"),
    ),
    ArcSensorDescription(
        key="multiio_inputs",
        translation_key="multiio_inputs",
        value_fn=_summary("multiio_inputs"),
    ),
    ArcSensorDescription(
        key="non_multiio_alarm_inputs",
        translation_key="non_multiio_alarm_inputs",
        value_fn=_summary("non_multiio_alarm_inputs"),
    ),
    ArcSensorDescription(
        key="config_namespaces",
        translation_key="config_namespaces",
        entity_registry_enabled_default=False,
        value_fn=_summary("config_namespaces"),
    ),
    ArcSensorDescription(
        key="inventory_configs",
        translation_key="inventory_configs",
        entity_registry_enabled_default=False,
        value_fn=_summary("candidate_configs_successful"),
    ),
    ArcSensorDescription(
        key="rpc_services",
        translation_key="rpc_services",
        entity_registry_enabled_default=False,
        value_fn=_summary("rpc_services"),
    ),
    ArcSensorDescription(
        key="rpc_services_with_methods",
        translation_key="rpc_services_with_methods",
        entity_registry_enabled_default=False,
        value_fn=_summary("rpc_services_with_method_lists"),
    ),
    ArcSensorDescription(
        key="pircam_candidate_methods",
        translation_key="pircam_candidate_methods",
        entity_registry_enabled_default=False,
        value_fn=_summary("pircam_candidate_methods"),
    ),
    ArcSensorDescription(
        key="alarm_input_slots",
        translation_key="alarm_input_slots",
        entity_registry_enabled_default=False,
        value_fn=_summary("alarm_input_slots"),
    ),
    ArcSensorDescription(
        key="alarm_output_slots",
        translation_key="alarm_output_slots",
        entity_registry_enabled_default=False,
        value_fn=_summary("alarm_output_slots"),
    ),
    ArcSensorDescription(
        key="alarm_output_state_records",
        translation_key="alarm_output_state_records",
        entity_registry_enabled_default=False,
        value_fn=_summary("alarm_output_state_records"),
    ),
    ArcSensorDescription(
        key="event_codes_observed",
        translation_key="event_codes_observed",
        entity_registry_enabled_default=False,
        value_fn=_summary("event_codes_observed"),
    ),
    ArcSensorDescription(
        key="all_events_observed",
        translation_key="all_events_observed",
        entity_registry_enabled_default=False,
        value_fn=_summary("all_events_observed"),
    ),
    ArcSensorDescription(
        key="dhip_generation",
        translation_key="dhip_generation",
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.realtime.generation if h.realtime else None,
    ),
    ArcSensorDescription(
        key="reconnects",
        translation_key="reconnects",
        always_available=True,
        value_fn=lambda h: h.realtime.reconnect_count if h.realtime else None,
    ),
    ArcSensorDescription(
        key="realtime_events",
        translation_key="realtime_events",
        value_fn=lambda h: h.engine.realtime_events_received if h.engine else None,
    ),
    ArcSensorDescription(
        key="duplicate_events",
        translation_key="duplicate_events",
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.engine.duplicate_events if h.engine else None,
    ),
    ArcSensorDescription(
        key="snapshot_corrections",
        translation_key="snapshot_corrections",
        value_fn=lambda h: h.engine.snapshot_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="reconnect_corrections",
        translation_key="reconnect_corrections",
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.engine.reconnect_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="periodic_corrections",
        translation_key="periodic_corrections",
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.engine.periodic_corrections if h.engine else None,
    ),
    ArcSensorDescription(
        key="stale_snapshot_rejects",
        translation_key="stale_snapshot_rejects",
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.engine.stale_snapshot_rejects if h.engine else None,
    ),
    ArcSensorDescription(
        key="last_alarm_event_time",
        translation_key="last_alarm_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda h: (
            parse_timestamp(h.realtime.last_alarm_event_time) if h.realtime else None
        ),
    ),
    ArcSensorDescription(
        key="last_frame_time",
        translation_key="last_frame_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda h: (
            parse_timestamp(h.realtime.last_frame_time) if h.realtime else None
        ),
    ),
    ArcSensorDescription(
        key="last_realtime_error",
        translation_key="last_realtime_error",
        always_available=True,
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


class DahuaArcDiagnosticSensor(DahuaArcEntity, SensorEntity):
    _attr_should_poll = True
    entity_description: ArcSensorDescription

    def __init__(
        self,
        hub: ArcHub,
        entry: ConfigEntry[ArcHub],
        description: ArcSensorDescription,
    ):
        super().__init__(hub, entry)
        self.entity_description = description
        self._attr_unique_id = f"{self._uid}_{description.key}"
        self._attr_device_info = root_device_info(hub, self._uid)

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.hub)

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or self.hub.available
