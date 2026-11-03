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


async def test_multiio_child_reparenting_handles_stale_parent(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="ARC-TEST-001", data={})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ARC-TEST-001")},
        name="ARC Root",
    )
    old_parent = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "old_parent_id")},
        name="Old Parent",
    )
    # Child registered with old parent
    registry.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        parent_device_id=old_parent.id,
        identifiers={(DOMAIN, "ARC-TEST-001:zone:7")},
        name="Kitchen Window",
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


async def test_multiio_child_reparenting_when_async_get_device_returns_none(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="ARC-TEST-002", data={})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ARC-TEST-002")},
        name="ARC Root",
    )
    old_parent = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "old_parent_id_2")},
        name="Old Parent",
    )
    # Child registered with old parent
    registry.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        parent_device_id=old_parent.id,
        identifiers={(DOMAIN, "ARC-TEST-002:zone:150")},
        name="Radar Zone 150",
    )
    # Simulate HA 2026.8+ where async_get_device is deprecated / returns None
    registry.async_get_device = lambda *args, **kwargs: None

    zone = Zone(
        index=150,
        name="Radar Zone 150",
        classification="multiio_input",
        is_multiio=True,
        level1=2,
        level2=1,
    )
    hub = SimpleNamespace(
        radio_devices={},
        parents={2: {"name": "MultiIO board 2"}},
        primary_zones={150: zone},
    )
    physical = _register_radio_devices(hass, entry, hub, "ARC-TEST-002")
    children = _register_multiio_children(hass, entry, hub, "ARC-TEST-002", physical)
    assert children[150].parent_device_id == physical[2].id


async def test_multiio_child_reparenting_cross_config_entry(
    hass: HomeAssistant,
) -> None:
    old_entry = MockConfigEntry(domain=DOMAIN, unique_id="ARC-OLD", data={})
    old_entry.add_to_hass(hass)
    new_entry = MockConfigEntry(domain=DOMAIN, unique_id="ARC-NEW", data={})
    new_entry.add_to_hass(hass)

    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=new_entry.entry_id,
        identifiers={(DOMAIN, "ARC-NEW")},
        name="ARC Root New",
    )
    old_parent = registry.async_get_or_create(
        config_entry_id=old_entry.entry_id,
        identifiers={(DOMAIN, "old_parent_id_3")},
        name="Old Parent",
    )
    # Child registered with old entry and old parent
    registry.async_get_or_create_child(
        config_entry_id=old_entry.entry_id,
        parent_device_id=old_parent.id,
        identifiers={(DOMAIN, "ARC-NEW:zone:150")},
        name="Radar Zone 150",
    )

    zone = Zone(
        index=150,
        name="Radar Zone 150",
        classification="multiio_input",
        is_multiio=True,
        level1=2,
        level2=1,
    )
    hub = SimpleNamespace(
        radio_devices={},
        parents={2: {"name": "MultiIO board 2"}},
        primary_zones={150: zone},
    )
    physical = _register_radio_devices(hass, new_entry, hub, "ARC-NEW")
    children = _register_multiio_children(hass, new_entry, hub, "ARC-NEW", physical)
    assert children[150].parent_device_id == physical[2].id


async def test_multiio_child_reparenting_separate_child_devices_store(
    hass: HomeAssistant,
) -> None:
    """Replicate HA 2026.10.4 where child devices live in child_devices, not devices."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="ARC-TEST-2026-10", data={})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    root = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ARC-TEST-2026-10")},
        name="ARC Root",
    )

    child_store: dict[str, SimpleNamespace] = {
        "stale_child_150": SimpleNamespace(
            id="stale_child_150",
            config_entry_id=entry.entry_id,
            parent_device_id=root.id,
            identifiers={(DOMAIN, "ARC-TEST-2026-10:zone:150")},
            name="Ground Floor LR Entrance Radar",
            area_id=None,
        )
    }

    def _get_child_by_ident(identifier: tuple[str, str], config_entry_id: str):
        for child in child_store.values():
            if (
                child.config_entry_id == config_entry_id
                and identifier in child.identifiers
            ):
                return child
        return None

    orig_remove = registry.async_remove_device

    def _remove_device(device_id: str) -> None:
        if device_id in child_store:
            del child_store[device_id]
            return
        orig_remove(device_id)

    def _get_or_create_child(
        *, config_entry_id: str, parent_device_id: str, identifiers: set, name: str
    ):
        ident = next(iter(identifiers))
        existing = _get_child_by_ident(ident, config_entry_id)
        if existing is not None and existing.parent_device_id != parent_device_id:
            raise RuntimeError(
                f"the child device is already registered with a different parent device {existing.parent_device_id}; reparenting is not supported, remove the child device first"
            )
        new_child = SimpleNamespace(
            id="new_child_150",
            config_entry_id=config_entry_id,
            parent_device_id=parent_device_id,
            identifiers=identifiers,
            name=name,
            area_id=None,
        )
        child_store[new_child.id] = new_child
        return new_child

    registry.async_get_child_device_by_identifier = _get_child_by_ident  # type: ignore[attr-defined]
    registry.async_remove_device = _remove_device  # type: ignore[method-assign]
    registry.async_get_or_create_child = _get_or_create_child  # type: ignore[method-assign]

    zone = Zone(
        index=150,
        name="Ground Floor LR Entrance Radar",
        classification="multiio_input",
        is_multiio=True,
        level1=2,
        level2=1,
    )
    hub = SimpleNamespace(
        radio_devices={},
        parents={2: {"name": "MultiIO board 2"}},
        primary_zones={150: zone},
    )
    physical = _register_radio_devices(hass, entry, hub, "ARC-TEST-2026-10")
    children = _register_multiio_children(
        hass, entry, hub, "ARC-TEST-2026-10", physical
    )
    assert children[150].parent_device_id == physical[2].id
    assert "stale_child_150" not in child_store
