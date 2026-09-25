"""ARC3800H local state hub.

CGI discovers the configured Alarm/MultiIO topology; one DHIP connection
provides authoritative AlarmRegion.getChannelsState snapshots and a second
DHIP connection streams AlarmInputSourceSignal events. Research-only features
live in :mod:`.research` and are constructed only when explicitly enabled.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .protocol.cgi import (
    discover_alarm_points,
    discover_multiio_parents,
    fetch_alarm_config,
)
from .protocol.engine import Reconciler, StateEngine
from .protocol.inventory import (
    PRIMARY_SENSOR_CLASSES,
    REDACTED,
    EventCatalog,
    InventoryRpcClient,
    RadioDeviceInfo,
    classify_alarm_record,
    extract_radio_devices,
    extract_zone_area_hints,
    record_is_physical,
    redact_sensitive,
    summarize_alarm_records,
    summarize_rpc_inventory,
)
from .protocol.models import Zone
from .protocol.realtime import RealtimeClient
from .protocol.snapshot import SnapshotClient
from .protocol.util import rpc_response_value, timestamp
from .research.detector_test import DetectorTestController
from .research.pircam import PirCamMedia
from .research.wpan import WPANResearchPoller
from .vendor.dahua.exceptions import LoginError

_LOGGER = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 30


class ArcHub:
    """Own the two DHIP connections and the state engine."""

    def __init__(
        self,
        host: str,
        http_port: int,
        dhip_port: int,
        username: str,
        password: str,
        periodic_resync_seconds: int = 300,
        enable_research_features: bool = False,
    ):
        self.host, self.http_port, self.dhip_port = host, http_port, dhip_port
        self.username, self.password = username, password
        self.periodic_resync_seconds = periodic_resync_seconds
        self.enable_research_features = enable_research_features
        self.zones: dict[int, Zone] = {}
        self.parents: dict[int, dict[str, Any]] = {}
        self.alarm_records: dict[int, dict[str, str]] = {}
        self.initial_alarm_snapshot: list[dict[str, Any]] = []
        self.rpc_inventory: dict[str, Any] = {}
        self.research_refresh_inventory: dict[str, Any] = {}
        self.inventory_error: str | None = None
        self.area_hints: dict[int, str] = {}
        self.radio_devices: dict[int, RadioDeviceInfo] = {}
        self.event_catalog = EventCatalog()
        self._listeners: set[Callable[[set[int] | None], None]] = set()
        self.snapshot_client: SnapshotClient | None = None
        self.engine: StateEngine | None = None
        self.reconciler: Reconciler | None = None
        self.realtime: RealtimeClient | None = None
        self._periodic_stop = threading.Event()
        self._periodic_thread: threading.Thread | None = None
        self._static_summary: dict[str, Any] | None = None

        # Set by the Home Assistant adapter during setup.
        self.area_decisions: dict[str, dict[str, Any]] = {}
        self.child_device_ids: dict[int, str] = {}
        self.multiio_parent_ids: dict[int, str] = {}
        # Called (from a worker thread) once when the ARC rejects credentials.
        self.on_auth_failed: Callable[[], None] | None = None
        self.auth_failed = False
        self._auth_failed_lock = threading.Lock()

        # Research-only helpers; None unless enable_research_features is set.
        self.wpan_research: WPANResearchPoller | None = None
        self.pircam: PirCamMedia | None = None
        self.detector_test: DetectorTestController | None = None

    # -- listeners ---------------------------------------------------------

    def add_listener(
        self, callback: Callable[[set[int] | None], None]
    ) -> Callable[[], None]:
        """Register a change listener.

        Callbacks run on worker threads and receive the changed Alarm[]
        indexes, or ``None`` for hub-wide health/availability changes.
        """
        self._listeners.add(callback)

        def remove() -> None:
            self._listeners.discard(callback)

        return remove

    def _notify(self, indices: set[int] | None = None) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(indices)
            except Exception:
                _LOGGER.exception("ARC listener failed")

    def _handle_auth_failure(self, exc: LoginError | None = None) -> None:
        """Stop every background login and ask HA to start reauthentication."""
        with self._auth_failed_lock:
            if self.auth_failed:
                return
            self.auth_failed = True
        # Repeated failed logins can lock the ARC account.
        self._periodic_stop.set()
        if self.snapshot_client is not None:
            self.snapshot_client.auth_failed = True
        if self.wpan_research is not None:
            # The research poller re-logs in every few seconds; stop it now.
            # hub.stop() still joins its threads on unload.
            self.wpan_research.stop_event.set()
        _LOGGER.error("Dahua ARC rejected the configured credentials: %s", exc)
        self._notify(None)
        if self.on_auth_failed is not None:
            try:
                self.on_auth_failed()
            except Exception:
                _LOGGER.exception("ARC reauth trigger failed")

    # -- research delegation -----------------------------------------------

    def pircam_snapshot(self, index: int) -> dict[str, Any] | None:
        return self.pircam.snapshot(index) if self.pircam else None

    def fetch_pircam_image(self, index: int) -> bytes | None:
        return self.pircam.fetch_image(index) if self.pircam else None

    def start_detector_test(self, index: int) -> dict[str, Any]:
        if self.detector_test is None:
            raise RuntimeError("Research features are disabled")
        return self.detector_test.start(index)

    def stop_detector_test(self, index: int) -> dict[str, Any]:
        if self.detector_test is None:
            raise RuntimeError("Research features are disabled")
        return self.detector_test.stop(index)

    def detector_test_status(self, index: int) -> dict[str, Any]:
        return self.detector_test.status(index) if self.detector_test else {}

    # -- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        # A cached snapshot socket can remain connected after event delivery has
        # stopped. Expose zone state only while the realtime path is healthy;
        # its reconnect path takes a fresh authoritative snapshot first.
        return bool(self.realtime and self.realtime.connected)

    def _system_rpc_value(self, method: str) -> Any:
        return rpc_response_value((self.rpc_inventory.get("system") or {}).get(method))

    @property
    def device_type(self) -> str:
        value = self._system_rpc_value("magicBox.getDeviceType")
        return str(value) if value else "Dahua ARC alarm hub"

    @property
    def software_version(self) -> str | None:
        value = self._system_rpc_value("magicBox.getSoftwareVersion")
        return str(value) if value else None

    @property
    def serial_number(self) -> str | None:
        value = self._system_rpc_value("magicBox.getSerialNo")
        return str(value) if value else None

    @property
    def configuration_url(self) -> str:
        port = "" if self.http_port == 80 else f":{self.http_port}"
        return f"http://{self.host}{port}"

    @property
    def primary_zones(self) -> dict[int, Zone]:
        """Physical inputs that have meaningful HA binary-sensor semantics."""
        return {
            idx: zone
            for idx, zone in self.zones.items()
            if zone.classification in PRIMARY_SENSOR_CLASSES
        }

    def radio_device_for_zone(self, zone: Zone) -> RadioDeviceInfo | None:
        if zone.level1 is None or zone.level1 <= 0:
            return None
        return self.radio_devices.get(zone.level1)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # CGI Alarm[] is the authoritative topology source for the AlarmIn path.
        self.alarm_records = fetch_alarm_config(
            self.host, self.http_port, self.username, self.password
        )
        self.parents = discover_multiio_parents(self.alarm_records)

        # Acquire one authoritative snapshot before constructing entities.
        # This lets us expose non-MultiIO Alarm[] records only when the ARC
        # itself confirms that the same index is a real AlarmIn state.
        self.snapshot_client = SnapshotClient(
            self.host, self.dhip_port, self.username, self.password
        )
        self.snapshot_client.connect()
        self.initial_alarm_snapshot = self.snapshot_client.snapshot()
        self.zones = discover_alarm_points(
            self.alarm_records, self.initial_alarm_snapshot, self.parents
        )

        # Read-only inventory discovery is isolated from the production state
        # engine. Unsupported methods/config tables are recorded as discovery
        # failures and never prevent normal operation.
        inventory_client = InventoryRpcClient(
            self.host, self.dhip_port, self.username, self.password
        )
        try:
            self.rpc_inventory = inventory_client.collect(
                include_method_catalog=self.enable_research_features
            )
            self.inventory_error = None
        except Exception as exc:
            self.inventory_error = f"{type(exc).__name__}: {exc}"
            self.rpc_inventory = {"error": self.inventory_error}
            _LOGGER.warning("ARC extended inventory discovery failed: %s", exc)
        finally:
            inventory_client.close()

        # Correlate authoritative Alarm[] rows with Dahua subsystem areas and
        # the AirFly radio-device map (models, serials, repeater parents).
        self.area_hints = extract_zone_area_hints(self.rpc_inventory)
        for idx, zone in self.zones.items():
            zone.area_hint = self.area_hints.get(idx, "")
        self.radio_devices = extract_radio_devices(
            self.rpc_inventory, self.alarm_records, self.area_hints
        )
        self._static_summary = self._build_static_summary()

        if self.enable_research_features:
            self.pircam = PirCamMedia(
                self.host,
                self.http_port,
                self.username,
                self.password,
                self.zones,
                self.snapshot_client,
                self._notify,
            )
            self.detector_test = DetectorTestController(
                self.host,
                self.dhip_port,
                self.username,
                self.password,
                self.zones,
                self._notify,
            )

        self.engine = StateEngine(self.zones, self._notify)
        self.engine.start()
        self.reconciler = Reconciler(self.snapshot_client, self.engine)
        self.realtime = RealtimeClient(
            self.host,
            self.dhip_port,
            self.username,
            self.password,
            self.engine,
            self.reconciler,
            self.event_catalog,
            health_callback=lambda: self._notify(None),
            event_callback=self.pircam.process_event if self.pircam else None,
            auth_failed_callback=self._handle_auth_failure,
        )
        self.realtime.start()
        if not self.realtime.ready_event.wait(READY_TIMEOUT_SECONDS):
            raise TimeoutError(
                "ARC realtime DHIP attach/resync did not complete within "
                f"{READY_TIMEOUT_SECONDS} seconds"
            )
        if self.realtime.last_login_error is not None:
            raise self.realtime.last_login_error
        if self.realtime.last_error and not self.realtime.connected:
            raise RuntimeError(self.realtime.last_error)
        self._periodic_thread = threading.Thread(
            target=self._periodic_loop,
            name="dahua-arc-periodic",
            daemon=True,
        )
        self._periodic_thread.start()

        if self.enable_research_features:
            # Research-only read path. Performs only LowRateWPAN get*/attach
            # calls and never modifies accessory state.
            self.wpan_research = WPANResearchPoller(
                self.host,
                self.dhip_port,
                self.username,
                self.password,
                self.zones,
                self.radio_devices,
                self.engine,
            )
            self.wpan_research.start()

        _LOGGER.info(
            "Dahua ARC discovery: %d physical AlarmIn records, %d primary sensors, "
            "%d paired radio devices (%d MultiIO inputs)",
            len(self.zones),
            len(self.primary_zones),
            len(self.radio_devices),
            sum(1 for zone in self.primary_zones.values() if zone.is_multiio),
        )

    def refresh_research_inventory(self) -> None:
        """Refresh dynamic read-only research tables for diagnostics download.

        Service/method introspection is collected once at startup. This refresh
        skips the expensive listMethod pass and re-reads current config and
        event-state tables. It does not change entities or runtime state.
        """
        if self.auth_failed:
            return
        client = InventoryRpcClient(
            self.host, self.dhip_port, self.username, self.password
        )
        try:
            client.connect()
            self.research_refresh_inventory = client.collect(
                include_method_catalog=False, include_research_tables=True
            )
        except LoginError as exc:
            self.research_refresh_inventory = {
                "collected_at": timestamp(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._handle_auth_failure(exc)
        except Exception as exc:
            self.research_refresh_inventory = {
                "collected_at": timestamp(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _LOGGER.warning("ARC research inventory refresh failed: %s", exc)
        finally:
            client.close()

    def _periodic_loop(self) -> None:
        while not self._periodic_stop.wait(self.periodic_resync_seconds):
            try:
                if self.reconciler:
                    self.reconciler.run("periodic pure-DHIP sanity resync", "periodic")
            except LoginError as exc:
                self._handle_auth_failure(exc)
                return
            except Exception as exc:
                _LOGGER.warning("ARC periodic snapshot failed: %s", exc)
            # Keep diagnostic health entities current.
            self._notify(None)

    # -- diagnostics -------------------------------------------------------

    def _build_static_summary(self) -> dict[str, Any]:
        """Summary values that are fixed once discovery has completed."""
        primary = self.primary_zones
        return {
            **summarize_alarm_records(self.alarm_records),
            "tracked_physical_alarm_records": len(self.zones),
            "exposed_alarm_inputs": len(primary),
            "paired_radio_devices": len(self.radio_devices),
            "multiio_parents": len(self.parents),
            "multiio_inputs": sum(1 for z in primary.values() if z.is_multiio),
            "wireless_primary_sensors": sum(
                1 for z in primary.values() if not z.is_multiio
            ),
            "non_multiio_alarm_inputs": sum(
                1 for z in primary.values() if not z.is_multiio
            ),
            **summarize_rpc_inventory(self.rpc_inventory),
        }

    def inventory_summary(self) -> dict[str, Any]:
        if self._static_summary is None:
            self._static_summary = self._build_static_summary()
        codes, total = self.event_catalog.counts()
        return {
            **self._static_summary,
            "event_codes_observed": codes,
            "all_events_observed": total,
            "inventory_error": self.inventory_error,
        }

    def diagnostics(self) -> dict[str, Any]:
        meaningful_records = {
            idx: {
                "classification": classify_alarm_record(cfg),
                "config": redact_sensitive(cfg),
            }
            for idx, cfg in self.alarm_records.items()
            if record_is_physical(cfg)
        }
        return {
            "inventory_summary": self.inventory_summary(),
            "available": self.available,
            "auth_failed": self.auth_failed,
            "enable_research_features": self.enable_research_features,
            "realtime": self.realtime.health() if self.realtime else None,
            "snapshot": self.snapshot_client.health() if self.snapshot_client else None,
            "engine": self.engine.metrics() if self.engine else None,
            "alarm_records": meaningful_records,
            "extended_rpc_inventory": redact_sensitive(self.rpc_inventory),
            "research_refresh_inventory": redact_sensitive(
                self.research_refresh_inventory
            ),
            "event_catalog": self.event_catalog.summary(),
            "wpan_research": (
                self.wpan_research.diagnostics()
                if self.wpan_research is not None
                else None
            ),
            "detector_test_control": (
                self.detector_test.diagnostics() if self.detector_test else None
            ),
            "pircam_snapshots": (
                redact_sensitive(self.pircam.diagnostics()) if self.pircam else None
            ),
            "area_hints": dict(sorted(self.area_hints.items())),
            "area_decisions": dict(self.area_decisions),
            "multiio_child_device_count": len(self.child_device_ids),
            "radio_devices": {
                level1: {
                    "alarm_index": d.alarm_index,
                    "name": d.name,
                    "sense_method": d.sense_method,
                    "classification": d.classification,
                    "model": d.model,
                    "serial": REDACTED if d.serial else None,
                    "serial_hash": d.serial_hash,
                    "parent_level1": d.parent_level1,
                    "area_hint": d.area_hint,
                }
                for level1, d in sorted(self.radio_devices.items())
            },
            "zone_states": {
                idx: {
                    "name": z.name,
                    "classification": z.classification,
                    "is_multiio": z.is_multiio,
                    "sense_method": z.sense_method,
                    "area_hint": z.area_hint or None,
                    "level1": z.level1,
                    "level2": z.level2,
                    "online_state": z.online_state,
                    "raw_alarm_state": z.raw_alarm_state,
                    "active": z.active,
                    "tamper": z.tamper,
                    "low_power": z.low_power,
                    "last_source": z.last_source,
                    "last_action": z.last_action,
                }
                for idx, z in self.zones.items()
            },
        }

    def stop(self) -> None:
        self._periodic_stop.set()
        if self.detector_test is not None:
            self.detector_test.cancel_timers()
        if self._periodic_thread is not None:
            self._periodic_thread.join(timeout=2)
        if self.wpan_research is not None:
            self.wpan_research.stop()
        if self.realtime is not None:
            self.realtime.stop()
        if self.engine is not None:
            self.engine.stop()
        if self.snapshot_client is not None:
            self.snapshot_client.close()


def probe_connection(
    host: str, http_port: int, dhip_port: int, username: str, password: str
) -> dict[str, Any]:
    """Validate credentials and read what the config flow needs."""
    records = fetch_alarm_config(host, http_port, username, password)
    parents = discover_multiio_parents(records)
    client = SnapshotClient(host, dhip_port, username, password)
    try:
        client.connect()
        snapshot = client.snapshot()
        zones = discover_alarm_points(records, snapshot, parents)
        by_pos = {x["array_pos"] for x in snapshot}
        missing_multiio = [
            idx
            for idx, cfg in records.items()
            if classify_alarm_record(cfg) == "multiio_input" and idx not in by_pos
        ]
        if missing_multiio:
            raise RuntimeError(
                f"Snapshot missing configured MultiIO zone indexes: {missing_multiio[:10]}"
            )
        # Fetch only AlarmSubSystem for config-flow area preview. This is a
        # small read-only request and makes the preview use the same Dahua
        # room assignment that runtime smart matching will use.
        area_hints: dict[int, str] = {}
        inventory_client = InventoryRpcClient(host, dhip_port, username, password)
        serial_number: str | None = None
        try:
            inventory_client.connect()
            serial_raw = rpc_response_value(
                inventory_client.safe_request("magicBox.getSerialNo")
            )
            serial_number = str(serial_raw) if serial_raw else None
            subsystem_result = inventory_client.safe_request(
                "configManager.getConfig", {"name": "AlarmSubSystem"}
            )
            area_hints = extract_zone_area_hints(
                {"candidate_configs": {"AlarmSubSystem": subsystem_result}}
            )
        except Exception:
            # Area intelligence is optional and must never make connection
            # validation fail. Sensor-name matching remains a safe fallback.
            area_hints = {}
        finally:
            inventory_client.close()

        primary = {
            idx: zones[idx]
            for idx in sorted(zones)
            if zones[idx].classification in PRIMARY_SENSOR_CLASSES
        }
        summary = summarize_alarm_records(records)
        return {
            "records": len(records),
            "zones": len(primary),
            "tracked_physical_records": len(zones),
            "serial_number": serial_number,
            "parents": len(parents),
            "snapshot_records": len(snapshot),
            "zone_names": [zone.name for zone in primary.values()],
            "area_match_items": [
                {"index": idx, "name": zone.name, "area_hint": area_hints.get(idx)}
                for idx, zone in primary.items()
            ],
            "multiio_zones": sum(1 for zone in zones.values() if zone.is_multiio),
            "non_multiio_zones": sum(
                1 for zone in primary.values() if not zone.is_multiio
            ),
            "meaningful_alarm_records": summary["meaningful_alarm_records"],
        }
    finally:
        client.close()
