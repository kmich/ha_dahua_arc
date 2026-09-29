"""Read-only area arm-state tracking from ARC realtime arm/disarm events.

The ARC pushes ``AreaArmModeChange`` (one per area) and a
``GlobalAreaArmModeChange`` summary whenever areas are armed or disarmed,
and ``ArmingFailure`` / ``GlobalArmingFailure`` when an arm request is
refused, for example because a contact is open. Areas are identified by the
event ``Index``, which is the zero-based position in ``AlarmSubSystem``
(``AreaId - 1``); the ``Abnormal`` zone detail uses the one-based ``AreaId``.

The events only report changes. The current state comes from the
``AreaArmMode`` config table (``Areas[index].Mode``), read whenever the
realtime stream (re)attaches and on every periodic resync. Like zone
snapshots, a table read never overrides an arm event that arrived after the
read began.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .util import safe_int, timestamp

_LOGGER = logging.getLogger(__name__)

AREA_ARM_EVENT = "AreaArmModeChange"
GLOBAL_ARM_EVENT = "GlobalAreaArmModeChange"
AREA_ARM_FAILURE_EVENT = "ArmingFailure"
GLOBAL_ARM_FAILURE_EVENT = "GlobalArmingFailure"
ARM_EVENT_CODES = frozenset(
    {
        AREA_ARM_EVENT,
        GLOBAL_ARM_EVENT,
        AREA_ARM_FAILURE_EVENT,
        GLOBAL_ARM_FAILURE_EVENT,
    }
)

DISARMED = "disarmed"
ARMED_HOME = "armed_home"
ARMED_AWAY = "armed_away"
ARMED_PARTIAL_2 = "armed_partial_2"
MIXED = "mixed"

# Dahua arm-mode codes. "D", "p1" (Home/Stay) and "T" (total/Away) are
# confirmed on ARC3800H hardware; "p2" follows Dahua's naming and is not yet
# verified. An unrecognised code leaves the state unknown and is kept as the
# raw mode for diagnostics.
ARM_MODES = {
    "D": DISARMED,
    "p1": ARMED_HOME,
    "T": ARMED_AWAY,
    "p2": ARMED_PARTIAL_2,
}
AREA_STATES = (DISARMED, ARMED_HOME, ARMED_AWAY, ARMED_PARTIAL_2)
AREA_ARM_MODE_CONFIG = "AreaArmMode"
SOURCE_EVENT = "event"
SOURCE_TABLE = "AreaArmMode table"
SYSTEM_STATES = (*AREA_STATES, MIXED)

# One arm/disarm produces a burst of per-area events within about a second.
# Notifying once per burst keeps the derived system state from passing
# through a transient "mixed" state.
NOTIFY_QUIET_SECONDS = 1.0
NOTIFY_MAX_DELAY_SECONDS = 5.0


def arm_mode_state(raw_mode: str | None) -> str | None:
    return ARM_MODES.get(raw_mode) if raw_mode else None


def parse_abnormal_zones(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``Abnormal.detail[].ZoneAbnormal[]`` into one list of zones."""
    abnormal = data.get("Abnormal")
    if not isinstance(abnormal, dict):
        return []
    zones: list[dict[str, Any]] = []
    for area in abnormal.get("detail") or []:
        if not isinstance(area, dict):
            continue
        area_name = str(area.get("AreaName") or "").strip() or None
        for zone in area.get("ZoneAbnormal") or []:
            if not isinstance(zone, dict):
                continue
            zones.append(
                {
                    "area_id": safe_int(area.get("Area")),
                    "area": area_name,
                    "zone_index": safe_int(zone.get("Index")),
                    "zone": str(zone.get("Name") or "").strip() or None,
                    "reason": zone.get("Reason"),
                }
            )
    return zones


def parse_area_arm_modes(table: Any) -> dict[int, str]:
    """Map area index -> raw mode from the ``AreaArmMode`` config table.

    ``Areas`` has one row per ``AlarmSubSystem`` row, in the same order.
    """
    rows = table.get("Areas") if isinstance(table, dict) else None
    if not isinstance(rows, list):
        raise ValueError("AreaArmMode table has no Areas list")
    modes: dict[int, str] = {}
    for index, row in enumerate(rows):
        if isinstance(row, dict) and row.get("Mode"):
            modes[index] = str(row["Mode"])
    return modes


