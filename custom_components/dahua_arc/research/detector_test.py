"""ARD1731 PIRCam detector (sensitivity) test control — research mode only.

This is the only code path in the integration that WRITES to the ARC. It
toggles the accessory's SensitivityTest flag through
``LowRateWPAN.setAccessoryParam`` and nothing else: it never arms/disarms the
ARC and never touches siren/output methods. A started test is automatically
stopped after :data:`AUTO_STOP_SECONDS`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..protocol.models import Zone
from ..protocol.util import timestamp
from ..vendor.dahua import DHIPTransport

AUTO_STOP_SECONDS = 120
PAYLOAD_STYLES = ("InfoPascal", "InfoSdkNames", "StuInfoSdkNames")


def detector_test_targets(zones: dict[int, Zone]) -> dict[int, Zone]:
    """Every configured PIR camera with a wireless short address."""
    return {
        idx: zone
        for idx, zone in zones.items()
        if zone.sense_method == "PIRCam" and zone.level1 is not None and zone.level1 > 0
    }


def detector_test_payload(style: str, short_addr: int, enabled: bool) -> dict[str, Any]:
    """Build one bounded candidate representation of the same operation.

    Public NetSDK maps the operation to NET_WPAN_ACCESSORY_INFO fields
    nShortAddr + bySensitivityTest. Dahua DHIP commonly strips NetSDK type
    prefixes and exposes PascalCase JSON, but this firmware's exact JSON
    spelling is undocumented. Every candidate below therefore expresses only
    the same two semantic fields: target address and SensitivityTest.
    """
    value = 1 if enabled else 0
    if style == "InfoPascal":
        return {"Info": {"ShortAddr": short_addr, "SensitivityTest": value}}
    if style == "InfoSdkNames":
        return {"Info": {"nShortAddr": short_addr, "bySensitivityTest": value}}
    if style == "StuInfoSdkNames":
        return {"stuInfo": {"nShortAddr": short_addr, "bySensitivityTest": value}}
    raise ValueError(f"Unknown detector-test payload style: {style}")


def extract_factory_object(response: dict[str, Any]) -> int | None:
    """Extract a Dahua factory object id from known RPC response shapes."""
    params = response.get("params")
    # Dahua instance services return the newly-created object ID directly in
    # the top-level "result" field. This is not merely a boolean.
    candidates: list[Any] = [
        response.get("result"),
        response.get("object"),
        response.get("Object"),
    ]
    if isinstance(params, dict):
        candidates.extend(
            [
                params.get("object"),
                params.get("Object"),
                params.get("objectId"),
                params.get("ObjectID"),
            ]
        )
    for value in candidates:
        if isinstance(value, bool):
            continue
        try:
            obj = int(value)
        except TypeError, ValueError:
            continue
        if obj > 0:
            return obj
    return None


def response_summary(response: dict[str, Any]) -> dict[str, Any]:
    error = response.get("error")
    return {
        "result": response.get("result"),
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "error_message": error.get("message") if isinstance(error, dict) else None,
        "object": extract_factory_object(response),
    }


def _new_state(zone: Zone) -> dict[str, Any]:
    return {
        "target_name": zone.name,
        "target_index": zone.index,
        "target_short_addr": zone.level1,
        "enabled": False,
        "auto_stop_seconds": AUTO_STOP_SECONDS,
        "last_requested_at": None,
        "last_action": None,
        "last_success": None,
        "last_error": None,
        "attempts": [],
        "factory_attempts": [],
        "last_factory_object": None,
        "last_destroy_result": None,
    }


class DetectorTestController:
    """Start/stop the ARD1731 detector test on any configured PIR camera."""

    def __init__(
        self,
        host: str,
        dhip_port: int,
        username: str,
        password: str,
        zones: dict[int, Zone],
        notify: Callable[[set[int] | None], None],
    ) -> None:
        self.host, self.dhip_port = host, dhip_port
        self.username, self.password = username, password
        self.targets = detector_test_targets(zones)
        self._notify = notify
        self._lock = threading.RLock()
        self._timers: dict[int, threading.Timer] = {}
        # Firmware-level preference: the payload spelling that worked once.
        self._preferred_style: str | None = None
        self._state: dict[int, dict[str, Any]] = {
            idx: _new_state(zone) for idx, zone in self.targets.items()
        }

    def _target(self, index: int) -> Zone:
        zone = self.targets.get(index)
        if zone is None:
            raise RuntimeError(f"Alarm index {index} is not a PIRCam detector")
        return zone

    def _rpc(self, index: int, enabled: bool, *, automatic: bool = False) -> dict:
        """Execute the Dahua instance-service form of setAccessoryParam.

        The direct service-level call is rejected by the tested firmware.
        Dahua RPC tooling uses service.factory.instance ->
        service.method(object=...) -> service.destroy for instance services.
        """
        zone = self._target(index)
        short_addr = int(zone.level1 or 0)

        with self._lock:
            preferred = self._preferred_style
        styles = [preferred] if preferred else []
        styles += [style for style in PAYLOAD_STYLES if style not in styles]

        transport = DHIPTransport(self.host, self.dhip_port, timeout=12)
        attempts: list[dict[str, Any]] = []
        factory_attempts: list[dict[str, Any]] = []
        successful_style: str | None = None
        successful_result: dict[str, Any] | None = None
        object_id: int | None = None
        destroy_summary: dict[str, Any] | None = None

        try:
            transport.connect()
            transport.login(self.username, self.password)

            # Dahua tooling normalizes empty factory params to JSON null.
            # Try null first, then {} only if this firmware rejects null.
            for factory_style, factory_params in (("null", None), ("empty_object", {})):
                response = transport.call(
                    "LowRateWPAN.factory.instance", factory_params
                )
                factory_attempts.append(
                    {
                        "at": timestamp(),
                        "style": factory_style,
                        **response_summary(response),
                    }
                )
                object_id = extract_factory_object(response)
                if object_id is not None:
                    break

            if object_id is None:
                raise RuntimeError(
                    "LowRateWPAN.factory.instance did not return a usable object"
                )

            for style in styles:
                response = transport.call(
                    "LowRateWPAN.setAccessoryParam",
                    detector_test_payload(style, short_addr, enabled),
                    object_id=object_id,
                )
                summary = {
                    "at": timestamp(),
                    "action": "start" if enabled else "stop",
                    "automatic": automatic,
                    "style": style,
                    "object": object_id,
                    **response_summary(response),
                }
                attempts.append(summary)
                if response.get("result"):
                    successful_style = style
                    successful_result = summary
                    break
        finally:
            # Destroy the short-lived LowRateWPAN instance whenever one was
            # created. Failure to destroy is recorded but does not overwrite
            # the primary command result.
            if object_id is not None and transport.sock is not None:
                try:
                    destroy_summary = response_summary(
                        transport.call("LowRateWPAN.destroy", None, object_id=object_id)
                    )
                except Exception as exc:
                    destroy_summary = {
                        "result": False,
                        "error_code": None,
                        "error_message": f"{type(exc).__name__}: {exc}",
                        "object": object_id,
                    }
            transport.close()

        with self._lock:
            state = self._state[index]
            state["last_requested_at"] = timestamp()
            state["last_action"] = (
                "auto-stop"
                if automatic and not enabled
                else ("start" if enabled else "stop")
            )
            state["factory_attempts"] = factory_attempts[-10:]
            state["last_factory_object"] = object_id
            state["last_destroy_result"] = destroy_summary
            state["attempts"] = [*state["attempts"], *attempts][-40:]

            if successful_style is None:
                state["last_success"] = False
                if object_id is None:
                    detail = "factory.instance returned no object"
                elif attempts:
                    errors = ", ".join(
                        f"{a['style']}={a.get('error_code')}:{a.get('error_message')}"
                        for a in attempts
                    )
                    detail = f"instance setAccessoryParam rejected: {errors}"
                else:
                    detail = "instance setAccessoryParam produced no result"
                state["last_error"] = detail
            else:
                self._preferred_style = successful_style
                state["successful_style"] = successful_style
                state["enabled"] = enabled
                state["last_success"] = True
                state["last_error"] = None
                detail = None

        self._notify({index})
        if detail is not None:
            raise RuntimeError(detail)
        return dict(successful_result or {})

    def start(self, index: int) -> dict[str, Any]:
        """Start the detector test for :data:`AUTO_STOP_SECONDS` seconds."""
        result = self._rpc(index, True)
        with self._lock:
            old = self._timers.pop(index, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(AUTO_STOP_SECONDS, self._auto_stop, args=(index,))
            timer.daemon = True
            self._timers[index] = timer
            timer.start()
        return result

    def _auto_stop(self, index: int) -> None:
        try:
            self._rpc(index, False, automatic=True)
        except Exception as exc:
            with self._lock:
                self._state[index]["last_error"] = (
                    f"Automatic detector-test stop failed: {type(exc).__name__}: {exc}"
                )
        finally:
            with self._lock:
                self._timers.pop(index, None)

    def stop(self, index: int) -> dict[str, Any]:
        """Stop the detector test immediately."""
        with self._lock:
            timer = self._timers.pop(index, None)
        if timer is not None:
            timer.cancel()
        return self._rpc(index, False)

    def status(self, index: int) -> dict[str, Any]:
        with self._lock:
            data = dict(self._state.get(index) or {})
            timer = self._timers.get(index)
            data["timer_active"] = bool(timer and timer.is_alive())
            return data

    def diagnostics(self) -> dict[int, dict[str, Any]]:
        return {idx: self.status(idx) for idx in sorted(self._state)}

    def cancel_timers(self) -> None:
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()
