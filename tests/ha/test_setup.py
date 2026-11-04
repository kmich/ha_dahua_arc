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
from custom_components.dahua_arc.binary_sensor import (
    DahuaArcZoneBinarySensor,
    _device_info_for_zone,
)
from custom_components.dahua_arc.client import RadioDeviceInfo, Zone
from custom_components.dahua_arc.const import (
    CONF_ARC_SERIAL,
    CONF_ENABLE_RESEARCH_FEATURES,
    DOMAIN,
    ISSUE_RESEARCH_ENABLED,
    ISSUE_SERIAL_MISMATCH,
)
from custom_components.dahua_arc.diagnostics import async_get_config_entry_diagnostics
from custom_components.dahua_arc.entity import root_device_info
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_OFF,
    STATE_UNKNOWN,
)
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
    assert state.name == "Kitchen Window"
    assert state.attributes["device_class"] == "window"
    assert state.attributes["dahua_area"] == "Kitchen"

    pircam_id = _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{HALL_PIRCAM}")
    assert pircam_id is not None
    pircam_state = hass.states.get(pircam_id)
    assert pircam_state.name == "Hall PIR Camera"

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

    # Camera platform exposes PIRCam snapshot camera by default.
    assert (
        _entity_id(hass, "camera", f"{SERIAL}_pircam_{HALL_PIRCAM}_latest_snapshot")
        is not None
    )
    # Research button platform stays empty unless research mode is enabled.
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
    assert device.name == "Alarm Panel"
    assert device.model == "ARC3800H"
    assert device.configuration_url == "http://192.0.2.10"

    assert await hass.config_entries.async_unload(entry.entry_id)
    hub.stop.assert_called_once()


async def test_arm_state_entities_follow_arm_events(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)
    system = _entity_id(hass, "sensor", f"{SERIAL}_arm_state")
    kitchen = _entity_id(hass, "sensor", f"{SERIAL}_area_1_arm_state")
    garage = _entity_id(hass, "sensor", f"{SERIAL}_area_2_arm_state")
    failure = _entity_id(hass, "sensor", f"{SERIAL}_last_arming_failure")
    # Disabled Dahua areas get no entity.
    assert _entity_id(hass, "sensor", f"{SERIAL}_area_3_arm_state") is None
    assert garage == "sensor.alarm_panel_garage_arm_state"
    assert hass.states.get(system).attributes["options"] == [
        "disarmed",
        "armed_home",
        "armed_away",
        "armed_partial_2",
        "mixed",
    ]
    # The ARC reports changes only: unknown until the first one.
    for entity_id in (system, kitchen, garage, failure):
        assert hass.states.get(entity_id).state == "unknown"

    def pulse(code: str, index: int, mode: str, **data) -> dict:
        return {
            "Action": "Pulse",
            "Code": code,
            "Index": index,
            "Data": {"Mode": mode, "TriggerMode": "Remote", **data},
        }

    open_window = {
        "detail": [
            {
                "Area": 2,
                "AreaName": "Garage ",
                "ZoneAbnormal": [
                    {"Index": 11, "Name": "Garage Door", "Reason": "Open"}
                ],
            }
        ]
    }
    events = [
        pulse("GlobalArmingFailure", -1, "p1", IsGlobal=True, Abnormal=open_window),
        pulse("GlobalAreaArmModeChange", -1, "p1", IsGlobal=True, Profile="Force"),
        pulse("AreaArmModeChange", 0, "p1", IsGlobal=True, Profile="Auto"),
        pulse(
            "AreaArmModeChange",
            1,
            "p1",
            IsGlobal=True,
            Profile="Force",
            Abnormal=open_window,
        ),
    ]

    # Engine callbacks arrive from a worker thread.
    def feed() -> None:
        for event in events:
            hub.engine._apply_event(event)

    worker = threading.Thread(target=feed)
    worker.start()
    worker.join()
    await hass.async_block_till_done()

    assert hass.states.get(system).state == "armed_home"
    assert hass.states.get(system).attributes["armed_areas"] == ["Kitchen", "Garage"]
    garage_state = hass.states.get(garage)
    assert garage_state.state == "armed_home"
    assert garage_state.attributes["forced"] is True
    assert garage_state.attributes["bypassed_zones"] == ["Garage Door"]
    assert garage_state.attributes["dahua_mode"] == "p1"
    assert hass.states.get(kitchen).attributes["forced"] is False
    failure_state = hass.states.get(failure)
    assert failure_state.state != "unknown"
    assert failure_state.attributes["requested_mode"] == "armed_home"
    assert failure_state.attributes["open_zones"] == [
        {"area": "Garage", "zone": "Garage Door", "reason": "Open"}
    ]

    # A reconnect forgets the arm state: changes may have been missed.
    worker = threading.Thread(target=hub.engine.begin_generation, args=(2,))
    worker.start()
    worker.join()
    await hass.async_block_till_done()
    assert hass.states.get(system).state == "unknown"
    assert hass.states.get(garage).state == "unknown"

    hub.realtime.connected = False
    hub._notify(None)
    await hass.async_block_till_done()
    assert hass.states.get(system).state == "unavailable"
    await hass.config_entries.async_unload(entry.entry_id)
    # Unloaded entities stop listening for arm changes.
    assert not hub._arm_listeners