@dataclass(slots=True)
class ArmFailure:
    at: str
    raw_mode: str | None
    trigger_mode: str | None
    open_zones: list[dict[str, Any]] = field(default_factory=list)

    @property
    def state(self) -> str | None:
        return arm_mode_state(self.raw_mode)


@dataclass(slots=True)
class ArmArea:
    index: int
    name: str
    raw_mode: str | None = None
    source: str | None = None
    last_event_seq: int = 0
    profile: str | None = None
    trigger_mode: str | None = None
    changed_at: str | None = None
    bypassed_zones: list[dict[str, Any]] = field(default_factory=list)
    last_failure: ArmFailure | None = None

    @property
    def area_id(self) -> int:
        return self.index + 1

    @property
    def state(self) -> str | None:
        return arm_mode_state(self.raw_mode)


class ArmingTracker:
    """Thread-safe per-area arm state fed by the realtime event processor."""

    def __init__(
        self,
        areas: dict[int, str],
        change_callback: Callable[[], None] | None = None,
        *,
        quiet_seconds: float = NOTIFY_QUIET_SECONDS,
        max_delay_seconds: float = NOTIFY_MAX_DELAY_SECONDS,
    ):
        self.areas = {
            index: ArmArea(index=index, name=name)
            for index, name in sorted(areas.items())
        }
        self.change_callback = change_callback
        self.quiet_seconds = quiet_seconds
        self.max_delay_seconds = max_delay_seconds
        self.lock = threading.RLock()
        self.last_failure: ArmFailure | None = None
        self.last_global_change: dict[str, Any] | None = None
        self.events_received = 0
        self.unknown_area_events = 0
        self.unknown_modes: set[str] = set()
        self.invalidations = 0
        self.event_sequence = 0
        self.table_reads = 0
        self.table_corrections = 0
        self.table_stale_rejects = 0
        self.last_table_read: str | None = None
        self.last_table_error: str | None = None
        self._timer: threading.Timer | None = None
        self._pending_since: float | None = None

    # -- state -------------------------------------------------------------

    def system_state(self) -> str | None:
        """All areas in one state -> that state; otherwise ``mixed``.

        Unknown while any area's state is unknown.
        """
        with self.lock:
            states = {area.state for area in self.areas.values()}
        if not states or None in states:
            return None
        return states.pop() if len(states) == 1 else MIXED

    def armed_areas(self) -> list[str]:
        with self.lock:
            return [
                area.name
                for area in self.areas.values()
                if area.state not in (None, DISARMED)
            ]

    def watermark(self) -> int:
        """Event sequence to pass to :meth:`apply_table` for a read begun now."""
        with self.lock:
            return self.event_sequence

    def _note_mode(self, raw_mode: str | None) -> None:
        if raw_mode is not None and raw_mode not in ARM_MODES:
            if raw_mode not in self.unknown_modes:
                _LOGGER.warning("Unrecognised ARC arm mode %r", raw_mode)
            self.unknown_modes.add(raw_mode)

    def apply_table(self, modes: dict[int, str], watermark: int) -> None:
        """Apply current modes read from the ``AreaArmMode`` table.

        An area whose arm event arrived after ``watermark`` keeps the event's
        state: the table read may predate it.
        """
        changed = False
        with self.lock:
            self.table_reads += 1
            self.last_table_read = timestamp()
            self.last_table_error = None
            for index, area in self.areas.items():
                raw_mode = modes.get(index)
                if raw_mode is None:
                    continue
                if area.last_event_seq > watermark:
                    self.table_stale_rejects += 1
                    continue
                self._note_mode(raw_mode)
                if area.raw_mode == raw_mode:
                    continue
                if area.raw_mode is not None:
                    # A known state that was wrong: an event was missed.
                    self.table_corrections += 1
                area.raw_mode = raw_mode
                area.source = SOURCE_TABLE
                changed = True
        if changed:
            self._schedule_notify()

    def table_failed(self, error: str) -> None:
        with self.lock:
            self.last_table_error = error

    def invalidate(self) -> None:
        """Forget arm states: events may have been missed while detached."""
        changed = False
        with self.lock:
            for area in self.areas.values():
                if area.raw_mode is not None:
                    area.raw_mode = None
                    area.source = None
                    changed = True
            self.invalidations += 1
        if changed:
            self._schedule_notify()

    # -- events ------------------------------------------------------------

    def apply_event(self, event: dict[str, Any]) -> None:
        code = str(event.get("Code") or "")
        if code not in ARM_EVENT_CODES:
            return
        data = event.get("Data") or {}
        if not isinstance(data, dict):
            data = {}
        raw_mode = str(data.get("Mode") or "") or None
        trigger_mode = str(data.get("TriggerMode") or "") or None
        now = timestamp()

        with self.lock:
            self.events_received += 1
            self._note_mode(raw_mode)

            if code == GLOBAL_ARM_EVENT:
                self.last_global_change = {
                    "at": now,
                    "raw_mode": raw_mode,
                    "state": arm_mode_state(raw_mode),
                    "profile": data.get("Profile"),
                    "trigger_mode": trigger_mode,
                }
                return
            if code == GLOBAL_ARM_FAILURE_EVENT:
                self.last_failure = ArmFailure(
                    at=now,
                    raw_mode=raw_mode,
                    trigger_mode=trigger_mode,
                    open_zones=parse_abnormal_zones(data),
                )
            else:
                area = self.areas.get(safe_int(event.get("Index"), -1))
                if area is None:
                    self.unknown_area_events += 1
                    return
                if code == AREA_ARM_FAILURE_EVENT:
                    area.last_failure = ArmFailure(
                        at=now,
                        raw_mode=raw_mode,
                        trigger_mode=trigger_mode,
                        open_zones=parse_abnormal_zones(data),
                    )
                    # A single-area arm has no global failure to summarise
                    # it. ParentEvent is unreliable here: the first area of
                    # a global burst omits it, so use IsGlobal instead.
                    if not data.get("IsGlobal"):
                        self.last_failure = area.last_failure
                else:
                    self.event_sequence += 1
                    area.last_event_seq = self.event_sequence
                    area.raw_mode = raw_mode
                    area.source = SOURCE_EVENT
                    area.profile = data.get("Profile")
                    area.trigger_mode = trigger_mode
                    area.changed_at = now
                    area.bypassed_zones = parse_abnormal_zones(data)
        self._schedule_notify()

    # -- notification ------------------------------------------------------

    def _schedule_notify(self) -> None:
        if self.quiet_seconds <= 0:
            self._fire()
            return
        now = time.monotonic()
        with self.lock:
            if self._pending_since is None:
                self._pending_since = now
            if self._timer is not None:
                self._timer.cancel()
            remaining = self._pending_since + self.max_delay_seconds - now
            timer = threading.Timer(
                max(0.0, min(self.quiet_seconds, remaining)), self._fire
            )
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self.lock:
            self._timer = None
            self._pending_since = None
        if self.change_callback is not None:
            try:
                self.change_callback()
            except Exception:
                _LOGGER.exception("ARC arm-state callback failed")

    def stop(self) -> None:
        with self.lock:
            timer, self._timer = self._timer, None
            self._pending_since = None
        if timer is not None:
            timer.cancel()

    # -- diagnostics -------------------------------------------------------

    @staticmethod
    def _failure_dict(failure: ArmFailure | None) -> dict[str, Any] | None:
        if failure is None:
            return None
        return {
            "at": failure.at,
            "raw_mode": failure.raw_mode,
            "state": failure.state,
            "trigger_mode": failure.trigger_mode,
            "open_zones": list(failure.open_zones),
        }

    def diagnostics(self) -> dict[str, Any]:
        with self.lock:
            return {
                "system_state": self.system_state(),
                "events_received": self.events_received,
                "unknown_area_events": self.unknown_area_events,
                "unknown_modes": sorted(self.unknown_modes),
                "invalidations": self.invalidations,
                "table_reads": self.table_reads,
                "table_corrections": self.table_corrections,
                "table_stale_rejects": self.table_stale_rejects,
                "last_table_read": self.last_table_read,
                "last_table_error": self.last_table_error,
                "last_global_change": self.last_global_change,
                "last_failure": self._failure_dict(self.last_failure),
                "areas": {
                    area.area_id: {
                        "name": area.name,
                        "state": area.state,
                        "raw_mode": area.raw_mode,
                        "source": area.source,
                        "profile": area.profile,
                        "trigger_mode": area.trigger_mode,
                        "changed_at": area.changed_at,
                        "bypassed_zones": list(area.bypassed_zones),
                        "last_failure": self._failure_dict(area.last_failure),
                    }
                    for area in self.areas.values()
                },
            }
