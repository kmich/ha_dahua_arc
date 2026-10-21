"""Supervised single-call arm/disarm probe for a Dahua ARC (Phase 0.4).

NOT part of the integration package (``scripts/check_package.py`` asserts it is
never shipped). Run it only while standing at the panel, with everyone in the
house informed. It sends exactly one RPC per run: no loops, no retries.

    set ARC_PASSWORD=...        (never pass the password on the command line)
    python scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --method AlarmRegion.setArmMode --params-json '{"...": "..."}' \\
        --i-am-at-the-panel

What a run does:

1. logs in, reads ``AreaArmMode`` (before);
2. checks the guards below and shows the exact request;
3. asks you to type SEND (skip with ``--yes``);
4. attaches an event session, sends the one request, prints the reply;
5. prints arm/alarm/failure events for ``--listen`` seconds;
6. reads ``AreaArmMode`` again (after) and writes a redacted transcript.

Guards. Unless ``--allow-arm`` is given, the run only goes ahead when every
area is already disarmed (``D``) and the parameters look like a disarm: they
must contain the string ``D`` and no arm-like string (``p1``, ``T``, ``Home``,
``Away`` ...). A disarm sent while everything is disarmed cannot arm anything,
so it is a safe way to learn which parameter shape the ARC accepts. Only the
alarm services listed in ``ALLOWED_SERVICES`` may be called, and
``configManager`` writes are never allowed.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.dahua_arc.vendor.dahua import (  # noqa: E402
    DHIPError,
    DHIPTransport,
    LoginError,
    const,
)

ALLOWED_SERVICES = ("AlarmRegion", "alarmSubregion", "alarm", "alarmSubSystem")
METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*$")
# Strings that mean "arm" in any plausible parameter shape; matched
# case-insensitively against every string value in the parameters.
ARM_LIKE = {
    "p1",
    "p2",
    "t",
    "home",
    "away",
    "arm",
    "armed",
    "total",
    "partial",
    "partial1",
    "partial2",
    "stay",
    "night",
}
DISARMED = "D"
EVENT_HINTS = ("arm", "alarm", "defence", "defense", "region")
REDACT_KEYS = {
    "password",
    "session",
    "sn",
    "serialno",
    "userid",
    "nickname",
    "operatorinfo",
    "netclientaddr",
    "mac",
    "username",
}


class GuardError(Exception):
    """The requested trial is refused before anything is sent."""


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in string_leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in string_leaves(v)]
    return []


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "**REDACTED**" if k.casefold() in REDACT_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def check_method(method: str) -> None:
    if not METHOD_RE.match(method):
        raise GuardError(f"{method!r} is not a service.method name")
    service = method.partition(".")[0]
    if service not in ALLOWED_SERVICES:
        raise GuardError(
            f"service {service!r} is not allowed; use one of {ALLOWED_SERVICES}"
        )


def check_disarm_only(params: Any, modes: list[str]) -> None:
    """Refuse anything that could arm unless every area is already disarmed."""
    leaves = string_leaves(params)
    lowered = {leaf.casefold() for leaf in leaves}
    armish = sorted(lowered & ARM_LIKE)
    if armish:
        raise GuardError(
            f"parameters contain arm-like value(s) {armish}; "
            "re-run with --allow-arm only if you mean to arm"
        )
    if DISARMED not in leaves:
        raise GuardError(
            f"parameters must contain the disarm mode {DISARMED!r} "
            "(or pass --allow-arm)"
        )
    armed = [f"area {i}: {m}" for i, m in enumerate(modes) if m != DISARMED]
    if armed:
        raise GuardError(
            "an area is not disarmed, so this trial is not a no-op: "
            + ", ".join(armed)
            + " (disarm first, or pass --allow-arm)"
        )


def read_modes(transport: DHIPTransport) -> list[str]:
    reply = transport.call(
        "configManager.getConfig", {"name": "AreaArmMode"}, fragmented=True
    )
    if not reply.get("result"):
        raise GuardError(f"could not read AreaArmMode: {reply.get('error')}")
    rows = ((reply.get("params") or {}).get("table") or {}).get("Areas") or []
    return [str(row.get("Mode", DISARMED)) for row in rows]


def attach_events(args: argparse.Namespace, password: str, factory) -> DHIPTransport:
    transport = factory(args.host, args.port, timeout=12)
    try:
        transport.connect()
        transport.login(args.user, password)
        reply, _ = transport.request(const.EVENT_ATTACH, {"codes": ["All"]})
        if not reply.get("result"):
            raise DHIPError(f"eventManager.attach failed: {reply}")
    except Exception:
        transport.close()
        raise
    return transport


def listen(
    transport: DHIPTransport, seconds: float, emit: Callable[[str], None]
) -> list[dict[str, Any]]:
    """Collect events for ``seconds``; keepalives and unrelated codes are skipped."""
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        transport.set_timeout(min(1.0, remaining))
        try:
            frame, _, _ = transport.recv_frame()
        except TimeoutError, OSError:
            continue
        params = frame.get("params") or {}
        for event in params.get("eventList") or ([params] if params else []):
            code = str(event.get("Code", ""))
            if not any(hint in code.casefold() for hint in EVENT_HINTS):
                continue
            seen.append(event)
            data = event.get("Data") or {}
            emit(
                f"  event {code} index={event.get('Index')} mode={data.get('Mode')} "
                f"global={data.get('IsGlobal')} trigger={data.get('TriggerMode')}"
            )
    return seen


def run(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
    factory=DHIPTransport,
) -> dict[str, Any]:
    check_method(args.method)
    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError as exc:
        raise GuardError(f"--params-json is not valid JSON: {exc}") from exc

    control = factory(args.host, args.port, timeout=12)
    events = None
    transcript: dict[str, Any] = {
        "started": now(),
        "host_redacted": True,
        "method": args.method,
        "params": params,
        "allow_arm": args.allow_arm,
    }
    try:
        control.connect()
        control.login(args.user, password)
        before = read_modes(control)
        transcript["modes_before"] = before
        emit(f"AreaArmMode before: {before}")
        if not args.allow_arm:
            check_disarm_only(params, before)
        request = {"method": args.method, "params": params}
        emit("About to send exactly ONE request:\n" + json.dumps(request, indent=2))
        if not args.yes and ask("Type SEND to continue: ").strip() != "SEND":
            raise GuardError("not confirmed; nothing was sent")

        events = attach_events(args, password, factory)
        transcript["sent_at"] = now()
        try:
            reply = control.call(args.method, params, fragmented=True)
            transcript["reply"] = redact(reply)
            emit("Reply: " + json.dumps(redact(reply)))
        except Exception as exc:  # report, never retry
            transcript["send_error"] = f"{type(exc).__name__}: {exc}"
            emit(f"Send failed ({transcript['send_error']}); NOT retrying.")
        emit(f"Listening for events for {args.listen:.0f}s ...")
        transcript["events"] = [redact(e) for e in listen(events, args.listen, emit)]
        after = read_modes(control)
        transcript["modes_after"] = after
        transcript["modes_changed"] = after != before
        emit(f"AreaArmMode after: {after} (changed: {after != before})")
        return transcript
    finally:
        for transport in (events, control):
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=const.DEFAULT_PORT)
    parser.add_argument("--user", required=True)
    parser.add_argument("--method", required=True, help="e.g. AlarmRegion.setArmMode")
    parser.add_argument("--params-json", required=True, help="request params as JSON")
    parser.add_argument(
        "--i-am-at-the-panel",
        action="store_true",
        help="required: you are physically at the panel and the house is informed",
    )
    parser.add_argument(
        "--allow-arm",
        action="store_true",
        help="permit parameters that may arm; default is disarm-on-disarmed only",
    )
    parser.add_argument("--yes", action="store_true", help="skip the SEND prompt")
    parser.add_argument("--listen", type=float, default=20.0, help="seconds of events")
    parser.add_argument("--out", type=Path, help="write the redacted transcript here")
    args = parser.parse_args(argv)
    if not args.i_am_at_the_panel:
        parser.error("refusing to run without --i-am-at-the-panel")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = os.environ.get("ARC_PASSWORD") or (
        getpass.getpass("ARC password: ") if sys.stdin.isatty() else ""
    )
    if not password:
        print("Set ARC_PASSWORD (the password is never taken from the command line).")
        return 2
    try:
        transcript = run(args, password)
    except GuardError as exc:
        print(f"Refused: {exc}")
        return 2
    except (LoginError, DHIPError, OSError) as exc:
        print(f"Failed: {type(exc).__name__}: {exc}")
        return 1
    if args.out:
        args.out.write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        print(f"Transcript written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