async def test_alarm_entities_follow_alarm_events(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)
    system = _entity_id(hass, "binary_sensor", f"{SERIAL}_alarm")
    kitchen = _entity_id(hass, "binary_sensor", f"{SERIAL}_area_1_alarm")
    garage = _entity_id(hass, "binary_sensor", f"{SERIAL}_area_2_alarm")
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_area_3_alarm") is None
    assert garage == "binary_sensor.alarm_panel_garage_alarm"
    assert hass.states.get(system).attributes["device_class"] == "safety"
    # Unknown until the arm state is known.
    for entity_id in (system, kitchen, garage):
        assert hass.states.get(entity_id).state == "unknown"

    def run(*events: dict) -> None:
        def feed() -> None:
            for event in events:
                hub.engine._apply_event(event)

        worker = threading.Thread(target=feed)
        worker.start()
        worker.join()

    def arm(mode: str) -> list[dict]:
        return [
            {
                "Action": "Pulse",
                "Code": "AreaArmModeChange",
                "Index": index,
                "Data": {"Mode": mode, "TriggerMode": "Remote"},
            }
            for index in (0, 1)
        ]

    run(*arm("p1"))
    await hass.async_block_till_done()
    assert hass.states.get(system).state == STATE_OFF
    assert hass.states.get(garage).state == STATE_OFF

    run(
        {
            "Action": "Start",
            "Code": "AlarmLocal",
            "Index": 11,
            "Data": {
                "AlarmType": "Intrusion",
                "AreaInfo": [{"Index": 1, "Name": "Garage "}],
                "Areas": [1],
                "DevType": "MultiIOTransmitterP",
                "Name": "Garage Door",
            },
        }
    )
    await hass.async_block_till_done()
    system_state = hass.states.get(system)
    assert system_state.state == "on"
    assert system_state.attributes["alarm_areas"] == ["Garage"]
    assert system_state.attributes["last_alarm_zone"] == "Garage Door"
    [zone] = system_state.attributes["alarm_zones"]
    assert (zone["area"], zone["zone"], zone["alarm_type"]) == (
        "Garage",
        "Garage Door",
        "Intrusion",
    )
    garage_state = hass.states.get(garage)
    assert garage_state.state == "on"
    assert garage_state.attributes["alarm_started"] is not None
    assert hass.states.get(kitchen).state == STATE_OFF

    run(*arm("D"))
    await hass.async_block_till_done()
    assert hass.states.get(system).state == STATE_OFF
    garage_state = hass.states.get(garage)
    assert garage_state.state == STATE_OFF
    assert garage_state.attributes["alarm_ended"] is not None
    assert garage_state.attributes["alarm_zones"][0]["zone"] == "Garage Door"

    await hass.config_entries.async_unload(entry.entry_id)
    assert not hub._arm_listeners


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


