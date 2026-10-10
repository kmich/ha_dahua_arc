"""Opt-in arm/disarm commands, confirmed by the ARC's own events.

This is the only module that can change the ARC's arm state. Nothing here runs
on its own: :meth:`ArmController.execute` is called only by a user or
automation action, never by discovery, setup, reload or periodic code.

Each command uses a short-lived DHIP session of its own, so a failure never
disturbs the realtime or snapshot sessions. Exactly one RPC is written per
command and it is never retried: a reply that is lost may still have armed the
system, and a repeated disarm or arm would not be safe. The outcome comes from
the ARC's ``AreaArmModeChange`` / ``ArmingFailure`` events, falling back to one
read of the ``AreaArmMode`` table.

The RPC itself is described by a :class:`CommandSpec`. The real spec,
:data:`ARM_COMMAND_SPEC`, stays ``None`` until its method name and parameter
shape are verified on hardware (see ``docs/arm-control/02-protocol.md``).
Tests use :data:`FAKE_COMMAND_SPEC`, which is refused outside tests.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from ..vendor.dahua import DHIPTransport, const
from ..vendor.dahua.exceptions import LoginError
from .arming import (
    ARM_MODES,
    ArmingTracker,
    OutcomeMark,
    parse_abnormal_zones,
    parse_area_arm_modes,
)
from .util import timestamp

_LOGGER = logging.getLogger(__name__)

ArmMode = Literal["D", "p1", "T", "p2"]

CONNECT_TIMEOUT_SECONDS = 12
REPLY_TIMEOUT_SECONDS = 10
WAIT_TIMEOUT_SECONDS = 10.0
HISTORY_SIZE = 20


class CommandOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REFUSED = "refused"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    REJECTED = "rejected"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True, slots=True)
class ArmCommand:
    mode: ArmMode
    # Zero-based AlarmSubSystem indexes, sorted and non-empty.
    areas: tuple[int, ...]
    force: bool = False
    # Home Assistant context id, kept for diagnostics and never sent.
    origin: str = ""


@dataclass(slots=True)
class CommandResult:
    outcome: CommandOutcome
    command: ArmCommand
    started_at: str
    finished_at: str = ""
    confirmed_by: Literal["event", "table"] | None = None
    rpc_error_code: int | None = None
    rpc_error_message: str | None = None
    open_zones: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Diagnostics form: the command's shape, never request parameters."""
        return {
            "outcome": self.outcome.value,
            "mode": self.command.mode,
            "areas": list(self.command.areas),
            "force": self.command.force,
            "origin": self.command.origin,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "confirmed_by": self.confirmed_by,
            "rpc_error_code": self.rpc_error_code,
            "rpc_error_message": self.rpc_error_message,
            "open_zones": list(self.open_zones),
            "reason": self.reason,
        }


class ReplyResult(NamedTuple):
    ok: bool
    error_code: int | None = None
    error_message: str | None = None
    open_zones: list[dict[str, Any]] = []  # noqa: RUF012 - never mutated


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """The single place that encodes the ARC's arm RPC."""

    method: str
    # Instance services need ``<service>.factory.instance`` first and
    # ``<service>.destroy`` afterwards, like the detector test.
    needs_instance: bool
    build_params: Callable[[ArmCommand, str], Any]
    parse_reply: Callable[[dict[str, Any]], ReplyResult]


def error_fields(reply: dict[str, Any]) -> tuple[int | None, str | None]:
    error = reply.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, int) and not isinstance(code, bool) else None,
        str(message) if message else None,
    )


def _fake_build_params(command: ArmCommand, password: str) -> dict[str, Any]:
    return {"Mode": command.mode, "Areas": list(command.areas)}


def _fake_parse_reply(reply: dict[str, Any]) -> ReplyResult:
    if reply.get("result"):
        return ReplyResult(True)
    code, message = error_fields(reply)
    error = reply.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    zones = parse_abnormal_zones(data) if isinstance(data, dict) else []
    return ReplyResult(False, code, message, zones)


# Matches the fake ARC in tests. This method name must never be sent to real
# hardware; ArmController refuses this spec unless told it is under test.
FAKE_COMMAND_SPEC = CommandSpec(
    method="FakeArc.setArmMode",
    needs_instance=False,
    build_params=_fake_build_params,
    parse_reply=_fake_parse_reply,
)

# Filled in from the Phase 0 evidence table. Until then no real arm command
# exists and the integration cannot send one.
ARM_COMMAND_SPEC: CommandSpec | None = None


