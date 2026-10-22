"""Arm and disarm by pressing a paired keyfob through relay-driven entities.

The ARC accepts arm and disarm from a paired wireless keyfob. If Home Assistant
presses a keyfob's buttons electrically (a ``button``, ``switch`` or ``script``
entity wired to the keyfob by a relay), the ARC arms itself with all its own
checks and :class:`~.control.ArmController` confirms the result from the ARC's
events, exactly as for any other command.

This module knows nothing about Home Assistant: the integration supplies a
blocking ``press`` callable and an ``is_ready`` check. Nothing here is ever
called on setup, reload or a timer; only a user or automation command reaches
:meth:`KeyfobSender.send`, and it presses at most once and never retries. There
is deliberately no way to press an SOS button.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .control import ArmCommand, SendFailure

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_GAP_SECONDS = 5.0
# ARC mode code -> which configured button presses it.
MODE_BUTTON = {"p1": "home", "T": "away", "D": "disarm"}


@dataclass(frozen=True, slots=True)
class KeyfobButtons:
    """Home Assistant entity ids that press the keyfob's buttons."""

    home: str | None = None
    away: str | None = None
    disarm: str | None = None

    def for_mode(self, mode: str) -> str | None:
        name = MODE_BUTTON.get(mode)
        return getattr(self, name) if name else None

    def mapped_modes(self) -> list[str]:
        return [mode for mode in MODE_BUTTON if self.for_mode(mode)]


class KeyfobSender:
    """Press one keyfob button per command. Implements ``CommandSender``."""

    name = "keyfob"

    def __init__(
        self,
        buttons: KeyfobButtons,
        press: Callable[[str], None],
        is_ready: Callable[[str], bool],
        *,
        min_gap: float = DEFAULT_MIN_GAP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.buttons = buttons
        self._press = press
        self._is_ready = is_ready
        self._min_gap = min_gap
        self._clock = clock
        self._lock = threading.Lock()
        self._last_press: float | None = None

    def send(self, command: ArmCommand) -> SendFailure | None:
        entity_id = self.buttons.for_mode(command.mode)
        if entity_id is None:
            return SendFailure("mode_not_configured", command.mode)
        if not self._is_ready(entity_id):
            return SendFailure("relay_unavailable", entity_id)
        with self._lock:
            now = self._clock()
            if self._last_press is not None and now - self._last_press < self._min_gap:
                return SendFailure("too_soon", f"{self._min_gap:g}s")
            # From here the press may already have happened, even if the call
            # raises, so the gap starts now and a failure is never retried.
            self._last_press = now
        _LOGGER.info("Pressing keyfob %s via %s", MODE_BUTTON[command.mode], entity_id)
        try:
            self._press(entity_id)
        except Exception as exc:
            return SendFailure(
                "press_failed", f"{type(exc).__name__}: {exc}", issued=True
            )
        return None

    def describe(self) -> dict[str, Any]:
        """Facts for diagnostics: which modes are mapped, never entity states."""
        return {
            "name": self.name,
            "mapped_modes": self.buttons.mapped_modes(),
            "min_gap_seconds": self._min_gap,
        }
