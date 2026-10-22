"""Arming through a relay-driven keyfob, at the Home Assistant layer.

The relay entities are faked with Home Assistant services whose handlers make
the in-process fake ARC behave like a real keyfob arm, so the whole path is
exercised: panel -> controller -> sender -> press service -> ARC events ->
confirmation. No arm RPC exists in this path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.dahua_arc.const import (
    CONF_ARM_BACKEND,
    CONF_ARM_CODE_ACK_NO_CODE,
    CONF_ARM_CODE_HASH,
    CONF_ENABLE_ARM_CONTROL,
    CONF_KEYFOB_ALLOW_DISARM,
    CONF_KEYFOB_AWAY_ENTITY,
    CONF_KEYFOB_DISARM_ENTITY,
    CONF_KEYFOB_HOME_ENTITY,
    CONF_KEYFOB_PRESS_MS,
    CONF_KEYFOB_WAIT_SECONDS,
    DOMAIN,
    ISSUE_ARM_CONTROL_UNSUPPORTED,
    ISSUE_KEYFOB_ENTITY_MISSING,
)
from custom_components.dahua_arc.diagnostics import async_get_config_entry_diagnostics
from custom_components.dahua_arc.protocol.snapshot import SnapshotClient
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.fake_arc import ARM_METHOD, FakeArc
from tests.ha.test_arm_control import (
    CODE,
    CODE_HASH,
    DATA,
    SYSTEM_UID,
    WINDOW,
    Harness,
    _all_panels,
    _arc,
)
from tests.hub_factory import SERIAL, make_hub

HOME = "button.keyfob_home"
AWAY = "button.keyfob_away"
DISARM = "button.keyfob_disarm"
ENTITY_MODE = {HOME: "p1", AWAY: "T", DISARM: "D"}
KEYFOB_OPTIONS = {
    CONF_ENABLE_ARM_CONTROL: True,
    CONF_ARM_CODE_ACK_NO_CODE: True,
    CONF_ARM_BACKEND: "keyfob",
    CONF_KEYFOB_HOME_ENTITY: HOME,
    CONF_KEYFOB_AWAY_ENTITY: AWAY,
    CONF_KEYFOB_WAIT_SECONDS: 1,
}


class Relay:
    """Fake relay services: a press makes the fake ARC behave like a keyfob."""

    def __init__(self, hass: HomeAssistant, arc: FakeArc) -> None:
        self.hass = hass
        self.arc = arc
        self.calls: list[tuple[str, str, str]] = []
        self.fail_with: Exception | None = None

    def _handler(self, domain: str, service: str) -> Callable[[ServiceCall], None]:
        def handle(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            self.calls.append((domain, service, entity_id))
            if self.fail_with is not None:
                raise self.fail_with
            if service in ("press", "turn_on"):
                self.arc.keyfob_press(ENTITY_MODE[entity_id])

        return handle

    def register(self) -> None:
        for domain, service in (
            ("button", "press"),
            ("input_button", "press"),
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("script", "turn_on"),
        ):
            self.hass.services.async_register(
                domain, service, self._handler(domain, service)
            )

    def pressed(self) -> list[str]:
        """Entities pressed, in order (a switch counts once, on its turn_on)."""
        return [e for d, s, e in self.calls if s in ("press", "turn_on")]


@asynccontextmanager
async def keyfob_harness(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    areas: int = 2,
    entities: tuple[str, ...] = (HOME, AWAY, DISARM),
) -> AsyncIterator[tuple[Harness, Relay]]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=DATA,
        options={**KEYFOB_OPTIONS, **(options or {})},
        version=5,
    )
    entry.add_to_hass(hass)
    hub = make_hub(arm_control=True, arm_spec=False)
    arc = _arc(areas)
    if areas == 1:
        del hub.arming.areas[1]
    arc.event_listeners.append(hub.engine._apply_event)
    hub.arming.apply_table(
        {index: "D" for index in hub.arming.areas}, hub.arming.watermark()
    )
    relay = Relay(hass, arc)
    for entity_id in entities:
        hass.states.async_set(entity_id, "unknown")
    with arc.patch(), patch("custom_components.dahua_arc.ArcHub", return_value=hub):
        hub.snapshot_client = SnapshotClient(hub.host, 5000, "admin", "test-only")
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # After setup: the integration's own button platform registers the real
        # button.press service, which these fakes then replace.
        relay.register()
        try:
            yield Harness(hass, entry, hub, arc), relay
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            hub.snapshot_client.close()
            arc.close_timers()


# -- layout ---------------------------------------------------------------------


async def test_keyfob_backend_has_only_the_system_panel_and_sends_no_rpc(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass) as (h, relay):
        assert _all_panels(hass) == ["alarm_control_panel.arc3800h"]
        assert h.hub.arm_control is not None
        assert h.hub.arm_control.sender is not None
        assert h.hub.arm_control_unsupported is False
        assert h.attributes(SYSTEM_UID)["supported_features"] == 3
        issues = ir.async_get(hass)
        assert (
            issues.async_get_issue(
                DOMAIN, f"{h.entry.entry_id}_{ISSUE_ARM_CONTROL_UNSUPPORTED}"
            )
            is None
        )
        assert relay.calls == []  # setup never presses anything
        assert ARM_METHOD not in h.arc.calls


async def test_only_mapped_modes_are_offered(hass: HomeAssistant) -> None:
    async with keyfob_harness(
        hass,
        options={CONF_KEYFOB_AWAY_ENTITY: None, "arm_modes": ["armed_home"]},
    ) as (h, _relay):
        assert h.attributes(SYSTEM_UID)["supported_features"] == 1  # arm home only


# -- commands -------------------------------------------------------------------


async def test_arm_away_presses_the_away_button_once_and_is_confirmed(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass) as (h, relay):
        await h.call("alarm_arm_away")
        await hass.async_block_till_done()
        assert relay.pressed() == [AWAY]
        assert h.state(SYSTEM_UID) == "armed_away"
        attributes = h.attributes(SYSTEM_UID)
        assert attributes["changed_by"] == "Home Assistant"
        assert attributes["last_command"]["outcome"] == "confirmed"
        assert attributes["last_command"]["confirmed_by"] == "event"
        assert h.requests() == []
        assert ARM_METHOD not in h.arc.calls


async def test_arm_home_presses_the_home_button(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass) as (h, relay):
        await h.call("alarm_arm_home")
        await hass.async_block_till_done()
        assert relay.pressed() == [HOME]
        assert h.state(SYSTEM_UID) == "armed_home"


async def test_a_repeated_arm_presses_nothing_more(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass) as (h, relay):
        await h.call("alarm_arm_away")
        await h.call("alarm_arm_away")
        assert relay.pressed() == [AWAY]


async def test_disarm_is_off_by_default_and_presses_nothing(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass) as (h, relay):
        await h.call("alarm_arm_away")
        with pytest.raises(ServiceValidationError) as raised:
            await h.call("alarm_disarm")
        assert raised.value.translation_key == "disarm_not_enabled"
        assert relay.pressed() == [AWAY]
        assert h.state(SYSTEM_UID) == "armed_away"


async def test_disarm_when_enabled_still_needs_the_code(hass: HomeAssistant) -> None:
    options = {
        CONF_KEYFOB_ALLOW_DISARM: True,
        CONF_KEYFOB_DISARM_ENTITY: DISARM,
        CONF_ARM_CODE_HASH: CODE_HASH,
    }
    async with keyfob_harness(hass, options=options) as (h, relay):
        h.hub.keyfob_sender._min_gap = 0.0  # this test is not about the gap
        await h.call("alarm_arm_away")
        with pytest.raises(ServiceValidationError) as raised:
            await h.call("alarm_disarm")
        assert raised.value.translation_key == "code_required"
        with pytest.raises(ServiceValidationError) as wrong:
            await h.call("alarm_disarm", code="0000")
        assert wrong.value.translation_key == "invalid_code"
        assert relay.pressed() == [AWAY]
        await h.call("alarm_disarm", code=CODE)
        await hass.async_block_till_done()
        assert relay.pressed() == [AWAY, DISARM]
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_a_refused_arm_names_the_open_zone(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass) as (h, relay):
        h.arc.open_zones = [WINDOW]
        with pytest.raises(HomeAssistantError) as raised:
            await h.call("alarm_arm_away")
        assert raised.value.translation_key == "arm_refused_open_zones"
        assert "Kitchen Window" in raised.value.translation_placeholders["zones"]
        assert relay.pressed() == [AWAY]
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_no_confirmation_is_reported_and_never_pressed_again(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass, options={CONF_KEYFOB_WAIT_SECONDS: 0.3}) as (
        h,
        relay,
    ):
        h.arc.arm_takes_effect = False
        with pytest.raises(HomeAssistantError) as raised:
            await h.call("alarm_arm_away")
        assert raised.value.translation_key == "arm_unconfirmed_keyfob"
        assert relay.pressed() == [AWAY]
        assert h.state(SYSTEM_UID) == "disarmed"


async def test_an_unavailable_relay_presses_nothing(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass) as (h, relay):
        hass.states.async_set(AWAY, "unavailable")
        with pytest.raises(HomeAssistantError) as raised:
            await h.call("alarm_arm_away")
        assert raised.value.translation_key == "arm_relay_unavailable"
        assert raised.value.translation_placeholders["entity"] == AWAY
        assert relay.calls == []


async def test_a_failing_press_service_is_reported_without_a_retry(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass) as (h, relay):
        relay.fail_with = HomeAssistantError("relay offline")
        with pytest.raises(HomeAssistantError) as raised:
            await h.call("alarm_arm_away")
        assert raised.value.translation_key == "arm_press_failed"
        assert len(relay.calls) == 1


async def test_a_second_command_inside_the_gap_is_refused(hass: HomeAssistant) -> None:
    options = {
        CONF_KEYFOB_ALLOW_DISARM: True,
        CONF_KEYFOB_DISARM_ENTITY: DISARM,
    }
    async with keyfob_harness(hass, options=options) as (h, relay):
        await h.call("alarm_arm_away")
        with pytest.raises(HomeAssistantError) as raised:
            await h.call("alarm_disarm")
        assert raised.value.translation_key == "arm_too_soon"
        assert relay.pressed() == [AWAY]


# -- entity domains -------------------------------------------------------------


async def test_a_switch_is_turned_on_held_and_turned_off(hass: HomeAssistant) -> None:
    switch = "switch.keyfob_away"
    ENTITY_MODE[switch] = "T"
    try:
        async with keyfob_harness(
            hass,
            options={CONF_KEYFOB_AWAY_ENTITY: switch, CONF_KEYFOB_PRESS_MS: 100},
            entities=(HOME, switch),
        ) as (h, relay):
            await h.call("alarm_arm_away")
            await hass.async_block_till_done()
            assert [(d, s) for d, s, _e in relay.calls] == [
                ("switch", "turn_on"),
                ("switch", "turn_off"),
            ]
            assert h.state(SYSTEM_UID) == "armed_away"
    finally:
        del ENTITY_MODE[switch]


async def test_a_script_is_started(hass: HomeAssistant) -> None:
    script = "script.keyfob_home"
    ENTITY_MODE[script] = "p1"
    try:
        async with keyfob_harness(
            hass,
            options={CONF_KEYFOB_HOME_ENTITY: script},
            entities=(script, AWAY),
        ) as (h, relay):
            await h.call("alarm_arm_home")
            await hass.async_block_till_done()
            assert relay.calls == [("script", "turn_on", script)]
            assert h.state(SYSTEM_UID) == "armed_home"
    finally:
        del ENTITY_MODE[script]


# -- repairs and diagnostics ----------------------------------------------------


async def test_a_missing_relay_entity_raises_a_repair(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass, entities=(HOME,)) as (h, _relay):
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"{h.entry.entry_id}_{ISSUE_KEYFOB_ENTITY_MISSING}"
        )
        assert issue is not None
        assert issue.translation_placeholders == {"entities": AWAY}


async def test_no_repair_when_every_relay_entity_exists(hass: HomeAssistant) -> None:
    async with keyfob_harness(hass) as (h, _relay):
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"{h.entry.entry_id}_{ISSUE_KEYFOB_ENTITY_MISSING}"
            )
            is None
        )


async def test_diagnostics_describe_the_backend_without_entity_ids(
    hass: HomeAssistant,
) -> None:
    async with keyfob_harness(hass) as (h, _relay):
        with patch.object(h.hub, "refresh_arm_state_probe"):
            result = await async_get_config_entry_diagnostics(hass, h.entry)
        options = result["arm_control_options"]
        assert options["backend"] == "keyfob"
        assert options["keyfob"]["modes_mapped"] == ["p1", "T"]
        assert options["keyfob"]["disarm_allowed"] is False
        arm = result["runtime"]["arm_control"]
        assert arm["sender"]["name"] == "keyfob"
        assert arm["command_spec"] is None
        dumped = json.dumps(result, default=str)
        for entity_id in (HOME, AWAY, DISARM):
            assert entity_id not in dumped


async def test_the_default_backend_is_unchanged(hass: HomeAssistant) -> None:
    """Without the keyfob backend the panel layout is the existing one."""
    async with keyfob_harness(
        hass, options={CONF_ARM_BACKEND: "dhip"}, entities=()
    ) as (h, relay):
        assert h.hub.keyfob_sender is None
        assert h.hub.arm_control is None  # no verified RPC: inert, as before
        assert _all_panels(hass) == []
        assert relay.calls == []
