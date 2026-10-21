"""Opt-in arm/disarm control as standard alarm control panels.

Off unless the ``enable_arm_control`` option is set and the hub has a verified
arm command. A command's result comes from the ARC's own events, never from
the RPC reply alone (see :mod:`.protocol.control`).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import arm_code
from .client import ArcHub
from .const import (
    ARM_MODE_AWAY,
    ARM_MODE_HOME,
    CONF_ARM_CODE_HASH,
    CONF_ARM_MODES,
    CONF_CODE_ARM_REQUIRED,
    CONF_CODE_DISARM_REQUIRED,
    CONF_ENABLE_ARM_CONTROL,
    DEFAULT_ARM_MODES,
    DEFAULT_CODE_ARM_REQUIRED,
    DEFAULT_CODE_DISARM_REQUIRED,
    DEFAULT_ENABLE_ARM_CONTROL,
    DOMAIN,
    VERIFIED_ARM_MODES,
)
from .entity import DahuaArcArmStateEntity
from .protocol.arming import (
    ARMED_AWAY,
    ARMED_HOME,
    ARMED_PARTIAL_2,
    DISARMED,
    ArmArea,
)
from .protocol.control import (
    ArmCommand,
    ArmMode,
    CommandOutcome,
    CommandResult,
)
from .vendor.dahua import const as dahua_const

_LOGGER = logging.getLogger(__name__)

# Unlimited: the controller rejects a second command at once. Letting HA queue
# it behind a slow arm would run a stale disarm or arm later.
PARALLEL_UPDATES = 0
HOME_ASSISTANT = "Home Assistant"
NO_PERMISSION_CODE = 268894210  # DAHUA_ERRORS: insufficient permissions

_STATE_MAP = {
    DISARMED: AlarmControlPanelState.DISARMED,
    ARMED_HOME: AlarmControlPanelState.ARMED_HOME,
    ARMED_AWAY: AlarmControlPanelState.ARMED_AWAY,
    ARMED_PARTIAL_2: AlarmControlPanelState.ARMED_NIGHT,
}
Pending = Literal["arming", "disarming"]


def panel_state(
    areas: list[ArmArea], pending: Pending | None
) -> tuple[AlarmControlPanelState | None, bool]:
    """Map target areas to a panel state and whether they are mixed.

    Precedence, top wins: a command in flight, then any alarm, then unknown,
    then one common state, otherwise mixed (03 section 2.4).
    """
    if pending == "arming":
        return AlarmControlPanelState.ARMING, False
    if pending == "disarming":
        return AlarmControlPanelState.DISARMING, False
    if any(area.alarm is True for area in areas):
        return AlarmControlPanelState.TRIGGERED, False
    states = {area.state for area in areas}
    if not states or None in states:
        return None, False
    if len(states) == 1:
        return _STATE_MAP.get(states.pop()), False
    return AlarmControlPanelState.ARMED_CUSTOM_BYPASS, True


def _error(key: str, **placeholders: object) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


def _validation_error(key: str, **placeholders: object) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ArcHub],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    # Read-only unless opted in; also nothing without a verified command.
    if not entry.options.get(CONF_ENABLE_ARM_CONTROL, DEFAULT_ENABLE_ARM_CONTROL):
        return
    hub = entry.runtime_data
    if hub.arm_control is None or hub.arming is None:
        return
    areas = list(hub.arming.areas.values())
    if not areas:
        _LOGGER.warning("Arm control needs at least one enabled ARC area")
        return
    entities: list[DahuaArcAlarmPanel] = [DahuaArcSystemPanel(hub, entry, areas)]
    if len(areas) > 1:
        entities.extend(DahuaArcAreaPanel(hub, entry, area) for area in areas)
    async_add_entities(entities)


class DahuaArcAlarmPanel(DahuaArcArmStateEntity, AlarmControlPanelEntity):
    """One panel controlling a fixed set of ARC areas."""

    _unrecorded_attributes = frozenset(
        {
            "area_states",
            "area_id",
            "open_zones",
            "last_arming_failure",
            "last_arming_failure_open_zones",
            "last_command",
            "forced",
            "bypassed_zones",
        }
    )

    def __init__(
        self, hub: ArcHub, entry: ConfigEntry[ArcHub], areas: list[ArmArea]
    ) -> None:
        super().__init__(hub, entry)
        self._areas = areas
        self._target = tuple(area.index for area in areas)
        self._pending: Pending | None = None
        self._code_hash: str | None = entry.options.get(CONF_ARM_CODE_HASH) or None
        self._disarm_requires_code = bool(
            entry.options.get(CONF_CODE_DISARM_REQUIRED, DEFAULT_CODE_DISARM_REQUIRED)
        )
        self._arm_requires_code = bool(
            entry.options.get(CONF_CODE_ARM_REQUIRED, DEFAULT_CODE_ARM_REQUIRED)
        )
        modes = entry.options.get(CONF_ARM_MODES, DEFAULT_ARM_MODES)
        self._modes = [m for m in VERIFIED_ARM_MODES if m in modes] or list(
            DEFAULT_ARM_MODES
        )
        self._limiter = arm_code.AttemptLimiter()
        self._last_command: dict[str, Any] | None = None
        self._ha_event_seq: int | None = None

    # -- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return (
            self.hub.available
            and self.hub.arm_control is not None
            and not self.hub.auth_failed
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        return panel_state(self._areas, self._pending)[0]

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        features = AlarmControlPanelEntityFeature(0)
        if ARM_MODE_HOME in self._modes:
            features |= AlarmControlPanelEntityFeature.ARM_HOME
        if ARM_MODE_AWAY in self._modes:
            features |= AlarmControlPanelEntityFeature.ARM_AWAY
        return features

    @property
    def code_format(self) -> CodeFormat | None:
        return CodeFormat.NUMBER if self._code_hash else None

    @property
    def code_arm_required(self) -> bool:
        return bool(self._code_hash) and self._arm_requires_code

    @property
    def changed_by(self) -> str | None:
        # Only our own confirmed commands are attributed. The ARC's trigger
        # field is not yet mapped to a readable origin.
        if self._ha_event_seq is None:
            return None
        newest = max(area.last_event_seq for area in self._areas)
        return HOME_ASSISTANT if newest <= self._ha_event_seq else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        failure = max(
            (a.last_failure for a in self._areas if a.last_failure is not None),
            key=lambda f: f.at,
            default=None,
        )
        attributes: dict[str, Any] = {
            "open_zones": self.hub.open_zones(self._target),
            "last_arming_failure": failure.at if failure else None,
            "last_arming_failure_open_zones": (
                [zone["zone"] for zone in failure.open_zones] if failure else None
            ),
            "last_command": self._last_command,
        }
        attributes.update(self._panel_attributes())
        return attributes

    def _panel_attributes(self) -> dict[str, Any]:
        raise NotImplementedError

    # -- commands ----------------------------------------------------------

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._async_command("D", code, disarm=True)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._async_command("p1", code, disarm=False)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._async_command("T", code, disarm=False)

    async def _async_check_code(self, code: str | None, *, disarm: bool) -> None:
        """Gate the command in Home Assistant. Nothing reaches the ARC on failure."""
        stored = self._code_hash
        if not stored:
            return
        required = self._disarm_requires_code if disarm else self._arm_requires_code
        if not required:
            return
        locked = self._limiter.seconds_locked()
        if locked:
            raise _validation_error("invalid_code_locked", seconds=locked)
        if not code:
            raise _validation_error("code_required")
        if not await self.hass.async_add_executor_job(
            arm_code.verify_code, code, stored
        ):
            self._limiter.record_failure()
            raise _validation_error("invalid_code")
        self._limiter.record_success()

    def _context_id(self) -> str:
        context = getattr(self, "_context", None)
        return f"ha:{context.id}" if context is not None else ""

    async def _async_command(
        self, mode: ArmMode, code: str | None, *, disarm: bool
    ) -> None:
        controller = self.hub.arm_control
        if not self.available or controller is None:
            raise _error("arm_unavailable")
        await self._async_check_code(code, disarm=disarm)

        # Already there: nothing to send, so repeated calls are idempotent.
        if all(area.raw_mode == mode for area in self._areas):
            return

        self._pending = "disarming" if disarm else "arming"
        self.async_write_ha_state()
        command = ArmCommand(mode=mode, areas=self._target, origin=self._context_id())
        try:
            result = await self.hass.async_add_executor_job(controller.execute, command)
        finally:
            self._pending = None
        self._record(result)
        self.async_write_ha_state()
        if result.outcome is not CommandOutcome.CONFIRMED:
            raise self._exception(result, controller.wait_timeout)

    def _record(self, result: CommandResult) -> None:
        self._last_command = {
            "at": result.finished_at,
            "mode": result.command.mode,
            "outcome": result.outcome.value,
            "confirmed_by": result.confirmed_by,
            "error": result.reason or result.rpc_error_message,
        }
        if result.outcome is CommandOutcome.CONFIRMED:
            self._ha_event_seq = max(area.last_event_seq for area in self._areas)

    def _area_names(self) -> str:
        return ", ".join(area.name for area in self._areas)

    def _exception(self, result: CommandResult, wait_timeout: float) -> Exception:
        outcome, reason = result.outcome, result.reason
        if outcome is CommandOutcome.REFUSED:
            zones = sorted(
                {zone["zone"] for zone in result.open_zones if zone.get("zone")}
            )
            if zones:
                return _error(
                    "arm_refused_open_zones",
                    areas=self._area_names(),
                    zones=", ".join(zones),
                )
            return _error("arm_refused", areas=self._area_names())
        if outcome is CommandOutcome.UNCONFIRMED:
            return _error("arm_unconfirmed", seconds=int(wait_timeout))
        if outcome is CommandOutcome.AUTH_FAILED:
            return _error("arm_auth_failed")
        if outcome is CommandOutcome.REJECTED:
            if reason == "busy":
                return _error("arm_busy")
            if reason == "unavailable":
                return _error("arm_unavailable")
        if outcome is CommandOutcome.FAILED:
            if reason == "unloading":
                return _error("arm_unavailable")
            if reason == "unreachable":
                return _error("arm_unreachable", error=result.rpc_error_message or "")
            if result.rpc_error_code == NO_PERMISSION_CODE:
                return _error("arm_not_permitted")
        return _error(
            "arm_failed",
            code=result.rpc_error_code
            if result.rpc_error_code is not None
            else (reason or "unknown"),
            message=result.rpc_error_message
            or dahua_const.error_message(result.rpc_error_code),
        )


class DahuaArcSystemPanel(DahuaArcAlarmPanel):
    """The whole ARC: every enabled area."""

    _attr_name = None

    def __init__(
        self, hub: ArcHub, entry: ConfigEntry[ArcHub], areas: list[ArmArea]
    ) -> None:
        super().__init__(hub, entry, areas)
        self._attr_unique_id = f"{self._uid}_alarm_panel"

    def _panel_attributes(self) -> dict[str, Any]:
        mixed = panel_state(self._areas, self._pending)[1]
        return {
            "areas": [area.name for area in self._areas],
            "mixed": mixed,
            "area_states": {area.name: area.state for area in self._areas},
        }


class DahuaArcAreaPanel(DahuaArcAlarmPanel):
    """One Dahua area."""

    _attr_translation_key = "area_alarm_panel"

    def __init__(self, hub: ArcHub, entry: ConfigEntry[ArcHub], area: ArmArea) -> None:
        super().__init__(hub, entry, [area])
        self.area = area
        self._attr_unique_id = f"{self._uid}_area_{area.area_id}_alarm_panel"
        self._attr_translation_placeholders = {"area": area.name}

    def _panel_attributes(self) -> dict[str, Any]:
        area = self.area
        return {
            "area_id": area.area_id,
            "forced": area.profile == "Force" if area.profile else None,
            "bypassed_zones": [zone["zone"] for zone in area.bypassed_zones],
        }
