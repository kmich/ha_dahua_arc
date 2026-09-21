from __future__ import annotations

import logging
from urllib.error import HTTPError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .area_assignment import decide_zone_areas, may_auto_assign
from .area_matcher import AreaCandidate
from .client import ArcHub
from .const import (
    CONF_ARC_SERIAL,
    CONF_AREA_MATCH_AREAS,
    CONF_AREA_MATCH_THRESHOLD,
    CONF_AUTO_AREA_MATCH,
    CONF_DHIP_PORT,
    CONF_ENABLE_RESEARCH_FEATURES,
    CONF_HTTP_PORT,
    CONF_PERIODIC_RESYNC,
    CONF_ZONE_AREA_DECISIONS,
    CONF_ZONE_AREA_OWNERSHIP,
    DEFAULT_AREA_MATCH_THRESHOLD,
    DEFAULT_AUTO_AREA_MATCH,
    DEFAULT_DHIP_PORT,
    DEFAULT_ENABLE_RESEARCH_FEATURES,
    DEFAULT_HTTP_PORT,
    DEFAULT_PERIODIC_RESYNC,
    DOMAIN,
    ISSUE_INVENTORY_ERROR,
    ISSUE_NO_PRIMARY_ZONES,
    ISSUE_RESEARCH_ENABLED,
    PLATFORMS,
)
from .inventory import RadioDeviceInfo
from .vendor.dahua.exceptions import LoginError

_LOGGER = logging.getLogger(__name__)

DahuaArcConfigEntry = ConfigEntry[ArcHub]


def _radio_identifier(uid: str, device: RadioDeviceInfo) -> tuple[str, str]:
    return (DOMAIN, f"{uid}:{device.device_key}")


def _register_radio_devices(
    hass: HomeAssistant, entry: DahuaArcConfigEntry, hub: ArcHub, uid: str
) -> dict[int, dr.DeviceEntry]:
    """Register the physical AirFly device graph before entity platforms load."""
    registry = dr.async_get(hass)
    created: dict[int, dr.DeviceEntry] = {}

    root = registry.async_get_device_by_identifier((DOMAIN, uid), entry.entry_id)
    if root is None:
        raise RuntimeError("Dahua ARC root device is not registered")

    # Direct devices first, then repeater children so via_device_id always resolves.
    ordered = sorted(
        hub.radio_devices.values(),
        key=lambda d: (d.parent_level1 is not None, d.level1),
    )
    for device in ordered:
        via_device_id = root.id
        if device.parent_level1 is not None:
            parent_entry = created.get(device.parent_level1)
            if parent_entry is not None:
                via_device_id = parent_entry.id

        created[device.level1] = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={_radio_identifier(uid, device)},
            name=device.name,
            manufacturer="Dahua",
            model=device.model or device.sense_method or "ARC radio device",
            serial_number=device.serial,
            via_device_id=via_device_id,
        )
    for level1, parent in hub.parents.items():
        if level1 in created:
            continue
        created[level1] = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{uid}:multiio:{level1}")},
            name=str(parent.get("name") or f"MultiIO {level1}"),
            manufacturer="Dahua",
            model="MultiIO",
            via_device_id=root.id,
        )
    return created


def _apply_smart_device_areas(
    hass: HomeAssistant,
    entry: DahuaArcConfigEntry,
    hub: ArcHub,
    devices: dict[int, dr.DeviceEntry],
) -> None:
    """Apply the persisted zone decision to an unassigned physical device."""
    options = entry.options
    if not options.get(CONF_AUTO_AREA_MATCH, DEFAULT_AUTO_AREA_MATCH):
        return

    decisions = options.get(CONF_ZONE_AREA_DECISIONS, {})
    registry = dr.async_get(hass)
    ownership = dict(options.get(CONF_ZONE_AREA_OWNERSHIP, {}))
    changed = False

    # Keyfobs move with the user. Do not infer a physical room for them even if
    # the Dahua subsystem config associates them with an area.
    fixed_location_classes = {
        "motion",
        "radar",
        "repeater",
        "siren",
        "keypad",
        "contact",
        "smoke_or_fire",
        "flood_or_water",
        "glass_break",
    }

    for level1, device in hub.radio_devices.items():
        if device.classification not in fixed_location_classes:
            continue
        entry_device = devices.get(level1)
        if entry_device is None:
            continue
        decision = decisions.get(str(device.alarm_index), {})
        area_id = decision.get("area_id")
        key = f"radio:{level1}"
        owned = ownership.get(key, {})
        if not may_auto_assign(
            entry_device.area_id,
            area_id,
            owned.get("area_id"),
            was_automatically_assigned=owned.get("device_id") == entry_device.id,
        ):
            continue
        if entry_device.area_id != area_id:
            registry.async_update_device(entry_device.id, area_id=area_id)
        if owned != {"device_id": entry_device.id, "area_id": area_id}:
            ownership[key] = {"device_id": entry_device.id, "area_id": area_id}
            changed = True
        _LOGGER.info(
            "Smart area match: ARC device %s -> %s (%s%%)",
            device.name,
            area_id,
            decision.get("score"),
        )
    if changed:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ZONE_AREA_OWNERSHIP: ownership}
        )