async def test_wired_zone_fault_sensor_follows_sensor_abnormal_events(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    hub = make_hub()
    await _setup(hass, entry, hub)

    fault_id = _entity_id(
        hass, "binary_sensor", f"{SERIAL}_zone_{KITCHEN_WINDOW}_fault"
    )
    assert fault_id is not None
    assert er.async_get(hass).async_get(fault_id).entity_category == "diagnostic"
    assert hass.states.get(fault_id).state == STATE_OFF
    # Wireless zones keep their radio health entities instead.
    assert (
        _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{HALL_PIRCAM}_fault") is None
    )

    zone_id = _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KITCHEN_WINDOW}")
    zone_state = hass.states.get(zone_id).state
    event = {
        "Code": "SensorAbnormal",
        "Index": KITCHEN_WINDOW,
        "Data": {"Status": "Break"},
    }
    hub.engine._apply_event({**event, "Action": "Start"})
    await hass.async_block_till_done()
    state = hass.states.get(fault_id)
    assert state.state == "on"
    assert state.attributes["status"] == "Break"
    assert hass.states.get(zone_id).state == zone_state
    assert hub.diagnostics()["zone_states"][KITCHEN_WINDOW]["sensor_fault"] == "Break"

    # A fault active across a reconnect may have ended unseen.
    hub.engine.begin_generation(2)
    await hass.async_block_till_done()
    assert hass.states.get(fault_id).state == STATE_UNKNOWN

    hub.engine._apply_event({**event, "Action": "Stop"})
    await hass.async_block_till_done()
    state = hass.states.get(fault_id)
    assert state.state == STATE_OFF
    assert state.attributes["status"] is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_keyfob_with_stale_pircam_row_gets_no_sensor_or_camera(
    hass: HomeAssistant,
) -> None:
    """A re-paired fob can keep its slot's old "PIRCam" SenseMethod."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    # Entities an earlier build created from that stale row.
    registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{SERIAL}_zone_{KEYFOB}", config_entry=entry
    )
    registry.async_get_or_create(
        "camera",
        DOMAIN,
        f"{SERIAL}_pircam_{KEYFOB}_latest_snapshot",
        config_entry=entry,
    )
    hub = make_hub(stale_fob_pircam=True)
    assert hub.zones[KEYFOB].sense_method == "PIRCam"
    assert KEYFOB not in hub.primary_zones
    assert hub.radio_devices[4].classification == "keyfob"
    await _setup(hass, entry, hub)

    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{KEYFOB}") is None
    assert (
        _entity_id(hass, "camera", f"{SERIAL}_pircam_{KEYFOB}_latest_snapshot") is None
    )
    # The real PIR camera keeps its sensor and snapshot camera.
    assert _entity_id(hass, "binary_sensor", f"{SERIAL}_zone_{HALL_PIRCAM}")
    assert _entity_id(hass, "camera", f"{SERIAL}_pircam_{HALL_PIRCAM}_latest_snapshot")
    # The fob still gets its radio health entities, like any paired device.
    radio_uids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.unique_id.endswith("_tamper")
    }
    assert len(radio_uids) == 3  # MultiIO board, PIRCam, keyfob
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
    with patch.object(hub, "refresh_arm_state_probe") as probe:
        result = await async_get_config_entry_diagnostics(hass, entry)
    # The arm-state tables are re-read on every download, research mode or not.
    probe.assert_called_once()
    assert result["runtime"]["arming"]["areas"][1]["name"] == "Kitchen"
    text = str(result)
    assert "192.0.2.10" not in str(result["entry"])
    assert "admin" not in str(result["entry"])
    assert "test-only" not in text
    assert SERIAL not in text
    assert "PIRCAM-SN-1" not in text
    assert "FOB-SN-1" not in text
    assert result["runtime"]["inventory_summary"]["exposed_alarm_inputs"] == 2


async def test_zone_binary_sensor_naming_and_dedication(hass: HomeAssistant) -> None:
    entry = _entry(hass, title="My Alarm System")
    hub = make_hub()

    # 1. MultiIO zone with child device: dedicated, main entity (_attr_name = None)
    multiio_zone = Zone(
        index=1,
        name="Kitchen Window",
        classification="multiio_input",
        is_multiio=True,
        level1=2,
        level2=1,
    )
    hub.child_device_ids = {1: "child-dev-1"}
    hub.multiio_parent_ids = {2: "parent-dev-2"}
    info, is_dedicated = _device_info_for_zone(
        hub, "uid", multiio_zone, default_name=entry
    )
    assert is_dedicated is True
    sensor = DahuaArcZoneBinarySensor(hub, entry, multiio_zone)
    assert sensor._attr_name is None

    # 2. Radio device carrying only this single primary zone: dedicated (_attr_name = None)
    # HALL_PIRCAM is already on level1=3 in make_hub() and is the only primary zone on level1=3.
    hall_pircam_zone = hub.zones[HALL_PIRCAM]
    info, is_dedicated = _device_info_for_zone(
        hub, "uid", hall_pircam_zone, default_name=entry
    )
    assert is_dedicated is True
    sensor_single = DahuaArcZoneBinarySensor(hub, entry, hall_pircam_zone)
    assert sensor_single._attr_name is None

    # Also test an added radio device on level1=5 with a single primary zone:
    radio_single = RadioDeviceInfo(
        level1=5,
        alarm_index=20,
        name="Front Door Contact",
        sense_method="DoorContact",
        classification="contact",
    )
    single_zone = Zone(
        index=20,
        name="Front Door Contact",
        classification="contact",
        is_multiio=False,
        level1=5,
        level2=0,
    )
    hub.radio_devices[5] = radio_single
    hub.zones[20] = single_zone
    info, is_dedicated = _device_info_for_zone(
        hub, "uid", single_zone, default_name=entry
    )
    assert is_dedicated is True
    assert info["name"] == "Front Door Contact"
    sensor_added = DahuaArcZoneBinarySensor(hub, entry, single_zone)
    assert sensor_added._attr_name is None

    # 3. Radio device shared with multiple primary inputs: not dedicated (_attr_name = zone.name)
    radio_shared = RadioDeviceInfo(
        level1=4,
        alarm_index=3,
        name="Dual Input Board",
        sense_method="Transmitter",
        classification="contact",
    )
    shared_zone_a = Zone(
        index=3,
        name="Board Input 1",
        classification="contact",
        is_multiio=False,
        level1=4,
        level2=1,
    )
    shared_zone_b = Zone(
        index=4,
        name="Board Input 2",
        classification="contact",
        is_multiio=False,
        level1=4,
        level2=2,
    )
    hub.radio_devices[4] = radio_shared
    hub.zones[3] = shared_zone_a
    hub.zones[4] = shared_zone_b
    info_a, is_dedicated_a = _device_info_for_zone(
        hub, "uid", shared_zone_a, default_name=entry
    )
    info_b, is_dedicated_b = _device_info_for_zone(
        hub, "uid", shared_zone_b, default_name=entry
    )
    assert is_dedicated_a is False
    assert is_dedicated_b is False
    assert info_a["name"] == "Dual Input Board"
    assert info_b["name"] == "Dual Input Board"
    sensor_a = DahuaArcZoneBinarySensor(hub, entry, shared_zone_a)
    sensor_b = DahuaArcZoneBinarySensor(hub, entry, shared_zone_b)
    assert sensor_a._attr_name == "Board Input 1"
    assert sensor_b._attr_name == "Board Input 2"

    # 4. Zone fallback to panel root device: not dedicated (_attr_name = zone.name)
    root_zone = Zone(
        index=5,
        name="Panel Wired Loop 1",
        classification="wired_input",
        is_multiio=False,
        level1=-1,
        level2=-1,
    )
    hub.zones[5] = root_zone
    info_root, is_dedicated_root = _device_info_for_zone(
        hub, "uid", root_zone, default_name=entry
    )
    assert is_dedicated_root is False
    assert info_root["name"] == "My Alarm System"
    assert info_root["model"] == hub.device_type
    sensor_root = DahuaArcZoneBinarySensor(hub, entry, root_zone)
    assert sensor_root._attr_name == "Panel Wired Loop 1"

    # 5. Root device naming preserves readable title or falls back to "Alarm Panel"
    fallback_info = root_device_info(hub, "uid")
    assert fallback_info["name"] == "Alarm Panel"
    assert fallback_info["model"] == hub.device_type

    titled_info = root_device_info(hub, "uid", entry)
    assert titled_info["name"] == "My Alarm System"
    assert titled_info["model"] == hub.device_type
