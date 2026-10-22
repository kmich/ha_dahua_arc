"""Home Assistant side of arming through a relay-driven keyfob.

The protocol layer (:mod:`.protocol.keyfob`) knows nothing about Home
Assistant. This module supplies the two callables it needs, a blocking
``press`` and an ``is_ready`` check, built from the user's chosen entities, and
builds the sender from the config entry's options.

A press only ever happens inside an arm or disarm command that a user or an
automation asked for. Nothing here runs on setup, reload or a timer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    ARM_BACKEND_KEYFOB,
    CONF_ARM_BACKEND,
    CONF_ENABLE_ARM_CONTROL,
    CONF_KEYFOB_AWAY_ENTITY,
    CONF_KEYFOB_DISARM_ENTITY,
    CONF_KEYFOB_HOME_ENTITY,
    CONF_KEYFOB_PRESS_MS,
    DEFAULT_ARM_BACKEND,
    DEFAULT_ENABLE_ARM_CONTROL,
    DEFAULT_KEYFOB_PRESS_MS,
    KEYFOB_PRESS_DOMAINS,
)
from .protocol.keyfob import KeyfobButtons, KeyfobSender

_LOGGER = logging.getLogger(__name__)

# Time allowed for the Home Assistant service call itself, on top of any hold.
SERVICE_TIMEOUT_SECONDS = 10.0


def keyfob_enabled(options: Mapping[str, Any]) -> bool:
    return bool(options.get(CONF_ENABLE_ARM_CONTROL, DEFAULT_ENABLE_ARM_CONTROL)) and (
        options.get(CONF_ARM_BACKEND, DEFAULT_ARM_BACKEND) == ARM_BACKEND_KEYFOB
    )


def keyfob_buttons(options: Mapping[str, Any]) -> KeyfobButtons:
    return KeyfobButtons(
        home=options.get(CONF_KEYFOB_HOME_ENTITY) or None,
        away=options.get(CONF_KEYFOB_AWAY_ENTITY) or None,
        disarm=options.get(CONF_KEYFOB_DISARM_ENTITY) or None,
    )


def configured_entities(options: Mapping[str, Any]) -> list[str]:
    buttons = keyfob_buttons(options)
    return [e for e in (buttons.home, buttons.away, buttons.disarm) if e]


async def _async_press(hass: HomeAssistant, entity_id: str, hold_ms: int) -> None:
    """Press one entity once, by its domain."""
    domain = entity_id.partition(".")[0]
    if domain not in KEYFOB_PRESS_DOMAINS:
        raise ValueError(f"{entity_id} cannot press a keyfob button")
    target = {"entity_id": entity_id}
    if domain in ("button", "input_button"):
        await hass.services.async_call(domain, "press", target, blocking=True)
    elif domain == "script":
        await hass.services.async_call("script", "turn_on", target, blocking=True)
    else:
        # A switch is a momentary contact: on, hold, off. The off is attempted
        # even if something goes wrong while holding.
        await hass.services.async_call("switch", "turn_on", target, blocking=True)
        try:
            await asyncio.sleep(hold_ms / 1000)
        finally:
            await hass.services.async_call("switch", "turn_off", target, blocking=True)


def make_press(hass: HomeAssistant, hold_ms: int) -> Callable[[str], None]:
    """A blocking press for use from the controller's executor thread."""

    def press(entity_id: str) -> None:
        future = asyncio.run_coroutine_threadsafe(
            _async_press(hass, entity_id, hold_ms), hass.loop
        )
        future.result(SERVICE_TIMEOUT_SECONDS + hold_ms / 1000)

    return press


def make_is_ready(hass: HomeAssistant) -> Callable[[str], bool]:
    """True when the entity exists and is not ``unavailable``.

    ``unknown`` is normal for a button that has never been pressed, so it still
    counts as ready.
    """

    def is_ready(entity_id: str) -> bool:
        state = hass.states.get(entity_id)
        return state is not None and state.state != "unavailable"

    return is_ready


def build_sender(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> KeyfobSender | None:
    """The keyfob sender for these options, or None when keyfob mode is off."""
    if not keyfob_enabled(options):
        return None
    hold_ms = int(options.get(CONF_KEYFOB_PRESS_MS, DEFAULT_KEYFOB_PRESS_MS))
    return KeyfobSender(
        keyfob_buttons(options),
        make_press(hass, hold_ms),
        make_is_ready(hass),
    )
