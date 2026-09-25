"""Thread-safe zone state engine and snapshot reconciler."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from .models import Zone
from .snapshot import SnapshotClient
from .util import raw_to_active, safe_int, timestamp

_LOGGER = logging.getLogger(__name__)

EVENT_CODE = "AlarmInputSourceSignal"
PIRCAM_EVENT_CODE = "AlarmLocal"
PIRCAM_MOTION_HOLD_SECONDS = 5.0


class StateEngine:
    def __init__(
        self,
        zones: dict[int, Zone],
        change_callback: Callable[[set[int]], None] | None = None,
    ):
        self.zones = zones
        self.change_callback = change_callback
        self.lock = threading.RLock()
        self.event_queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
        self.stop_event = threading.Event()
        self.current_generation = 0
        self.processor_thread: threading.Thread | None = None
        self.last_snapshot_time: str | None = None
        self.last_snapshot_reason: str | None = None
        self.snapshot_count = 0
        self.event_sequence = 0
        self.realtime_events_received = 0
        self.realtime_state_changes = 0
        self.duplicate_events = 0
        self.stale_generation_events = 0
        self.snapshot_corrections = 0
        self.reconnect_corrections = 0
        self.periodic_corrections = 0
        self.stale_snapshot_rejects = 0
        self.wpan_motion_updates = 0
        self.wpan_motion_detections = 0
        # ARD1731 PIRCam emits AlarmLocal Start/Stop almost back-to-back.
        # Hold the HA motion state briefly so the pulse is observable and
        # automations cannot lose it due to event-loop coalescing.
        self._pircam_off_timers: dict[int, threading.Timer] = {}

    def begin_generation(self, generation: int) -> None:
        with self.lock:
            self.current_generation = generation

    def enqueue_event(self, generation: int, event: dict[str, Any]) -> None:
        self.event_queue.put((generation, event))

    def snapshot_watermark(self) -> int:
        with self.lock:
            return self.event_sequence

    def apply_snapshot(
        self,
        snapshot: list[dict[str, Any]],
        source: str,
        source_kind: str,
        event_watermark: int,
    ):
        changed: list[tuple[Zone, bool | None, bool | None]] = []
        skipped: list[Zone] = []
        changed_indices: set[int] = set()
        by_pos = {item["array_pos"]: item for item in snapshot}
        with self.lock:
            for idx, zone in self.zones.items():
                item = by_pos.get(idx)
                if item is None:
                    continue
                zone.snapshot_index = item["nIndex"]
                health = (item["online"], item.get("tamper"), item.get("low_power"))
                if health != (zone.online_state, zone.tamper, zone.low_power):
                    # Online/tamper/battery entities must redraw too.
                    changed_indices.add(idx)
                zone.online_state, zone.tamper, zone.low_power = health
                if zone.last_event_seq > event_watermark:
                    self.stale_snapshot_rejects += 1
                    skipped.append(zone)
                    continue
                old_active = zone.active
                new_active = raw_to_active(item["alarm_state"])
                zone.raw_alarm_state = item["alarm_state"]
                zone.active = new_active
                zone.last_source = source
                if old_active != new_active:
                    if old_active is not None:
                        changed.append((zone, old_active, new_active))
                    zone.last_changed = timestamp()
                    changed_indices.add(idx)
            self.last_snapshot_time = timestamp()
            self.last_snapshot_reason = source
            self.snapshot_count += 1
            corrections = len(changed)
            self.snapshot_corrections += corrections
            if source_kind == "reconnect":
                self.reconnect_corrections += corrections
            elif source_kind == "periodic":
                self.periodic_corrections += corrections
        if changed_indices and self.change_callback:
            self.change_callback(changed_indices)
        return changed, skipped

    def validate_index_mapping(
        self, snapshot: list[dict[str, Any]]
    ) -> tuple[int, list[tuple]]:
        by_pos = {item["array_pos"]: item for item in snapshot}
        mismatches, checked = [], 0
        for idx in sorted(self.zones):
            item = by_pos.get(idx)
            if item is None:
                mismatches.append((idx, None, "snapshot position missing"))
                continue
            checked += 1
            if item["nIndex"] != idx + 1:
                mismatches.append((idx, item["nIndex"], idx + 1))
        return checked, mismatches

    def start(self) -> None:
        self.processor_thread = threading.Thread(
            target=self._process_events, name="dahua-arc-events", daemon=True
        )
        self.processor_thread.start()

    def _process_events(self) -> None:
        while not self.stop_event.is_set():
            try:
                generation, event = self.event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.lock:
                if generation != self.current_generation:
                    self.stale_generation_events += 1
                    continue
            try:
                self._apply_event(event)
            except Exception:
                _LOGGER.exception("Failed processing ARC event")

    def _apply_event(self, event: dict[str, Any]) -> None:
        code = str(event.get("Code") or "")
        if code not in (EVENT_CODE, PIRCAM_EVENT_CODE):
            return

        idx, action = safe_int(event.get("Index")), event.get("Action")
        if idx is None or action not in ("Start", "Stop"):
            return

        data = event.get("Data") or {}
        if not isinstance(data, dict):
            data = {}

        # ARD1731-W2 reports actual armed intrusion/motion as AlarmLocal,
        # not AlarmInputSourceSignal. Do not generalize AlarmLocal to every
        # ARC event type until those devices are separately validated.
        if (
            code == PIRCAM_EVENT_CODE
            and str(data.get("DevType") or data.get("SenseMethod") or "") != "PIRCam"
        ):
            return

        changed_indices: set[int] = set()

        # PIRCam Stop usually follows Start within the same second. If we
        # immediately apply both, Home Assistant can observe only the final
        # Clear state. Record the Stop event now but defer the actual OFF
        # transition for a short, deterministic motion pulse.
        if code == PIRCAM_EVENT_CODE and action == "Stop":
            with self.lock:
                self.realtime_events_received += 1
                zone = self.zones.get(idx)
                if zone is None:
                    return
                self.event_sequence += 1
                zone.last_event_seq = self.event_sequence
                zone.last_source = "DHIP AlarmLocal realtime"
                zone.last_action = action
                if data.get("SN"):
                    zone.parent_sn = str(data["SN"])

                old_timer = self._pircam_off_timers.pop(idx, None)
                if old_timer is not None:
                    old_timer.cancel()

                timer = threading.Timer(
                    PIRCAM_MOTION_HOLD_SECONDS,
                    self._finish_pircam_motion,
                    args=(idx, self.event_sequence),
                )
                timer.daemon = True
                self._pircam_off_timers[idx] = timer
                timer.start()
            return

        new_active = action == "Start"
        with self.lock:
            self.realtime_events_received += 1
            zone = self.zones.get(idx)
            if zone is None:
                return

            if code == PIRCAM_EVENT_CODE:
                old_timer = self._pircam_off_timers.pop(idx, None)
                if old_timer is not None:
                    old_timer.cancel()

            old_active = zone.active
            self.event_sequence += 1
            zone.last_event_seq = self.event_sequence
            zone.active = new_active
            zone.raw_alarm_state = 1 if new_active else 5
            zone.last_source = (
                "DHIP AlarmLocal realtime"
                if code == PIRCAM_EVENT_CODE
                else "DHIP realtime"
            )
            zone.last_action = action
            zone.last_changed = timestamp()
            if data.get("SN"):
                zone.parent_sn = str(data["SN"])
            exchannel = safe_int(data.get("ExChannel"))
            if exchannel is not None:
                zone.event_exchannel = exchannel
            if old_active != new_active:
                self.realtime_state_changes += 1
                changed_indices.add(idx)
            else:
                self.duplicate_events += 1

        if changed_indices and self.change_callback:
            self.change_callback(changed_indices)

    def apply_wpan_motion(self, idx: int, active: bool, source: str) -> None:
        """Apply a validated LowRateWPAN PIR-camera alarm-state transition.

        Research builds use this only when getAccessoryStatus reports a
        non-zero AlarmState for a configured PIRCam. FindMe is deliberately
        ignored because live diagnostics show it follows the radio heartbeat,
        not detector motion.
        """
        changed = False
        with self.lock:
            zone = self.zones.get(idx)
            if zone is None or zone.sense_method != "PIRCam":
                return
            old_active = zone.active
            self.event_sequence += 1
            zone.last_event_seq = self.event_sequence
            zone.active = bool(active)
            zone.raw_alarm_state = 1 if active else 5
            zone.last_source = source
            zone.last_action = "WPAN-Start" if active else "WPAN-Stop"
            if old_active != zone.active:
                zone.last_changed = timestamp()
                self.wpan_motion_updates += 1
                if active:
                    self.wpan_motion_detections += 1
                changed = True
        if changed and self.change_callback:
            self.change_callback({idx})

    def _finish_pircam_motion(self, idx: int, stop_event_seq: int) -> None:
        """Finish a short ARD1731 motion pulse after its immediate Stop."""
        changed = False
        with self.lock:
            timer = self._pircam_off_timers.get(idx)
            if timer is None:
                return
            self._pircam_off_timers.pop(idx, None)

            zone = self.zones.get(idx)
            if zone is None:
                return

            # A newer event supersedes this delayed Stop.
            if zone.last_event_seq != stop_event_seq or zone.last_action != "Stop":
                return

            if zone.active is not False:
                zone.active = False
                zone.raw_alarm_state = 5
                zone.last_source = (
                    f"DHIP AlarmLocal realtime "
                    f"({PIRCAM_MOTION_HOLD_SECONDS:g}s motion hold)"
                )
                zone.last_changed = timestamp()
                self.realtime_state_changes += 1
                changed = True

        if changed and self.change_callback:
            self.change_callback({idx})

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            return {
                "current_generation": self.current_generation,
                "queued_events": self.event_queue.qsize(),
                "snapshot_count": self.snapshot_count,
                "last_snapshot_time": self.last_snapshot_time,
                "last_snapshot_reason": self.last_snapshot_reason,
                "event_sequence": self.event_sequence,
                "realtime_events_received": self.realtime_events_received,
                "realtime_state_changes": self.realtime_state_changes,
                "duplicate_events": self.duplicate_events,
                "stale_generation_events": self.stale_generation_events,
                "snapshot_corrections": self.snapshot_corrections,
                "reconnect_corrections": self.reconnect_corrections,
                "periodic_corrections": self.periodic_corrections,
                "stale_snapshot_rejects": self.stale_snapshot_rejects,
                "wpan_motion_updates": self.wpan_motion_updates,
                "wpan_motion_detections": self.wpan_motion_detections,
            }

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            timers = list(self._pircam_off_timers.values())
            self._pircam_off_timers.clear()
        for timer in timers:
            timer.cancel()
        if self.processor_thread is not None:
            self.processor_thread.join(timeout=2)


class Reconciler:
    def __init__(self, snapshot_client: SnapshotClient, engine: StateEngine):
        self.snapshot_client, self.engine = snapshot_client, engine
        self.lock = threading.RLock()

    def run(
        self, reason: str, source_kind: str
    ) -> list[tuple[Zone, bool | None, bool | None]]:
        with self.lock:
            watermark = self.engine.snapshot_watermark()
            snapshot = self.snapshot_client.snapshot()
            checked, mismatches = self.engine.validate_index_mapping(snapshot)
            if mismatches:
                raise RuntimeError(
                    f"ARC snapshot index mapping failed: {mismatches[:5]}"
                )
            if checked != len(self.engine.zones):
                raise RuntimeError(
                    f"Snapshot mapped {checked}/{len(self.engine.zones)} configured zones"
                )
            changed, skipped = self.engine.apply_snapshot(
                snapshot, reason, source_kind, watermark
            )
        if skipped:
            _LOGGER.debug("Rejected %d stale snapshot values", len(skipped))
        if changed:
            _LOGGER.info("%s corrected %d ARC zone state(s)", source_kind, len(changed))
        return changed
