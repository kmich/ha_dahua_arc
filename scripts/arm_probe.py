"""Supervised single-call arm/disarm probe for a Dahua ARC (Phase 0.4).

NOT part of the integration package (``scripts/check_package.py`` asserts it is
never shipped). Run it only while standing at the panel, with everyone in the
house informed. It sends exactly one RPC per run: no loops, no retries.

    (needs Python 3.14, nothing else; the password is prompted for, or read from
    the ARC_PASSWORD environment variable, and is never taken from the command line)
    py -3.14 scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --shape areas-mode --i-am-at-the-panel --out trial1.json

``--list-shapes`` prints the built-in disarm-only guesses; ``--params-json``
sends custom parameters instead (mind your shell's quoting).

Alarm-user code. The ARC's alarm users each have a code, and the arm request may
need one. ``--prompt-code`` asks for it with no echo and uses it only for the
shapes that carry it (``areas-password`` ...) or a ``__CODE__`` value in
``--params-json``. It is never read from the command line, a file, the
environment or a pipe, and it is replaced by ``**CODE**`` in everything printed
and in the transcript:

    py -3.14 scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --shape areas-usernumber --user-number 1 --prompt-code \\
        --i-am-at-the-panel --out trial9.json

Login client type. ``--client-type`` (default ``Web3.0``) sets the DHIP login
``clientType`` for a trial. ``--probe-client-types`` is a read-only mode: it logs
in under each client type (``--client-types A,B`` to choose) and calls the
getters that fail under ``Web3.0`` once each, printing which succeed. It needs no
shape, code or panel flag, never arms anything, and stops at once on a login
failure that could count towards an account lockout:

    py -3.14 scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --probe-client-types --out client_types.json

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
# Load the vendored DHIP client as the top-level package ``dahua``. Importing it
# through ``custom_components.dahua_arc`` would run the integration's
# ``__init__`` and need Home Assistant installed; this script must run on any
# plain Python.
sys.path.insert(0, str(ROOT / "custom_components" / "dahua_arc" / "vendor"))

from dahua import DHIPError, DHIPTransport, LoginError, const  # noqa: E402

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
# Disarm-only parameter guesses for ``--shape`` (no JSON quoting needed). ``a`` is
# the zero-based area index; one-based ids are ``a + 1``.
SHAPES: dict[str, Callable[[int], dict[str, Any]]] = {
    "areas-mode": lambda a: {"Mode": DISARMED, "Areas": [a]},
    "area-mode": lambda a: {"Area": a, "Mode": DISARMED},
    "index-mode": lambda a: {"Index": a, "Mode": DISARMED},
    "areaid-mode": lambda a: {"AreaID": a + 1, "Mode": DISARMED},
    "areas-objects": lambda a: {"Areas": [{"Index": a, "Mode": DISARMED}]},
    "armmode-objects": lambda a: {"armMode": [{"Index": a, "Mode": DISARMED}]},
}
# The alarm users on the ARC have a Password (the arming code) and a UserNumber,
# so the arm call may need them. These guesses carry the code; ``code`` is the real
# code when sending and CODE_PLACEHOLDER everywhere it could be shown or stored.
CODE_PLACEHOLDER = "**CODE**"
CODE_TOKEN = "__CODE__"  # noqa: S105  (a placeholder in --params-json, not a secret)
CODE_SHAPES: dict[str, Callable[[int, str, int], dict[str, Any]]] = {
    "areas-password": lambda a, c, n: {"Mode": DISARMED, "Areas": [a], "Password": c},
    "areas-usernumber": lambda a, c, n: {
        "Mode": DISARMED,
        "Areas": [a],
        "UserNumber": n,
        "Password": c,
    },
    "areas-userid": lambda a, c, n: {
        "Mode": DISARMED,
        "Areas": [a],
        "UserID": n,
        "Password": c,
    },
    "areas-user-object": lambda a, c, n: {
        "Mode": DISARMED,
        "Areas": [a],
        "User": {"UserNumber": n, "Password": c},
    },
    "areas-objects-password": lambda a, c, n: {
        "Areas": [{"Index": a, "Mode": DISARMED}],
        "Password": c,
    },
    "armmode-objects-password": lambda a, c, n: {
        "armMode": [{"Index": a, "Mode": DISARMED}],
        "Password": c,
    },
}
DEFAULT_METHOD = "AlarmRegion.setArmMode"
DEFAULT_CLIENT_TYPE = "Web3.0"
# --probe-client-types: the DHIP login "clientType" values tried, and the
# read-only getters called under each. Unknown values simply fail to log in.
CLIENT_TYPES = ("Web3.0", "Local", "NetKeyboard", "Dahua3.0", "Dahua3.0-Web3.0")
CLIENT_TYPE_GETTERS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("AlarmRegion.getArmMode", {}),
    ("AlarmRegion.getZoneArmMode", {}),
    ("AlarmRegion.getZoneArmMode", {"Zones": [0]}),
    ("AlarmRegion.getUserArmModeAuth", {}),
    ("AlarmRegion.getUserArmModeAuth", {"UserID": 0}),
)
# Login failures that may count towards an account lockout: stop at once.
LOCKOUT_RISK_CODES = {268632080, 268632081, 268632082}
EVENT_HINTS = ("arm", "alarm", "defence", "defense", "region")
REDACT_KEYS = {
    "password",
    "pin",
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


def substitute(value: Any, token: str, replacement: str) -> Any:
    """Replace every string equal to ``token`` with ``replacement``."""
    if isinstance(value, str):
        return replacement if value == token else value
    if isinstance(value, dict):
        return {k: substitute(v, token, replacement) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, token, replacement) for v in value]
    return value


def scrub(text: str, code: str) -> str:
    """Hide the code wherever it could be printed or written to disk."""
    return text.replace(code, CODE_PLACEHOLDER) if code else text


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
        transport.login(args.user, password, args.client_type)
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
    ask_code: Callable[[str], str] = getpass.getpass,
    factory=DHIPTransport,
) -> dict[str, Any]:
    check_method(args.method)
    code = ""
    if args.prompt_code:
        code = ask_code("Alarm-user code (hidden; never printed or stored): ").strip()
        if not code:
            raise GuardError("no code entered; nothing was sent")

    def say(message: str) -> None:
        emit(scrub(message, code))

    def build(use: str) -> Any:
        """The params with ``use`` standing in for the code."""
        if args.shape in CODE_SHAPES:
            return CODE_SHAPES[args.shape](args.area, use, args.user_number)
        if args.shape:
            return SHAPES[args.shape](args.area)
        try:
            return substitute(json.loads(args.params_json), CODE_TOKEN, use)
        except json.JSONDecodeError as exc:
            raise GuardError(f"--params-json is not valid JSON: {exc}") from exc

    params = build(code or CODE_PLACEHOLDER)
    shown = build(CODE_PLACEHOLDER)  # what may be printed and stored

    control = factory(args.host, args.port, timeout=12)
    events = None
    transcript: dict[str, Any] = {
        "started": now(),
        "host_redacted": True,
        "method": args.method,
        "params": redact(shown),
        "allow_arm": args.allow_arm,
    }
    try:
        control.connect()
        control.login(args.user, password, args.client_type)
        before = read_modes(control)
        transcript["modes_before"] = before
        say(f"AreaArmMode before: {before}")
        if not args.allow_arm:
            check_disarm_only(shown, before)
        request = {"method": args.method, "params": shown}
        say("About to send exactly ONE request:\n" + json.dumps(request, indent=2))
        if not args.yes and ask("Type SEND to continue: ").strip() != "SEND":
            raise GuardError("not confirmed; nothing was sent")

        events = attach_events(args, password, factory)
        transcript["sent_at"] = now()
        try:
            reply = control.call(args.method, params, fragmented=True)
            transcript["reply"] = redact(reply)
            say("Reply: " + json.dumps(redact(reply)))
        except Exception as exc:  # report, never retry
            transcript["send_error"] = f"{type(exc).__name__}: {exc}"
            say(f"Send failed ({transcript['send_error']}); NOT retrying.")
        say(f"Listening for events for {args.listen:.0f}s ...")
        transcript["events"] = [redact(e) for e in listen(events, args.listen, say)]
        after = read_modes(control)
        transcript["modes_after"] = after
        transcript["modes_changed"] = after != before
        say(f"AreaArmMode after: {after} (changed: {after != before})")
        # Last line of defence: the code never reaches a transcript.
        return json.loads(scrub(json.dumps(transcript), code))
    finally:
        for transport in (events, control):
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()


def probe_client_types(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    factory=DHIPTransport,
) -> list[dict[str, Any]]:
    """Log in under each client type and call the read-only getters once each.

    Nothing here can change the panel: every method is a ``get*`` from
    CLIENT_TYPE_GETTERS. A login failure that could count towards an account
    lockout stops the whole probe immediately.
    """
    for method, _params in CLIENT_TYPE_GETTERS:
        check_method(method)
        if not method.rpartition(".")[2].startswith("get"):
            raise GuardError(f"{method} is not a getter")
    results: list[dict[str, Any]] = []
    for client_type in args.client_types or CLIENT_TYPES:
        entry: dict[str, Any] = {"client_type": client_type, "calls": []}
        results.append(entry)
        transport = factory(args.host, args.port, timeout=12)
        try:
            transport.connect()
            try:
                transport.login(args.user, password, client_type)
            except LoginError as exc:
                entry["login"] = f"failed (code {exc.code})"
                emit(f"{client_type:16} login failed (code {exc.code})")
                if exc.code in LOCKOUT_RISK_CODES:
                    emit("Stopping: this failure can count towards a lockout.")
                    break
                continue
            entry["login"] = "ok"
            emit(f"{client_type:16} login ok")
            for method, params in CLIENT_TYPE_GETTERS:
                reply = transport.call(method, params, fragmented=True)
                ok = bool(reply.get("result"))
                call = {
                    "method": method,
                    "params": params,
                    "ok": ok,
                    "error_code": None
                    if ok
                    else (reply.get("error") or {}).get("code"),
                    # key names only: no reply value is stored
                    "reply_keys": sorted(reply["params"])
                    if ok and isinstance(reply.get("params"), dict)
                    else [],
                }
                entry["calls"].append(call)
                outcome = "OK" if ok else f"error {call['error_code']}"
                emit(f"    {method} {json.dumps(params)} -> {outcome}")
        except (DHIPError, OSError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            emit(f"{client_type:16} {entry['error']}")
        finally:
            with contextlib.suppress(Exception):
                transport.close()
    return results


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    if "--list-shapes" in (sys.argv[1:] if argv is None else argv):
        for name, build in SHAPES.items():
            print(f"{name:24} {json.dumps(build(0))}")
        for name, build_code in CODE_SHAPES.items():
            print(f"{name:24} {json.dumps(build_code(0, CODE_PLACEHOLDER, 1))}")
        print("(shapes with a code need --prompt-code)")
        raise SystemExit(0)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=const.DEFAULT_PORT)
    parser.add_argument("--user", required=True)
    parser.add_argument("--method", default=DEFAULT_METHOD)
    params = parser.add_mutually_exclusive_group(required=False)
    params.add_argument("--params-json", help="request params as JSON")
    params.add_argument(
        "--shape",
        choices=sorted({*SHAPES, *CODE_SHAPES}),
        help="a built-in disarm-only guess (see --list-shapes); no quoting needed",
    )
    parser.add_argument(
        "--area", type=int, default=0, help="zero-based area for --shape (default 0)"
    )
    parser.add_argument(
        "--client-type",
        default=DEFAULT_CLIENT_TYPE,
        help="DHIP login clientType for the trial (default Web3.0)",
    )
    parser.add_argument(
        "--probe-client-types",
        action="store_true",
        help="read-only: log in under each client type and call the failing "
        "getters once each; nothing is armed or changed",
    )
    parser.add_argument(
        "--client-types",
        type=lambda text: [part.strip() for part in text.split(",") if part.strip()],
        help="comma-separated clientType values for --probe-client-types "
        f"(default {','.join(CLIENT_TYPES)})",
    )
    parser.add_argument(
        "--prompt-code",
        action="store_true",
        help="ask for an alarm-user code without echo; it is never printed, logged "
        "or stored (needed by the *-password / *-user* shapes, or by a "
        f"{CODE_TOKEN} value in --params-json)",
    )
    parser.add_argument(
        "--user-number",
        type=int,
        default=1,
        help="UserNumber of the alarm user for shapes that include it (default 1)",
    )
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
    if args.probe_client_types:
        if args.shape or args.params_json or args.prompt_code:
            parser.error("--probe-client-types takes no shape, params or code")
        return args
    if not (args.shape or args.params_json):
        parser.error("one of --shape or --params-json is required")
    if not args.i_am_at_the_panel:
        parser.error("refusing to run without --i-am-at-the-panel")
    if args.shape in CODE_SHAPES and not args.prompt_code:
        parser.error(f"shape {args.shape!r} carries a code: add --prompt-code")
    if args.prompt_code and not (
        args.shape in CODE_SHAPES or CODE_TOKEN in (args.params_json or "")
    ):
        parser.error(
            f"--prompt-code needs a code shape or {CODE_TOKEN} in --params-json"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.prompt_code and not sys.stdin.isatty():
        print("--prompt-code needs a terminal: the code is never read from a pipe.")
        return 2
    password = os.environ.get("ARC_PASSWORD") or (
        getpass.getpass("ARC password: ") if sys.stdin.isatty() else ""
    )
    if not password:
        print("Set ARC_PASSWORD (the password is never taken from the command line).")
        return 2
    try:
        if args.probe_client_types:
            transcript = {
                "started": now(),
                "results": probe_client_types(args, password),
            }
        else:
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