def _object_id(reply: dict[str, Any]) -> int | None:
    """Object id from a ``factory.instance`` reply (returned as ``result``)."""
    params = reply.get("params")
    candidates: list[Any] = [reply.get("result"), reply.get("object")]
    if isinstance(params, dict):
        candidates += [params.get("object"), params.get("Object")]
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


class ArmController:
    """Send one confirmed arm/disarm command at a time."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        spec: CommandSpec,
        tracker: ArmingTracker,
        read_table: Callable[[], Any],
        hub_available: Callable[[], bool],
        auth_failed: Callable[[], bool],
        on_auth_failed: Callable[[LoginError], None],
        stop_event: threading.Event,
        transport_factory: Callable[[], DHIPTransport] | None = None,
        wait_timeout: float = WAIT_TIMEOUT_SECONDS,
        reply_timeout: float = REPLY_TIMEOUT_SECONDS,
        allow_fake_spec: bool = False,
    ) -> None:
        if spec is FAKE_COMMAND_SPEC and not allow_fake_spec:
            raise ValueError("The fake arm command spec is for tests only")
        self.spec = spec
        self.tracker = tracker
        self.wait_timeout = wait_timeout
        self.reply_timeout = reply_timeout
        self._username, self._password = username, password
        self._read_table = read_table
        self._hub_available = hub_available
        self._auth_failed = auth_failed
        self._on_auth_failed = on_auth_failed
        self._stop_event = stop_event
        self._transport_factory = transport_factory or (
            lambda: DHIPTransport(host, port, timeout=CONNECT_TIMEOUT_SECONDS)
        )
        self._command_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)

    # -- public ------------------------------------------------------------

    @property
    def in_flight(self) -> bool:
        return self._command_lock.locked()

    def execute(self, command: ArmCommand) -> CommandResult:
        """Run ``command`` to a result. Blocking: call from an executor."""
        started = timestamp()
        if not self._command_lock.acquire(blocking=False):
            return self._finish(
                self._result(CommandOutcome.REJECTED, command, started, "busy")
            )
        try:
            result = self._execute_locked(command, started)
        except Exception as exc:
            # Never let an unexpected error escape with the lock state unknown.
            _LOGGER.exception("ARC arm command crashed")
            result = self._result(
                CommandOutcome.FAILED,
                command,
                started,
                "unreachable",
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._command_lock.release()
        return self._finish(result)

    def history(self) -> list[dict[str, Any]]:
        with self._history_lock:
            return list(self._history)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "command_spec": self.spec.method,
            "in_flight": self.in_flight,
            "history": self.history(),
        }

    # -- internals ---------------------------------------------------------

    def _result(
        self,
        outcome: CommandOutcome,
        command: ArmCommand,
        started: str,
        reason: str | None = None,
        *,
        code: int | None = None,
        message: str | None = None,
        open_zones: list[dict[str, Any]] | None = None,
        confirmed_by: Literal["event", "table"] | None = None,
    ) -> CommandResult:
        if message and self._password:
            message = message.replace(self._password, "***")
        return CommandResult(
            outcome=outcome,
            command=command,
            started_at=started,
            confirmed_by=confirmed_by,
            rpc_error_code=code,
            rpc_error_message=message,
            open_zones=open_zones or [],
            reason=reason,
        )

    def _finish(self, result: CommandResult) -> CommandResult:
        result.finished_at = timestamp()
        with self._history_lock:
            self._history.append(result.to_dict())
        command = result.command
        names = [
            self.tracker.areas[i].name for i in command.areas if i in self.tracker.areas
        ]
        level = (
            logging.INFO
            if result.outcome in (CommandOutcome.CONFIRMED, CommandOutcome.REFUSED)
            else logging.WARNING
        )
        _LOGGER.log(
            level,
            "ARC arm command %s areas=%s origin=%s -> %s (%s)",
            command.mode,
            names,
            command.origin or "-",
            result.outcome.value,
            result.confirmed_by
            or result.reason
            or result.rpc_error_message
            or result.rpc_error_code,
        )
        return result

    def _execute_locked(self, command: ArmCommand, started: str) -> CommandResult:
        def reject(reason: str) -> CommandResult:
            return self._result(CommandOutcome.REJECTED, command, started, reason)

        if not command.areas or any(a not in self.tracker.areas for a in command.areas):
            return reject("unknown_area")
        if command.mode not in ARM_MODES:
            return reject("unsupported_mode")
        if self._stop_event.is_set():
            return self._result(CommandOutcome.FAILED, command, started, "unloading")
        if self._auth_failed():
            return self._result(CommandOutcome.AUTH_FAILED, command, started)
        if not self._hub_available():
            return reject("unavailable")

        mark = self.tracker.outcome_mark()
        reply = self._send(command, started)
        if isinstance(reply, CommandResult):
            return reply

        parsed = self.spec.parse_reply(reply)
        if not parsed.ok:
            outcome = (
                CommandOutcome.REFUSED if parsed.open_zones else CommandOutcome.FAILED
            )
            return self._result(
                outcome,
                command,
                started,
                code=parsed.error_code,
                message=parsed.error_message,
                open_zones=list(parsed.open_zones),
            )
        return self._confirm(command, started, mark)

    def _send(
        self, command: ArmCommand, started: str
    ) -> dict[str, Any] | CommandResult:
        """Write the one RPC. A failure after the write is never retried."""
        transport = self._transport_factory()
        object_id: int | None = None
        clean = True
        try:
            try:
                transport.connect()
                transport.login(self._username, self._password)
            except LoginError as exc:
                self._on_auth_failed(exc)
                return self._result(CommandOutcome.AUTH_FAILED, command, started)
            transport.set_timeout(self.reply_timeout)
            if self.spec.needs_instance:
                service = self.spec.method.split(".", 1)[0]
                created = transport.call(f"{service}.factory.instance", None)
                object_id = _object_id(created)
                if object_id is None:
                    code, message = error_fields(created)
                    return self._result(
                        CommandOutcome.FAILED,
                        command,
                        started,
                        "instance_failed",
                        code=code,
                        message=message,
                    )
            params = self.spec.build_params(command, self._password)
            return transport.call(self.spec.method, params, object_id=object_id)
        except Exception as exc:
            # Before the write nothing happened; after it the command may have
            # taken effect. Either way: report, never resend.
            clean = False
            return self._result(
                CommandOutcome.FAILED,
                command,
                started,
                "unreachable",
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._close(transport, object_id, clean=clean)

    def _close(
        self, transport: DHIPTransport, object_id: int | None, *, clean: bool
    ) -> None:
        """Destroy the instance and log out, best effort, then drop the socket.

        After a transport error the stream state is unknown, so just close.
        """
        if clean and transport.sock is not None:
            if object_id is not None:
                service = self.spec.method.split(".", 1)[0]
                with contextlib.suppress(Exception):
                    transport.call(f"{service}.destroy", None, object_id=object_id)
            with contextlib.suppress(Exception):
                transport.call(const.LOGOUT, None)
        transport.close()

    def _confirm(
        self, command: ArmCommand, started: str, mark: OutcomeMark
    ) -> CommandResult:
        raw = command.mode
        waited = self.tracker.wait_for_outcome(
            mark, command.areas, raw, self.wait_timeout, self._stop_event
        )
        if waited.outcome == "stopped":
            return self._result(CommandOutcome.FAILED, command, started, "unloading")
        if waited.outcome == "refused":
            return self._result(
                CommandOutcome.REFUSED,
                command,
                started,
                open_zones=list(waited.open_zones),
            )
        if waited.outcome == "confirmed":
            return self._result(
                CommandOutcome.CONFIRMED,
                command,
                started,
                confirmed_by=waited.confirmed_by,
            )

        # No event in time: read the table once. Never resend.
        watermark = self.tracker.watermark()
        try:
            modes = parse_area_arm_modes(self._read_table())
        except LoginError as exc:
            self._on_auth_failed(exc)
            return self._result(CommandOutcome.AUTH_FAILED, command, started)
        except Exception as exc:
            self.tracker.table_failed(f"{type(exc).__name__}: {exc}")
            return self._result(
                CommandOutcome.UNCONFIRMED,
                command,
                started,
                "table_unreadable",
                message=f"{type(exc).__name__}: {exc}",
            )
        self.tracker.apply_table(modes, watermark)
        again = self.tracker.wait_for_outcome(mark, command.areas, raw, 0.0)
        if again.outcome == "confirmed":
            return self._result(
                CommandOutcome.CONFIRMED,
                command,
                started,
                confirmed_by=again.confirmed_by,
            )
        if again.outcome == "refused":
            return self._result(
                CommandOutcome.REFUSED,
                command,
                started,
                open_zones=list(again.open_zones),
            )
        return self._result(CommandOutcome.UNCONFIRMED, command, started, "timeout")
