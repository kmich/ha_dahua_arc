"""Verify HA 2026.9 MultiIO child-device and area ownership behavior."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.dahua_arc import (
    _apply_child_areas,
    _register_multiio_children,
    _register_radio_devices,
)
from custom_components.dahua_arc.const import (
    CONF_AUTO_AREA_MATCH,
    CONF_ZONE_AREA_DECISIONS,
    CONF_ZONE_AREA_OWNERSHIP,
    DOMAIN,
)
from custom_components.dahua_arc.protocol.models import Zone
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_multiio_child_is_under_physical_parent_and_preserves_manual_area(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ARC-TEST-001",
        data={},
        options={CONF_AUTO_AREA_MATCH: True},
        version=5,
    )
    entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    office = areas.async_create("Office")
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ARC-TEST-001")},
        name="ARC",
    )
    zone = Zone(
        index=7,
        name="Kitchen Window",
        classification="multiio_input",
        is_multiio=True,
        level1=2,
        level2=1,
    )
    hub = SimpleNamespace(
        radio_devices={},
        parents={2: {"name": "MultiIO board 2"}},
        primary_zones={7: zone},
    )
    physical = _register_radio_devices(hass, entry, hub, "ARC-TEST-001")
    children = _register_multiio_children(hass, entry, hub, "ARC-TEST-001", physical)
    assert children[7].parent_device_id == physical[2].id
    assert children[7].id != physical[2].id
    decisions = {"7": {"area_id": kitchen.id, "score": 100, "reason": "exact"}}
    _apply_child_areas(hass, entry, children, decisions)
    assert registry.async_get(children[7].id).area_id == kitchen.id
    assert entry.options[CONF_ZONE_AREA_OWNERSHIP]["7"]["area_id"] == kitchen.id

    # A later user change must win over a new automatic recommendation.
    registry.async_update_child_device(children[7].id, area_id=office.id)
    _apply_child_areas(
        hass,
        entry,
        children,
        {"7": {"area_id": kitchen.id, "score": 100, "reason": "exact"}},
    )
    assert registry.async_get(children[7].id).area_id == office.id

    # The persisted mapping is visible in options rather than re-matched.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ZONE_AREA_DECISIONS: decisions}
    )
    assert entry.options[CONF_ZONE_AREA_DECISIONS]["7"]["area_id"] == kitchen.id
