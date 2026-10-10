"""Read-only area arm and alarm state from ARC realtime events.

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

An alarm is ``AlarmLocal`` Start: ``Index`` is the zone's ``Alarm[]`` index
and ``Data.Areas`` the zero-based area indexes it alarmed. Disarming the area
ends it, followed by ``AlarmClear`` Confirm whose ``Index`` is the area. No
config table reports an alarm in progress, so alarm state is known only from
these events: it is off once the area's arm state is known and no alarm has
arrived since.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

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
ALARM_EVENT = "AlarmLocal"
ALARM_CLEAR_EVENT = "AlarmClear"
ALARM_CLEAR_AREA = "AlarmArea"
TRACKED_EVENT_CODES = ARM_EVENT_CODES | {ALARM_EVENT, ALARM_CLEAR_EVENT}

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


WAIT_POLL_SECONDS = 0.25


class OutcomeMark(NamedTuple):
    """Counters taken before a command is sent.

    Only events and table reads that arrive after the mark can confirm or
    refuse that command, so earlier history never decides a new one.
    """

    event_seq: int
    failure_seq: int
    table_seq: int


class WaitResult(NamedTuple):
    """Result of :meth:`ArmingTracker.wait_for_outcome`."""

    outcome: Literal["confirmed", "refused", "timeout", "stopped"]
    confirmed_by: Literal["event", "table"] | None = None
    open_zones: tuple[dict[str, Any], ...] = ()


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
    last_failure_seq: int = 0
    last_table_seq: int = 0
    profile: str | None = None
    trigger_mode: str | None = None
    changed_at: str | None = None
    bypassed_zones: list[dict[str, Any]] = field(default_factory=list)
    last_failure: ArmFailure | None = None
    # None while unknown. The zones and times describe the current alarm, or
    # the last one once it has ended.
    alarm: bool | None = None
    alarm_started: str | None = None
    alarm_ended: str | None = None
    alarm_zones: list[dict[str, Any]] = field(default_factory=list)
    # An alarm that was on when the realtime stream detached. The next table
    # read decides whether it survived: a disarm ends it.
    alarm_unconfirmed: bool = False

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
        # Signalled on every arm event, failure, table apply and invalidate so
        # a command waiting for its outcome wakes at once. The HA notification
        # debounce below is separate and never delays it.
        self._changed = threading.Condition(self.lock)
        self.last_failure: ArmFailure | None = None
        self.last_global_change: dict[str, Any] | None = None
        self.last_alarm: dict[str, Any] | None = None
        self.alarm_events_received = 0
        self.unknown_clear_types: set[str] = set()
        self.events_received = 0
        self.unknown_area_events = 0
        self.unknown_modes: set[str] = set()
        self.invalidations = 0
        self.event_sequence = 0
        self.failure_sequence = 0
        self.table_sequence = 0
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

    def alarm_state(self) -> bool | None:
        """On while any area is in alarm; unknown while any area is unknown."""
        with self.lock:
            alarms = {area.alarm for area in self.areas.values()}
        if True in alarms:
            return True
        if not alarms or None in alarms:
            return None
        return False

    def alarm_areas(self) -> list[ArmArea]:
        with self.lock:
            return [area for area in self.areas.values() if area.alarm]

    def watermark(self) -> int:
        """Event sequence to pass to :meth:`apply_table` for a read begun now."""
        with self.lock:
            return self.event_sequence

    def outcome_mark(self) -> OutcomeMark:
        """Counters to pass to :meth:`wait_for_outcome` for a command sent now."""
        with self.lock:
            return OutcomeMark(
                self.event_sequence, self.failure_sequence, self.table_sequence
            )

    def _evaluate(
        self, mark: OutcomeMark, areas: Iterable[int], raw_mode: str
    ) -> WaitResult | None:
        """Decide a command from state newer than ``mark``; ``None`` if open."""
        targets = [self.areas[index] for index in areas if index in self.areas]
        if not targets:
            return None
        sources: set[str] = set()
        for area in targets:
            if area.raw_mode != raw_mode:
                break
            if area.last_event_seq > mark.event_seq:
                sources.add("event")
            elif area.source == SOURCE_TABLE and area.last_table_seq > mark.table_seq:
                sources.add("table")
            else:
                break
        else:
            return WaitResult("confirmed", "event" if sources == {"event"} else "table")
        refused = [a for a in targets if a.last_failure_seq > mark.failure_seq]
        if refused:
            zones = tuple(
                zone
                for area in refused
                if area.last_failure is not None
                for zone in area.last_failure.open_zones
            )
            return WaitResult("refused", None, zones)
        return None

    def wait_for_outcome(
        self,
        mark: OutcomeMark,
        areas: Iterable[int],
        raw_mode: str,
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> WaitResult:
        """Block until every target area reaches ``raw_mode`` or one refuses.

        Confirmed by an ``AreaArmModeChange`` newer than the mark, or by a
        table read newer than the mark. A reconnect (``invalidate``) does not
        end the wait: the table read that follows it resolves it.
        """
        targets = tuple(areas)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._changed:
            while True:
                result = self._evaluate(mark, targets, raw_mode)
                if result is not None:
                    return result
                if stop_event is not None and stop_event.is_set():
                    return WaitResult("stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return WaitResult("timeout")
                self._changed.wait(min(remaining, WAIT_POLL_SECONDS))

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
            self.table_sequence += 1
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
                changed |= self._settle_alarm(area, raw_mode)
                area.last_table_seq = self.table_sequence
                if area.raw_mode == raw_mode:
                    continue
                if area.raw_mode is not None:
                    # A known state that was wrong: an event was missed.
                    self.table_corrections += 1
                area.raw_mode = raw_mode
                area.source = SOURCE_TABLE
                changed = True
            self._changed.notify_all()
        if changed:
            self._schedule_notify()

    @staticmethod
    def _settle_alarm(area: ArmArea, raw_mode: str) -> bool:
        """Resolve an unknown alarm state once the area's arm state is known."""
        if area.alarm is None:
            area.alarm = False
            return True
        if area.alarm_unconfirmed:
            area.alarm_unconfirmed = False
            if arm_mode_state(raw_mode) == DISARMED:
                # Disarmed while detached: the alarm ended unseen.
                area.alarm = False
                area.alarm_ended = timestamp()
                return True
        return False

    def table_failed(self, error: str) -> None:
        with self.lock:
            self.last_table_error = error

    def invalidate(self) -> None:
        """Forget arm states: events may have been missed while detached.

        An alarm in progress stays on, unconfirmed: the ARC holds an alarm
        until the area is disarmed, which the next table read shows.
        """
        changed = False
        with self.lock:
            for area in self.areas.values():
                if area.raw_mode is not None:
                    area.raw_mode = None
                    area.source = None
                    changed = True
                if area.alarm:
                    area.alarm_unconfirmed = True
                elif area.alarm is not None:
                    area.alarm = None
                    changed = True
            self.invalidations += 1
            self._changed.notify_all()
        if changed:
            self._schedule_notify()

    # -- events ------------------------------------------------------------

    def apply_event(self, event: dict[str, Any]) -> None:
        try:
            self._apply_event(event)
        finally:
            # Wake waiting commands even when a handler returned early.
            with self.lock:
                self._changed.notify_all()

    def _apply_event(self, event: dict[str, Any]) -> None:
        code = str(event.get("Code") or "")
        if code not in TRACKED_EVENT_CODES:
            return
        data = event.get("Data") or {}
        if not isinstance(data, dict):
            data = {}
        if code == ALARM_EVENT:
            self._apply_alarm(event, data)
            return
        if code == ALARM_CLEAR_EVENT:
            self._apply_alarm_clear(event, data)
            return
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
                    self.failure_sequence += 1
                    area.last_failure_seq = self.failure_sequence
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
                    if arm_mode_state(raw_mode) == DISARMED:
                        # Disarming ends an alarm; AlarmClear follows.
                        self._end_alarm(area, now)
                    else:
                        self._settle_alarm(area, raw_mode)
        self._schedule_notify()

    @staticmethod
    def _end_alarm(area: ArmArea, now: str) -> None:
        if area.alarm:
            area.alarm_ended = now
        area.alarm = False
        area.alarm_unconfirmed = False

    def _apply_alarm(self, event: dict[str, Any], data: dict[str, Any]) -> None:
        if event.get("Action") != "Start":
            # Stop follows the input, not the alarm, which lasts until disarm.
            with self.lock:
                self.alarm_events_received += 1
            return
        indexes = data.get("Areas")
        if not isinstance(indexes, list):
            indexes = [
                info.get("Index")
                for info in data.get("AreaInfo") or []
                if isinstance(info, dict)
            ]
        now = timestamp()
        zone = {
            "zone_index": safe_int(event.get("Index")),
            "zone": str(data.get("Name") or "").strip() or None,
            "alarm_type": data.get("AlarmType"),
            "at": now,
        }
        with self.lock:
            self.alarm_events_received += 1
            areas = []
            for index in indexes:
                area = self.areas.get(safe_int(index, -1))
                if area is None:
                    self.unknown_area_events += 1
                    continue
                if not area.alarm:
                    area.alarm = True
                    area.alarm_started = now
                    area.alarm_ended = None
                    area.alarm_zones = []
                area.alarm_unconfirmed = False
                if all(
                    seen["zone_index"] != zone["zone_index"]
                    for seen in area.alarm_zones
                ):
                    area.alarm_zones.append({"area": area.name, **zone})
                areas.append(area.name)
            self.last_alarm = {**zone, "areas": areas}
            if not areas:
                return
        self._schedule_notify()

    def _apply_alarm_clear(self, event: dict[str, Any], data: dict[str, Any]) -> None:
        clear_type = str(data.get("Type") or "")
        with self.lock:
            self.alarm_events_received += 1
            if clear_type != ALARM_CLEAR_AREA:
                # Only area clears are verified; Index may mean something else.
                self.unknown_clear_types.add(clear_type)
                return
            area = self.areas.get(safe_int(event.get("Index"), -1))
            if area is None:
                self.unknown_area_events += 1
                return
            self._end_alarm(area, timestamp())
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
                "alarm_state": self.alarm_state(),
                "alarm_events_received": self.alarm_events_received,
                "unknown_clear_types": sorted(self.unknown_clear_types),
                "last_alarm": self.last_alarm,
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
                        "alarm": area.alarm,
                        "alarm_unconfirmed": area.alarm_unconfirmed,
                        "alarm_started": area.alarm_started,
                        "alarm_ended": area.alarm_ended,
                        "alarm_zones": list(area.alarm_zones),
                    }
                    for area in self.areas.values()
                },
            }
