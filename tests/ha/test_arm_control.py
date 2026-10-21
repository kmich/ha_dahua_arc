"""Opt-in arm/disarm control through the Home Assistant alarm panels.

Everything runs against FAKE_COMMAND_SPEC and the in-process fake ARC. No real
arm RPC exists in the integration yet.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.dahua_arc import arm_code
from custom_components.dahua_arc.alarm_control_panel import panel_state
from custom_components.dahua_arc.const import (
    CONF_ARC_SERIAL,
    CONF_ARM_CODE_ACK_NO_CODE,
    CONF_ARM_CODE_HASH,
    CONF_CODE_ARM_REQUIRED,
    CONF_CODE_DISARM_REQUIRED,
    CONF_ENABLE_ARM_CONTROL,
    DOMAIN,
    ISSUE_ARM_CONTROL_UNSUPPORTED,
    ISSUE_ARM_CONTROL_WITHOUT_CODE,
)
from custom_components.dahua_arc.diagnostics import async_get_config_entry_diagnostics
from custom_components.dahua_arc.protocol.arming import ArmArea
from custom_components.dahua_arc.repairs import async_create_fix_flow
from homeassistant.components.alarm_control_panel import DATA_COMPONENT
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState as State,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.fake_arc import ARM_METHOD, FakeArc
from tests.hub_factory import KITCHEN_WINDOW, SERIAL, make_hub

DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "test-only",
    CONF_ARC_SERIAL: SERIAL,
}
CODE = "1234"
CODE_HASH = arm_code.hash_code(CODE)
WINDOW = {"Index": KITCHEN_WINDOW, "Name": "Kitchen Window", "Reason": "Open"}
SYSTEM_UID = f"{SERIAL}_alarm_panel"
KITCHEN_UID = f"{SERIAL}_area_1_alarm_panel"
GARAGE_UID = f"{SERIAL}_area_2_alarm_panel"


def _arc(areas: int = 2) -> FakeArc:
    subsystems = [
        {"Enable": True, "AreaId": 1, "Name": "Kitchen", "Zone": [7]},
        {"Enable": True, "AreaId": 2, "Name": "Garage ", "Zone": []},
    ][:areas]
    return FakeArc(
        password="test-only",
        inventory={
            "AreaArmMode": {"Areas": [{"Mode": "D"} for _ in subsystems]},
            "AlarmSubSystem": subsystems,
        },
    )


@dataclass
class Harness:
    hass: HomeAssistant
    entry: MockConfigEntry
    hub: Any
    arc: FakeArc

    def entity_id(self, unique_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "alarm_control_panel", DOMAIN, unique_id
        )

    def state(self, unique_id: str) -> str:
        entity_id = self.entity_id(unique_id)
        assert entity_id is not None, unique_id
        return self.hass.states.get(entity_id).state

    def attributes(self, unique_id: str) -> dict[str, Any]:
        return dict(self.hass.states.get(self.entity_id(unique_id)).attributes)

    def entity(self, unique_id: str):
        return self.hass.data[DATA_COMPONENT].get_entity(self.entity_id(unique_id))

    async def call(self, action: str, unique_id: str = SYSTEM_UID, **data: Any):
        return await self.hass.services.async_call(
            "alarm_control_panel",
            action,
            {"entity_id": self.entity_id(unique_id), **data},
            blocking=True,
        )

    def requests(self) -> list[dict[str, Any]]:
        return self.arc.arm_requests


@asynccontextmanager
async def harness(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    areas: int = 2,
    arm_control: bool = True,
    arm_spec: bool = True,
) -> AsyncIterator[Harness]:
    base = {
        CONF_ENABLE_ARM_CONTROL: arm_control,
        CONF_ARM_CODE_ACK_NO_CODE: True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=DATA,
        options={**base, **(options or {})},
        version=5,
    )
    entry.add_to_hass(hass)
    hub = make_hub(arm_control=arm_control, arm_spec=arm_spec)
    arc = _arc(areas)
    if areas == 1:
        del hub.arming.areas[1]
    arc.event_listeners.append(hub.engine._apply_event)
    hub.arming.apply_table(
        {index: "D" for index in hub.arming.areas}, hub.arming.watermark()
    )
    with arc.patch(), patch("custom_components.dahua_arc.ArcHub", return_value=hub):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            yield Harness(hass, entry, hub, arc)
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            if hub.snapshot_client is not None:
                hub.snapshot_client.close()
            arc.close_timers()


def _all_panels(hass: HomeAssistant) -> list[str]:
    return hass.states.async_entity_ids("alarm_control_panel")


# -- opt-in and entity layout -------------------------------------------------


async def test_option_off_creates_no_panels_and_sends_nothing(
    hass: HomeAssistant,
) -> None:
    async with harness(hass, arm_control=False, arm_spec=False) as h:
        assert _all_panels(hass) == []
        assert h.hub.arm_control is None
        assert h.arc.calls == []
        with patch.object(h.hub, "refresh_arm_state_probe"):
            result = await async_get_config_entry_diagnostics(hass, h.entry)
        assert result["runtime"]["arm_control"] is None
        assert ARM_METHOD not in h.arc.calls
        issues = ir.async_get(hass)
        for suffix in (ISSUE_ARM_CONTROL_UNSUPPORTED, ISSUE_ARM_CONTROL_WITHOUT_CODE):
            assert (
                issues.async_get_issue(DOMAIN, f"{h.entry.entry_id}_{suffix}") is None
            )


async def test_a_controller_alone_does_not_create_panels(hass: HomeAssistant) -> None:
    async with harness(hass, options={CONF_ENABLE_ARM_CONTROL: False}) as h:
        assert h.hub.arm_control is not None
        assert _all_panels(hass) == []


async def test_option_on_without_a_verified_command_is_inert(
    hass: HomeAssistant,
) -> None:
    async with harness(hass, arm_spec=False) as h:
        assert h.hub.arm_control is None
        assert h.hub.arm_control_unsupported
        assert _all_panels(hass) == []
        issues = ir.async_get(hass)
        base = h.entry.entry_id
        assert issues.async_get_issue(DOMAIN, f"{base}_{ISSUE_ARM_CONTROL_UNSUPPORTED}")
        assert (
            issues.async_get_issue(DOMAIN, f"{base}_{ISSUE_ARM_CONTROL_WITHOUT_CODE}")
            is None
        )
        with patch.object(h.hub, "refresh_arm_state_probe"):
            result = await async_get_config_entry_diagnostics(hass, h.entry)
        assert result["runtime"]["arm_control"] == {"enabled": True, "supported": False}
        assert h.arc.calls == []


async def test_two_areas_get_a_system_panel_and_one_per_area(
    hass: HomeAssistant,
) -> None:
    async with harness(hass) as h:
        assert h.entity_id(SYSTEM_UID) == "alarm_control_panel.arc3800h"
        assert h.entity_id(KITCHEN_UID) == "alarm_control_panel.arc3800h_kitchen"
        assert h.entity_id(GARAGE_UID) == "alarm_control_panel.arc3800h_garage"
        assert len(_all_panels(hass)) == 3
        # A disabled Dahua area has no panel.
        assert h.entity_id(f"{SERIAL}_area_3_alarm_panel") is None
        assert h.state(SYSTEM_UID) == "disarmed"
        attrs = h.attributes(SYSTEM_UID)
        assert attrs["areas"] == ["Kitchen", "Garage"]
        assert attrs["mixed"] is False
        assert attrs["supported_features"] == 3  # arm home + arm away only
        assert attrs["code_format"] is None
        assert h.attributes(KITCHEN_UID)["area_id"] == 1


async def test_a_single_area_gets_only_the_system_panel(hass: HomeAssistant) -> None:
    async with harness(hass, areas=1) as h:
        assert _all_panels(hass) == ["alarm_control_panel.arc3800h"]
        await h.call("alarm_arm_away")
        assert h.requests() == [{"Mode": "T", "Areas": [0]}]


async def test_existing_unique_ids_are_untouched(hass: HomeAssistant) -> None:
    def unique_ids(h: Harness) -> set[str]:
        registry = er.async_get(h.hass)
        return {
            entity.unique_id
            for entity in er.async_entries_for_config_entry(registry, h.entry.entry_id)
        }

    async with harness(hass, arm_control=False, arm_spec=False) as off:
        before = unique_ids(off)
    async with harness(hass) as on:
        after = unique_ids(on)
    assert before <= after
    assert after - before == {SYSTEM_UID, KITCHEN_UID, GARAGE_UID}


# -- state mapping --------------------------------------------------------------


def _area(index: int, mode: str | None, *, alarm: bool | None = False) -> ArmArea:
    return ArmArea(index=index, name=f"A{index}", raw_mode=mode, alarm=alarm)


@pytest.mark.parametrize(
    ("areas", "pending", "expected", "mixed"),
    [
        ([_area(0, "D"), _area(1, "D")], None, State.DISARMED, False),
        ([_area(0, "p1"), _area(1, "p1")], None, State.ARMED_HOME, False),
        ([_area(0, "T")], None, State.ARMED_AWAY, False),
        ([_area(0, "p2")], None, State.ARMED_NIGHT, False),
        ([_area(0, "p1"), _area(1, "T")], None, State.ARMED_CUSTOM_BYPASS, True),
        ([_area(0, "D"), _area(1, "T")], None, State.ARMED_CUSTOM_BYPASS, True),
        ([_area(0, "T"), _area(1, None)], None, None, False),
        ([_area(0, "T", alarm=None)], None, State.ARMED_AWAY, False),
        ([_area(0, "D", alarm=True)], None, State.TRIGGERED, False),
        ([_area(0, None, alarm=True)], None, State.TRIGGERED, False),
        ([_area(0, "T", alarm=True), _area(1, "D")], None, State.TRIGGERED, False),
        ([_area(0, "D", alarm=True)], "arming", State.ARMING, False),
        ([_area(0, "T")], "disarming", State.DISARMING, False),
        ([], None, None, False),
    ],
)
def test_state_mapping(areas, pending, expected, mixed) -> None:
    assert panel_state(areas, pending) == (expected, mixed)


async def test_panels_follow_events_from_other_sources(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.push_event(
            {
                "Action": "Pulse",
                "Code": "AreaArmModeChange",
                "Index": 1,
                "Data": {"Mode": "T", "TriggerMode": "Keypad"},
            }
        )
        await hass.async_block_till_done()
        assert h.state(GARAGE_UID) == "armed_away"
        assert h.state(KITCHEN_UID) == "disarmed"
        assert h.state(SYSTEM_UID) == "armed_custom_bypass"
        assert h.attributes(SYSTEM_UID)["mixed"] is True
        assert h.attributes(SYSTEM_UID)["area_states"] == {
            "Kitchen": "disarmed",
            "Garage": "armed_away",
        }
        # Not armed by Home Assistant.
        assert h.attributes(GARAGE_UID).get("changed_by") is None

        h.arc.push_event(
            {
                "Action": "Start",
                "Code": "AlarmLocal",
                "Index": KITCHEN_WINDOW,
                "Data": {"Areas": [0], "Name": "Kitchen Window"},
            }
        )
        await hass.async_block_till_done()
        assert h.state(KITCHEN_UID) == "triggered"
        assert h.state(SYSTEM_UID) == "triggered"
        assert h.state(GARAGE_UID) == "armed_away"


# -- commands --------------------------------------------------------------------


async def test_arm_away_shows_arming_then_armed_away(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.arm_delay = 0.4
        task = hass.async_create_task(h.call("alarm_arm_away"))
        await asyncio.sleep(0.15)
        assert h.state(SYSTEM_UID) == "arming"
        await task
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "armed_away"
        assert h.state(KITCHEN_UID) == "armed_away"
        assert h.state(GARAGE_UID) == "armed_away"
        assert h.requests() == [{"Mode": "T", "Areas": [0, 1]}]
        attrs = h.attributes(SYSTEM_UID)
        assert attrs["changed_by"] == "Home Assistant"
        assert attrs["last_command"]["outcome"] == "confirmed"
        assert attrs["last_command"]["confirmed_by"] == "event"

        task = hass.async_create_task(h.call("alarm_disarm"))
        await asyncio.sleep(0.15)
        assert h.state(SYSTEM_UID) == "disarming"
        await task
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "disarmed"
        assert h.requests()[-1] == {"Mode": "D", "Areas": [0, 1]}


async def test_an_area_panel_commands_only_its_own_area(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        await h.call("alarm_arm_home", GARAGE_UID)
        await hass.async_block_till_done()
        assert h.requests() == [{"Mode": "p1", "Areas": [1]}]
        assert h.state(GARAGE_UID) == "armed_home"
        assert h.state(KITCHEN_UID) == "disarmed"
        assert h.state(SYSTEM_UID) == "armed_custom_bypass"


async def test_already_in_that_state_sends_nothing(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        await h.call("alarm_disarm")
        assert h.arc.calls == []
        await h.call("alarm_arm_away")
        await hass.async_block_till_done()
        calls = len(h.arc.calls)
        await h.call("alarm_arm_away")
        assert len(h.arc.calls) == calls
        assert len(h.requests()) == 1


async def test_refusal_names_the_open_zones(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.open_zones = [WINDOW]
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_home", KITCHEN_UID)
        assert error.value.translation_key == "arm_refused_open_zones"
        assert error.value.translation_placeholders == {
            "areas": "Kitchen",
            "zones": "Kitchen Window",
        }
        await hass.async_block_till_done()
        assert h.state(KITCHEN_UID) == "disarmed"
        attrs = h.attributes(KITCHEN_UID)
        assert attrs["last_arming_failure_open_zones"] == ["Kitchen Window"]
        assert attrs["last_command"]["outcome"] == "refused"
        assert len(h.requests()) == 1


async def test_no_permission_is_translated(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.arm_reply = "error"
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_away")
        assert error.value.translation_key == "arm_not_permitted"
        assert len(h.requests()) == 1


async def test_other_arc_errors_carry_the_code(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.arm_reply = "error"
        h.arc.arm_error_code = 1234
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_away")
        assert error.value.translation_key == "arm_failed"
        assert error.value.translation_placeholders["code"] == "1234"
        assert error.value.translation_placeholders["message"] == "No permission"


async def test_unconfirmed_is_reported_and_never_resent(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.hub.arm_control.wait_timeout = 1.0
        h.arc.arm_takes_effect = False
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_away")
        assert error.value.translation_key == "arm_unconfirmed"
        assert error.value.translation_placeholders == {"seconds": "1"}
        assert len(h.requests()) == 1
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_lost_reply_is_reported_as_unreachable(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.hub.arm_control.reply_timeout = 0.2
        h.arc.arm_reply = "no_reply"
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_away")
        assert error.value.translation_key == "arm_unreachable"
        assert len(h.requests()) == 1


async def test_a_second_command_while_busy_is_refused(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.arc.arm_delay = 0.5
        first = hass.async_create_task(h.call("alarm_arm_away", KITCHEN_UID))
        await asyncio.sleep(0.2)
        with pytest.raises(HomeAssistantError) as error:
            await h.call("alarm_arm_home", GARAGE_UID)
        assert error.value.translation_key == "arm_busy"
        await first
        assert len(h.requests()) == 1


async def test_panels_are_unavailable_while_the_realtime_stream_is_down(
    hass: HomeAssistant,
) -> None:
    async with harness(hass) as h:
        h.hub.realtime.connected = False
        h.hub._notify(None)
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "unavailable"
        assert h.state(KITCHEN_UID) == "unavailable"
        with pytest.raises(HomeAssistantError) as error:
            await h.entity(SYSTEM_UID).async_alarm_arm_away()
        assert error.value.translation_key == "arm_unavailable"
        assert h.arc.calls == []

        h.hub.realtime.connected = True
        h.hub._notify(None)
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_failed_credentials_make_panels_unavailable(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        h.hub.auth_failed = True
        h.hub._notify(None)
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "unavailable"
        assert h.arc.calls == []


async def test_open_zones_attribute_is_live(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        assert h.attributes(KITCHEN_UID)["open_zones"] == []
        # No zones are assigned to the Garage: nothing is known about it.
        assert h.attributes(GARAGE_UID)["open_zones"] is None
        h.hub.zones[KITCHEN_WINDOW].active = True
        h.hub._notify_arm()
        await hass.async_block_till_done()
        assert h.attributes(KITCHEN_UID)["open_zones"] == ["Kitchen Window"]
        assert h.attributes(SYSTEM_UID)["open_zones"] == ["Kitchen Window"]


# -- code --------------------------------------------------------------------------


async def test_without_a_code_nothing_is_asked(hass: HomeAssistant) -> None:
    async with harness(hass) as h:
        attrs = h.attributes(SYSTEM_UID)
        assert attrs["code_format"] is None
        assert attrs["code_arm_required"] is False


async def test_disarm_code_is_checked_before_anything_is_sent(
    hass: HomeAssistant,
) -> None:
    async with harness(hass, options={CONF_ARM_CODE_HASH: CODE_HASH}) as h:
        attrs = h.attributes(SYSTEM_UID)
        assert attrs["code_format"] == "number"
        assert attrs["code_arm_required"] is False
        await h.call("alarm_arm_away")  # arming needs no code by default
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError) as error:
            await h.call("alarm_disarm")
        assert error.value.translation_key == "code_required"
        with pytest.raises(ServiceValidationError) as error:
            await h.call("alarm_disarm", code="9999")
        assert error.value.translation_key == "invalid_code"
        assert len(h.requests()) == 1  # only the arm reached the fake ARC

        await h.call("alarm_disarm", code=CODE)
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_five_wrong_codes_lock_the_panel(hass: HomeAssistant) -> None:
    async with harness(hass, options={CONF_ARM_CODE_HASH: CODE_HASH}) as h:
        await h.call("alarm_arm_away")
        await hass.async_block_till_done()
        for _ in range(5):
            with pytest.raises(ServiceValidationError) as error:
                await h.call("alarm_disarm", code="0000")
            assert error.value.translation_key == "invalid_code"
        with pytest.raises(ServiceValidationError) as error:
            await h.call("alarm_disarm", code=CODE)
        assert error.value.translation_key == "invalid_code_locked"
        assert 1 <= int(error.value.translation_placeholders["seconds"]) <= 61
        assert len(h.requests()) == 1


async def test_arm_can_require_the_code_too(hass: HomeAssistant) -> None:
    options = {CONF_ARM_CODE_HASH: CODE_HASH, CONF_CODE_ARM_REQUIRED: True}
    async with harness(hass, options=options) as h:
        assert h.attributes(SYSTEM_UID)["code_arm_required"] is True
        with pytest.raises(ServiceValidationError):
            await h.call("alarm_arm_home")
        with pytest.raises(ServiceValidationError) as error:
            await h.call("alarm_arm_home", code="0000")
        assert error.value.translation_key == "invalid_code"
        assert h.requests() == []
        await h.call("alarm_arm_home", code=CODE)
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "armed_home"


async def test_disarm_code_can_be_made_optional(hass: HomeAssistant) -> None:
    options = {CONF_ARM_CODE_HASH: CODE_HASH, CONF_CODE_DISARM_REQUIRED: False}
    async with harness(hass, options=options) as h:
        await h.call("alarm_arm_away")
        await h.call("alarm_disarm")
        await hass.async_block_till_done()
        assert h.state(SYSTEM_UID) == "disarmed"


# -- diagnostics and repairs ----------------------------------------------------------


async def test_diagnostics_have_history_and_no_secrets(hass: HomeAssistant) -> None:
    async with harness(hass, options={CONF_ARM_CODE_HASH: CODE_HASH}) as h:
        await h.call("alarm_arm_away")
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError):
            await h.call("alarm_disarm", code="0000")
        with patch.object(h.hub, "refresh_arm_state_probe"):
            result = await async_get_config_entry_diagnostics(hass, h.entry)
        control = result["runtime"]["arm_control"]
        assert control["enabled"] is True
        assert control["supported"] is True
        assert control["command_spec"] == ARM_METHOD
        assert control["in_flight"] is False
        assert [e["outcome"] for e in control["history"]] == ["confirmed"]
        assert result["arm_control_options"]["code_set"] is True
        text = json.dumps(result, default=str)
        assert "test-only" not in text
        assert CODE_HASH not in text
        assert CODE_HASH.split("$")[3] not in text
        assert f'"{CODE}"' not in text


async def test_repair_issue_when_enabled_without_a_code(hass: HomeAssistant) -> None:
    options = {CONF_ARM_CODE_ACK_NO_CODE: False}
    async with harness(hass, options=options) as h:
        issue_id = f"{h.entry.entry_id}_{ISSUE_ARM_CONTROL_WITHOUT_CODE}"
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.is_fixable

        flow = await async_create_fix_flow(
            hass, issue_id, {"entry_id": h.entry.entry_id}
        )
        flow.hass = hass
        flow.issue_id = issue_id
        flow.data = {"entry_id": h.entry.entry_id}
        result = await flow.async_step_init()
        assert result["step_id"] == "confirm"
        result = await flow.async_step_confirm({})
        assert result["type"] == "create_entry"
        assert h.entry.options[CONF_ARM_CODE_ACK_NO_CODE] is True
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_no_repair_issue_when_a_code_is_set(hass: HomeAssistant) -> None:
    options = {CONF_ARM_CODE_ACK_NO_CODE: False, CONF_ARM_CODE_HASH: CODE_HASH}
    async with harness(hass, options=options) as h:
        issue_id = f"{h.entry.entry_id}_{ISSUE_ARM_CONTROL_WITHOUT_CODE}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