def _register_multiio_children(
    hass: HomeAssistant,
    entry: DahuaArcConfigEntry,
    hub: ArcHub,
    uid: str,
    devices: dict[int, dr.DeviceEntry],
) -> dict[int, dr.ChildDeviceEntry]:
    """Represent logical wired inputs under their physical MultiIO board."""
    registry = dr.async_get(hass)
    children: dict[int, dr.ChildDeviceEntry] = {}
    for index, zone in hub.primary_zones.items():
        if not zone.is_multiio or zone.level1 not in devices:
            continue
        parent = devices[zone.level1]
        children[index] = registry.async_get_or_create_child(
            config_entry_id=entry.entry_id,
            parent_device_id=parent.id,
            identifiers={(DOMAIN, f"{uid}:zone:{index}")},
            name=zone.name,
        )
    return children


def _persist_new_area_decisions(
    hass: HomeAssistant, entry: DahuaArcConfigEntry, hub: ArcHub
) -> dict[str, dict]:
    """Keep existing decisions stable; suggest only newly discovered indexes."""
    if not entry.options.get(CONF_AUTO_AREA_MATCH, DEFAULT_AUTO_AREA_MATCH):
        return dict(entry.options.get(CONF_ZONE_AREA_DECISIONS, {}))
    selected_ids = set(entry.options.get(CONF_AREA_MATCH_AREAS, []) or [])
    candidates = [
        AreaCandidate(
            area_id=area.id, name=area.name, aliases=tuple(sorted(area.aliases))
        )
        for area in ar.async_get(hass).async_list_areas()
        if not selected_ids or area.id in selected_ids
    ]
    previous = entry.options.get(CONF_ZONE_AREA_DECISIONS, {})
    items = (
        {"index": zone.index, "name": zone.name, "area_hint": zone.area_hint}
        for zone in hub.primary_zones.values()
    )
    decisions = decide_zone_areas(
        items,
        candidates,
        threshold=int(
            entry.options.get(CONF_AREA_MATCH_THRESHOLD, DEFAULT_AREA_MATCH_THRESHOLD)
        ),
        previous=previous,
    )
    if decisions != previous:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ZONE_AREA_DECISIONS: decisions}
        )
    return decisions


def _apply_child_areas(
    hass: HomeAssistant,
    entry: DahuaArcConfigEntry,
    children: dict[int, dr.ChildDeviceEntry],
    decisions: dict[str, dict],
) -> None:
    """Never replace a manual area, including a user-cleared auto area."""
    if not entry.options.get(CONF_AUTO_AREA_MATCH, DEFAULT_AUTO_AREA_MATCH):
        return
    registry = dr.async_get(hass)
    ownership = dict(entry.options.get(CONF_ZONE_AREA_OWNERSHIP, {}))
    changed = False
    for index, child in children.items():
        key = str(index)
        target = decisions.get(key, {}).get("area_id")
        owned = ownership.get(key, {})
        if not may_auto_assign(
            child.area_id,
            target,
            owned.get("area_id"),
            was_automatically_assigned=owned.get("device_id") == child.id,
        ):
            continue
        if child.area_id != target:
            registry.async_update_child_device(child.id, area_id=target)
        if owned != {"device_id": child.id, "area_id": target}:
            ownership[key] = {"device_id": child.id, "area_id": target}
            changed = True
    if changed:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ZONE_AREA_OWNERSHIP: ownership}
        )


