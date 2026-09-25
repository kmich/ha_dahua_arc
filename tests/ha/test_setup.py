"""Exercise the HA adapter and its platforms without an ARC on the network."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, patch

from custom_components.dahua_arc import (
    async_migrate_entry,
    async_remove_config_entry_device,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.dahua_arc.const import (
    CONF_ARC_SERIAL,
    CONF_ENABLE_RESEARCH_FEATURES,
    DOMAIN,
    ISSUE_RESEARCH_ENABLED,
    ISSUE_SERIAL_MISMATCH,
)
from custom_components.dahua_arc.diagnostics import async_get_config_entry_diagnostics
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.hub_factory import (
    HALL_PIRCAM,
    KEYFOB,
    KITCHEN_WINDOW,
    OFFLINE_DOOR,
    PLACEHOLDER,
    SERIAL,
    make_hub,
)

DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "test-only",
    CONF_ARC_SERIAL: SERIAL,
}


def _entry(hass: HomeAssistant, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=kwargs.pop("unique_id", SERIAL),
        data=kwargs.pop("data", DATA),
        version=5,
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, hub) -> None:
    with patch("custom_components.dahua_arc.ArcHub", return_value=hub):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def test_setup_unload_preserves_legacy_identity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.0.2.10:5000",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "test-only",
        },
        version=4,
    )
    entry.add_to_hass(hass)
    hub = make_hub()
    with (
        patch("custom_components.dahua_arc.ArcHub", return_value=hub),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as forward,
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as unload,
    ):
        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 5
        assert await async_setup_entry(hass, entry) is True
        assert entry.unique_id == "192.0.2.10:5000"
        assert entry.data[CONF_ARC_SERIAL] == SERIAL
        assert entry.runtime_data is hub
        assert forward.await_count == 1
        registry = dr.async_get(hass)
        assert (
            registry.async_get_device_by_identifier(
                (DOMAIN, "192.0.2.10:5000"), entry.entry_id
            )
            is not None
        )
        assert (
            registry.async_get(hub.child_device_ids[KITCHEN_WINDOW]).parent_device_id
            == (hub.multiio_parent_ids[2])
        )
        assert await async_unload_entry(hass, entry) is True
        unload.assert_awaited_once()
    hub.start.assert_called_once()
    hub.stop.assert_called_once()


async def test_platforms_create_expected_entities(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)
    assert entry.state is ConfigEntryState.LOADED

    zone_id = _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KITCHEN_WINDOW}")
    assert zone_id is not None
    state = hass.states.get(zone_id)
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == "window"
    assert state.attributes["dahua_area"] == "Kitchen"

    connection = _entity_id(hass, "binary_sensor", f"{SERIAL}_connection")
    assert hass.states.get(connection).state == "on"

    # Radio health entities exist for the PIRCam and keyfob devices.
    registry = er.async_get(hass)
    radio_uids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.unique_id.endswith("_tamper")
    }
    assert len(radio_uids) == 3  # MultiIO board, PIRCam, keyfob

    # The keyfob is a peripheral, never a primary zone sensor.
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KEYFOB}") is None

    # Research platforms stay empty unless research mode is enabled.
    assert (
        _entity_id(hass, "camera", f"{SERIAL}_pircam_{HALL_PIRCAM}_latest_snapshot")
        is None
    )
    assert (
        _entity_id(hass, "button", f"{SERIAL}_pircam_{HALL_PIRCAM}_start_detector_test")
        is None
    )

    # Research-oriented diagnostics are registered but disabled by default.
    rpc = registry.async_get(_entity_id(hass, "sensor", f"{SERIAL}_rpc_services"))
    assert rpc.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    exposed = _entity_id(hass, "sensor", f"{SERIAL}_configured_zones")
    assert hass.states.get(exposed).state == "2"

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), entry.entry_id
    )
    assert device.configuration_url == "http://192.0.2.10"

    assert await hass.config_entries.async_unload(entry.entry_id)
    hub.stop.assert_called_once()


async def test_zone_event_updates_only_that_zone(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)
    kitchen = _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KITCHEN_WINDOW}")
    pir = _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{HALL_PIRCAM}")
    pir_before = hass.states.get(pir).last_updated

    # Engine callbacks arrive from a worker thread.
    event = {
        "Code": "AlarmInputSourceSignal",
        "Index": KITCHEN_WINDOW,
        "Action": "Start",
    }
    worker = threading.Thread(target=hub.engine._apply_event, args=(event,))
    worker.start()
    worker.join()
    await hass.async_block_till_done()

    assert hass.states.get(kitchen).state == "on"
    assert hass.states.get(pir).last_updated == pir_before

    # A hub-wide availability change redraws everything.
    hub.realtime.connected = False
    hub._notify(None)
    await hass.async_block_till_done()
    assert hass.states.get(kitchen).state == "unavailable"
    assert hass.states.get(pir).state == "unavailable"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_runtime_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)

    worker = threading.Thread(
        target=hub._handle_auth_failure, args=(LoginError("bad password"),)
    )
    worker.start()
    worker.join()
    await hass.async_block_till_done()

    assert hub.auth_failed is True
    assert hub._periodic_stop.is_set()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]

    # A second failure does not start a second flow.
    hub._handle_auth_failure(LoginError("again"))
    await hass.async_block_till_done()
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_login_error_on_setup_requires_reauth(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    hub.start.side_effect = LoginError("bad password")
    await _setup(hass, entry, hub)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == SOURCE_REAUTH
    hub.stop.assert_called_once()


async def test_serial_mismatch_is_a_repair_not_a_reauth_loop(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    hub = make_hub(serial="SOMEONE-ELSE")
    await _setup(hass, entry, hub)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_{ISSUE_SERIAL_MISMATCH}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {"host": "192.0.2.10"}
    hub.stop.assert_called_once()


async def test_phantom_cleanup_only_removes_placeholders_and_peripherals(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)
    for idx in (PLACEHOLDER, KEYFOB, OFFLINE_DOOR):
        registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{SERIAL}_zone_{idx}",
            config_entry=entry,
        )
    hub = make_hub()
    await _setup(hass, entry, hub)

    # v0.3 phantom rows are gone...
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{PLACEHOLDER}") is None
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KEYFOB}") is None
    # ...but a real input missing from this startup's snapshot is preserved.
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{OFFLINE_DOOR}")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_remove_device_only_when_no_longer_on_the_arc(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)
    registry = dr.async_get(hass)
    root = registry.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    child = registry.async_get(hub.child_device_ids[KITCHEN_WINDOW])
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SERIAL}:radio:gone")},
        name="Removed sensor",
    )
    assert not await async_remove_config_entry_device(hass, entry, root)
    assert not await async_remove_config_entry_device(hass, entry, child)
    assert await async_remove_config_entry_device(hass, entry, stale)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_research_mode_entities_and_repair(hass: HomeAssistant) -> None:
    entry = _entry(hass, options={CONF_ENABLE_RESEARCH_FEATURES: True})
    hub = make_hub(research=True)
    await _setup(hass, entry, hub)

    camera = _entity_id(
        hass, "camera", f"{SERIAL}_pircam_{HALL_PIRCAM}_latest_snapshot"
    )
    start = _entity_id(
        hass, "button", f"{SERIAL}_pircam_{HALL_PIRCAM}_start_detector_test"
    )
    stop = _entity_id(
        hass, "button", f"{SERIAL}_pircam_{HALL_PIRCAM}_stop_detector_test"
    )
    assert camera and start and stop
    assert hass.states.get(start).attributes["target"] == "Hall PIR Camera"

    issue_id = f"{entry.entry_id}_{ISSUE_RESEARCH_ENABLED}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    with patch("custom_components.dahua_arc.ArcHub", return_value=make_hub()):
        from custom_components.dahua_arc.repairs import async_create_fix_flow

        flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
        flow.hass = hass
        flow.issue_id = issue_id
        flow.data = {"entry_id": entry.entry_id}
        result = await flow.async_step_init()
        assert result["step_id"] == "confirm"
        result = await flow.async_step_confirm({})
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options[CONF_ENABLE_RESEARCH_FEATURES] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_redacts_connection_identity(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    entry.runtime_data = hub
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result)
    assert "192.0.2.10" not in str(result["entry"])
    assert "admin" not in str(result["entry"])
    assert "test-only" not in text
    assert SERIAL not in text
    assert "PIRCAM-SN-1" not in text
    assert "FOB-SN-1" not in text
    assert result["runtime"]["inventory_summary"]["exposed_alarm_inputs"] == 2