def _migrate_primary_entity_devices(
    hass: HomeAssistant,
    entry: DahuaArcConfigEntry,
    hub: ArcHub,
    uid: str,
    devices: dict[int, dr.DeviceEntry],
    children: dict[int, dr.ChildDeviceEntry],
) -> None:
    """Move surviving v0.3 wireless sensor entities onto their real child device."""
    registry = er.async_get(hass)
    by_unique = {
        str(entity.unique_id or ""): entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for idx, zone in hub.primary_zones.items():
        if zone.level1 is None:
            continue
        device = children.get(idx) if zone.is_multiio else devices.get(zone.level1)
        entity = by_unique.get(f"{uid}_zone_{idx}")
        if device is None or entity is None or entity.device_id == device.id:
            continue
        registry.async_update_entity(entity.entity_id, device_id=device.id)


def _cleanup_v03_phantom_zone_entities(
    hass: HomeAssistant, entry: DahuaArcConfigEntry, hub: ArcHub, uid: str
) -> None:
    """Remove only the bogus ZoneXX entities created by the v0.3 inventory build."""
    registry = er.async_get(hass)
    valid = {f"{uid}_zone_{idx}" for idx in hub.primary_zones}
    prefix = f"{uid}_zone_"
    removed = 0

    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = str(entity.unique_id or "")
        if not unique_id.startswith(prefix) or unique_id in valid:
            continue
        suffix = unique_id[len(prefix) :]
        try:
            idx = int(suffix)
        except ValueError:
            continue
        # Only remove entities that are no longer valid primary sensors. This
        # includes the v0.3 placeholder ZoneXX rows and peripheral rows that
        # were incorrectly represented as generic opening sensors.
        if idx not in hub.primary_zones:
            registry.async_remove(entity.entity_id)
            removed += 1

    if removed:
        _LOGGER.warning(
            "Removed %s obsolete Dahua ARC v0.3 phantom/peripheral zone entities",
            removed,
        )


def _entry_issue_id(entry: DahuaArcConfigEntry, suffix: str) -> str:
    return f"{entry.entry_id}_{suffix}"


def _update_repair_issues(
    hass: HomeAssistant, entry: DahuaArcConfigEntry, hub: ArcHub
) -> None:
    """Create or clear user-visible repair issues for risky runtime states."""
    if entry.options.get(
        CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            _entry_issue_id(entry, ISSUE_RESEARCH_ENABLED),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RESEARCH_ENABLED,
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(
            hass, DOMAIN, _entry_issue_id(entry, ISSUE_RESEARCH_ENABLED)
        )

    if not hub.primary_zones:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _entry_issue_id(entry, ISSUE_NO_PRIMARY_ZONES),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_NO_PRIMARY_ZONES,
        )
    else:
        ir.async_delete_issue(
            hass, DOMAIN, _entry_issue_id(entry, ISSUE_NO_PRIMARY_ZONES)
        )

    if hub.inventory_error:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _entry_issue_id(entry, ISSUE_INVENTORY_ERROR),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_INVENTORY_ERROR,
            translation_placeholders={"error": hub.inventory_error[:500]},
        )
    else:
        ir.async_delete_issue(
            hass, DOMAIN, _entry_issue_id(entry, ISSUE_INVENTORY_ERROR)
        )


async def async_setup_entry(hass: HomeAssistant, entry: DahuaArcConfigEntry) -> bool:
    hub = ArcHub(
        host=entry.data[CONF_HOST],
        http_port=entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
        dhip_port=entry.data.get(CONF_DHIP_PORT, DEFAULT_DHIP_PORT),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        periodic_resync_seconds=entry.options.get(
            CONF_PERIODIC_RESYNC, DEFAULT_PERIODIC_RESYNC
        ),
        enable_research_features=entry.options.get(
            CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
        ),
    )
    try:
        await hass.async_add_executor_job(hub.start)
    except LoginError as exc:
        await hass.async_add_executor_job(hub.stop)
        raise ConfigEntryAuthFailed(
            f"Unable to authenticate to Dahua ARC: {exc}"
        ) from exc
    except HTTPError as exc:
        await hass.async_add_executor_job(hub.stop)
        if exc.code in (401, 403):
            raise ConfigEntryAuthFailed(
                f"Unable to authenticate to Dahua ARC: HTTP {exc.code}"
            ) from exc
        raise ConfigEntryNotReady(f"Unable to connect to Dahua ARC: {exc}") from exc
    except Exception as exc:
        await hass.async_add_executor_job(hub.stop)
        raise ConfigEntryNotReady(f"Unable to connect to Dahua ARC: {exc}") from exc

    entry.runtime_data = hub
    uid = entry.unique_id or entry.entry_id

    if hub.serial_number and not entry.data.get(CONF_ARC_SERIAL):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ARC_SERIAL: hub.serial_number}
        )
    elif (
        hub.serial_number
        and entry.data.get(CONF_ARC_SERIAL)
        and hub.serial_number != entry.data[CONF_ARC_SERIAL]
    ):
        await hass.async_add_executor_job(hub.stop)
        raise ConfigEntryAuthFailed("ARC serial differs from configured device")

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, uid)},
        name=hub.device_type,
        manufacturer="Dahua",
        model=hub.device_type,
        sw_version=hub.software_version,
        serial_number=hub.serial_number,
        configuration_url=f"http://{hub.host}",
    )

    devices = _register_radio_devices(hass, entry, hub, uid)
    hub.multiio_parent_ids = {level1: device.id for level1, device in devices.items()}
    children = _register_multiio_children(hass, entry, hub, uid, devices)
    hub.child_device_ids = {index: child.id for index, child in children.items()}
    decisions = _persist_new_area_decisions(hass, entry, hub)
    hub.area_decisions = decisions
    _apply_smart_device_areas(hass, entry, hub, devices)
    _apply_child_areas(hass, entry, children, decisions)
    _migrate_primary_entity_devices(hass, entry, hub, uid, devices, children)
    _cleanup_v03_phantom_zone_entities(hass, entry, hub, uid)
    _update_repair_issues(hass, entry, hub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DahuaArcConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await hass.async_add_executor_job(entry.runtime_data.stop)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older Dahua ARC config entries to the current flow version."""
    if entry.version < 5:
        hass.config_entries.async_update_entry(entry, version=5)
    return True
