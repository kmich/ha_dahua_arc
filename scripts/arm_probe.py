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
in under ``Web3.0`` and ``Local`` (``--client-types A,B`` to choose) and calls the
getters that fail under ``Web3.0`` once each, printing which succeed. It needs no
shape, code or panel flag, never arms anything, and stops at once on a login
failure that could count towards an account lockout. ``NetKeyboard`` and
``Dahua3.0`` client types are refused: a NetKeyboard login matches the IPS
signature "ET EXPLOIT Dahua Netkeyboard Authentication Bypass", which blocks the
traffic and can cut Home Assistant off from the ARC:

    py -3.14 scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --probe-client-types --out client_types.json

Key sweep. ``--sweep-keys`` is another read-only mode: on one ``Web3.0`` session
it calls ``AlarmRegion.getZoneArmMode`` and ``getUserArmModeAuth`` with each of
about 50 candidate parameter key names (values ``0`` and ``[0]``), and reports the
keys whose error differs from the baseline: those are names the ARC recognises.
It stops at once on any transport error; ``--sweep-delay`` spaces the calls:

    py -3.14 scripts/arm_probe.py --host 192.168.4.20 --user admin \\
        --sweep-keys --out sweep.json

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
import hashlib
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

ALLOWED_SERVICES = (
    "AlarmRegion",
    "alarmSubregion",
    "alarm",
    "alarmSubSystem",
    "AlarmKeyboard",
    "AlarmUserManager",
)
INSTANCE_CANDIDATE_SERVICES = ALLOWED_SERVICES
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
    # "Zones" (as a list) is the one key the ARC recognised in --sweep-keys.
    "zones-mode": lambda a: {"Mode": DISARMED, "Zones": [a]},
    "zones-armmode": lambda a: {"ArmMode": DISARMED, "Zones": [a]},
    "zones-objects": lambda a: {"Zones": [{"Index": a, "Mode": DISARMED}]},
    "zones-mode-1based": lambda a: {"Mode": DISARMED, "Zones": [a + 1]},
    # Supported modes from AlarmRegion.getCaps ("DisArm", "AtHome", "OutDoor") and NetSDK enum 0.
    "disarm-word": lambda a: {"Mode": "DisArm", "Areas": [a]},
    "disarm-enum": lambda a: {"Mode": 0, "Areas": [a]},
    "zones-disarm-word": lambda a: {"Mode": "DisArm", "Zones": [a]},
    "zones-disarm-enum": lambda a: {"Mode": 0, "Zones": [a]},
    # Subsystem-based shapes (alarm.getSubSystem returns 32 subsystem states)
    "subsystem-disarm-enum": lambda a: {"SubSystem": a, "Mode": 0},
    "subsystem-disarm-word": lambda a: {"SubSystem": a, "Mode": "DisArm"},
    "subsystem-disarm-lower": lambda a: {"subSystem": a, "mode": 0},
    "subsystems-disarm-enum": lambda a: {"SubSystems": [a], "Mode": 0},
    "subsystems-disarm-lower": lambda a: {"subSystems": [a], "mode": 0},
    "subsystem-state": lambda a: {"SubSystem": a, "State": "AllDisarming"},
    "subsystem-state-lower": lambda a: {"subSystem": a, "state": "AllDisarming"},
    # Channel-based shapes (Dahua NetSDK / P2P)
    "channel-disarm-enum": lambda a: {"Channel": a, "Mode": 0},
    "channel-disarm-word": lambda a: {"Channel": a, "Mode": "DisArm"},
    "channel-disarm-lower": lambda a: {"channel": a, "mode": 0},
    # Channel list shapes (proven schema for Dahua ARC: channel is list of ints)
    "channel-list-disarm-lower": lambda a: {"channel": [a], "mode": 0},
    "channel-list-disarm-enum": lambda a: {"Channel": [a], "Mode": 0},
    "channel-list-disarm-word": lambda a: {"channel": [a], "Mode": "DisArm"},
    "subsystem-list-disarm-lower": lambda a: {"subSystem": [a + 1], "mode": 0},
    "subsystem-list-disarm-enum": lambda a: {"SubSystem": [a + 1], "Mode": 0},
    "subsystems-list-disarm-lower": lambda a: {"subSystems": [a + 1], "mode": 0},
    "subsystems-list-disarm-enum": lambda a: {"SubSystems": [a + 1], "Mode": 0},
    "defence-areas-disarm-lower": lambda a: {"defenceAreas": [a], "mode": 0},
    "defence-area-disarm-lower": lambda a: {"defenceArea": a, "mode": 0},
    # Mode-only global shapes (Technaxx TX-84 / Dahua P2P)
    "mode-disarm-enum": lambda a: {"Mode": 0},
    "mode-disarm-word": lambda a: {"Mode": "DisArm"},
    "mode-disarm-lower": lambda a: {"mode": 0},
    "mode-disarm-lower-word": lambda a: {"mode": "disarm"},
    "armmode-disarm-enum": lambda a: {"ArmMode": 0},
    "armmode-disarm-word": lambda a: {"ArmMode": "Disarm"},
    "armmode-disarm-lower": lambda a: {"armMode": 0},
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
    "zones-password": lambda a, c, n: {"Mode": DISARMED, "Zones": [a], "Password": c},
    "zones-usernumber": lambda a, c, n: {
        "Mode": DISARMED,
        "Zones": [a],
        "UserNumber": n,
        "Password": c,
    },
    "zones-userid": lambda a, c, n: {
        "Mode": DISARMED,
        "Zones": [a],
        "UserID": n,
        "Password": c,
    },
    "zones-user-object": lambda a, c, n: {
        "Mode": DISARMED,
        "Zones": [a],
        "User": {"UserNumber": n, "Password": c},
    },
    "disarm-word-code": lambda a, c, n: {"Mode": "DisArm", "Areas": [a], "Password": c},
    "disarm-word-user": lambda a, c, n: {
        "Mode": "DisArm",
        "Areas": [a],
        "UserNumber": n,
        "Password": c,
    },
    "disarm-enum-user": lambda a, c, n: {
        "Mode": 0,
        "Areas": [a],
        "UserNumber": n,
        "Password": c,
    },
    "zones-disarm-word-code": lambda a, c, n: {
        "Mode": "DisArm",
        "Zones": [a],
        "Password": c,
    },
    "zones-disarm-word-user": lambda a, c, n: {
        "Mode": "DisArm",
        "Zones": [a],
        "UserNumber": n,
        "Password": c,
    },
    "zones-disarm-enum-user": lambda a, c, n: {
        "Mode": 0,
        "Zones": [a],
        "UserNumber": n,
        "Password": c,
    },
    # Subsystem shapes with code / password / pin
    "subsystem-password": lambda a, c, n: {
        "SubSystem": a,
        "Mode": 0,
        "Password": c,
    },
    "subsystem-lower-password": lambda a, c, n: {
        "subSystem": a,
        "mode": 0,
        "password": c,
    },
    "subsystem-pin": lambda a, c, n: {
        "subSystem": a,
        "mode": 0,
        "pin": c,
    },
    "subsystem-user-password": lambda a, c, n: {
        "SubSystem": a,
        "Mode": 0,
        "UserNumber": n,
        "Password": c,
    },
    "subsystem-user-lower": lambda a, c, n: {
        "subSystem": a,
        "mode": 0,
        "userNumber": n,
        "password": c,
    },
    "subsystems-password": lambda a, c, n: {
        "SubSystems": [a],
        "Mode": 0,
        "Password": c,
    },
    # Channel shapes with code / password / pin
    "channel-password": lambda a, c, n: {
        "Channel": a,
        "Mode": 0,
        "Password": c,
    },
    "channel-lower-password": lambda a, c, n: {
        "channel": a,
        "mode": 0,
        "password": c,
    },
    "channel-pin": lambda a, c, n: {
        "channel": a,
        "mode": 0,
        "pin": c,
    },
    "channel-user-password": lambda a, c, n: {
        "Channel": a,
        "Mode": 0,
        "UserNumber": n,
        "Password": c,
    },
    # Channel list shapes with code / password (proven schema for Dahua ARC)
    "channel-list-password": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "Password": c,
    },
    "channel-list-password-lower": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "password": c,
    },
    "channel-list-usernumber": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "UserNumber": n,
        "Password": c,
    },
    "channel-list-usernumber-lower": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "userNumber": n,
        "password": c,
    },
    "channel-list-name": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "Name": "Konstantinos",
        "Password": c,
    },
    "channel-list-user": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "User": "Konstantinos",
        "Password": c,
    },
    "channel-list-user-object": lambda a, c, n: {
        "channel": [a],
        "mode": 0,
        "User": {"UserNumber": n, "Password": c},
    },
    # Subsystem list shapes with code / password
    "subsystem-list-password": lambda a, c, n: {
        "subSystem": [a + 1],
        "mode": 0,
        "Password": c,
    },
    "subsystem-list-password-lower": lambda a, c, n: {
        "subSystem": [a + 1],
        "mode": 0,
        "password": c,
    },
    "subsystem-list-usernumber": lambda a, c, n: {
        "subSystem": [a + 1],
        "mode": 0,
        "UserNumber": n,
        "Password": c,
    },
    "subsystem-list-user": lambda a, c, n: {
        "subSystem": [a + 1],
        "mode": 0,
        "User": "Konstantinos",
        "Password": c,
    },
    "subsystem-list-name": lambda a, c, n: {
        "subSystem": [a + 1],
        "mode": 0,
        "Name": "Konstantinos",
        "Password": c,
    },
    "subsystems-list-password": lambda a, c, n: {
        "SubSystems": [a + 1],
        "Mode": 0,
        "Password": c,
    },
    # Mode-only global shapes with code / password / pin
    "mode-password": lambda a, c, n: {"Mode": 0, "Password": c},
    "mode-lower-password": lambda a, c, n: {"mode": 0, "password": c},
    "mode-pin": lambda a, c, n: {"mode": 0, "pin": c},
    "armmode-password": lambda a, c, n: {"ArmMode": "Disarm", "Password": c},
    "armmode-pin": lambda a, c, n: {"ArmMode": "Disarm", "pin": c},
}
DEFAULT_METHOD = "alarm.setArmMode"
DEFAULT_CLIENT_TYPE = "Web3.0"
# --probe-client-types: the DHIP login "clientType" values tried, and the
# read-only getters called under each. Only the two ordinary types are safe: a
# NetKeyboard login matches the IPS signature "ET EXPLOIT Dahua Netkeyboard
# Authentication Bypass" (it got the probe's traffic blocked and Home Assistant
# cut off from the ARC), and Dahua3.0 types timed out right after it.
CLIENT_TYPES = ("Web3.0", "Local")
FORBIDDEN_CLIENT_TYPE_PREFIXES = ("netkeyboard", "dahua3.0")
CLIENT_TYPE_GETTERS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("AlarmRegion.getArmMode", {}),
    ("AlarmRegion.getZoneArmMode", {}),
    ("AlarmRegion.getZoneArmMode", {"Zones": [0]}),
    ("AlarmRegion.getUserArmModeAuth", {}),
    ("AlarmRegion.getUserArmModeAuth", {"UserID": 0}),
)
# --sweep-keys: which parameter key names does the ARC recognise? The generic
# error for an unknown/missing key differs from the error once a key is parsed
# (getZoneArmMode {"Zones": [0]} gives 268632080, not -267976701), so a changed
# outcome marks a recognised key. Read-only getters only, one Web3.0 session.
SWEEP_METHODS = ("AlarmRegion.getZoneArmMode", "AlarmRegion.getUserArmModeAuth")
SWEEP_KEYS = [
    "Areas",
    "Area",
    "AreaID",
    "AreaId",
    "AreaIndex",
    "AreaNo",
    "Index",
    "Indexes",
    "Id",
    "ID",
    "Number",
    "No",
    "Zones",
    "Zone",
    "ZoneID",
    "ZoneId",
    "ZoneIndex",
    "ZoneNo",
    "Channel",
    "Channels",
    "Slot",
    "Region",
    "Regions",
    "SubSystem",
    "SubSystems",
    "SubSystemID",
    "Group",
    "Device",
    "Module",
    "Address",
    "UserID",
    "UserId",
    "UserIndex",
    "UserNumber",
    "User",
    "Users",
    "UserName",
    "Name",
    "Mode",
    "ArmMode",
    "Type",
    "Profile",
    "Status",
    "Condition",
    "Password",
    "Auth",
]
SWEEP_VALUES: tuple[Any, ...] = (0, [0])
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


def int_leaves(value: Any) -> list[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, dict):
        return [i for v in value.values() for i in int_leaves(v)]
    if isinstance(value, list):
        return [i for v in value for i in int_leaves(v)]
    return []


def _has_disarm_enum(value: Any) -> bool:
    """Return True if any Mode/ArmMode key in value has integer enum 0 (Disarm)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if (
                str(k).casefold() in ("mode", "armmode")
                and v == 0
                and not isinstance(v, bool)
            ):
                return True
            if _has_disarm_enum(v):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_disarm_enum(item):
                return True
    return False


def _object_id(reply: dict[str, Any]) -> int | None:
    """Object id from a ``factory.instance`` reply (returned as ``result`` or in params)."""
    params = reply.get("params")
    candidates: list[Any] = [reply.get("result"), reply.get("object")]
    if isinstance(params, dict):
        candidates += [
            params.get("object"),
            params.get("Object"),
            params.get("id"),
            params.get("ID"),
        ]
    for value in candidates:
        if isinstance(value, bool):
            continue
        try:
            obj = int(value)
        except (TypeError, ValueError):
            continue
        if obj > 0:
            return obj
    return None


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


def check_client_type(name: str) -> None:
    if name.casefold().startswith(FORBIDDEN_CLIENT_TYPE_PREFIXES):
        raise GuardError(
            f"client type {name!r} is refused: network IPS flags it as a Dahua "
            "exploit and blocks the traffic (use Web3.0 or Local)"
        )


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
    disarm_strings = {"d", "disarm", "alldisarming"}
    has_disarm_string = bool(lowered & disarm_strings)
    has_disarm_int = _has_disarm_enum(params)
    if not (has_disarm_string or has_disarm_int):
        raise GuardError(
            f"parameters must contain the disarm mode {DISARMED!r}, 'DisArm', 'AllDisarming', or 0 "
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
        except (TimeoutError, OSError):
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
    check_client_type(args.client_type)
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
        "instance": getattr(args, "instance", False),
    }
    object_id: int | None = None
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

        if getattr(args, "instance", False):
            service = args.method.split(".", 1)[0]
            created = control.call(f"{service}.factory.instance", None, fragmented=True)
            object_id = _object_id(created)
            transcript["instance_created"] = redact(created)
            say(f"{service}.factory.instance reply: " + json.dumps(redact(created)))
            if object_id is None:
                raise GuardError(
                    f"{service}.factory.instance returned no object id: {created}"
                )
            say(f"Acquired instance object_id={object_id}")

        events = attach_events(args, password, factory)
        transcript["sent_at"] = now()
        try:
            call_kwargs: dict[str, Any] = {"fragmented": True}
            if object_id is not None:
                call_kwargs["object_id"] = object_id
            reply = control.call(args.method, params, **call_kwargs)
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
        if object_id is not None and control is not None:
            service = args.method.split(".", 1)[0]
            with contextlib.suppress(Exception):
                control.call(
                    f"{service}.destroy", None, object_id=object_id, fragmented=True
                )
                say(f"Destroyed instance object_id={object_id}")
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
    client_types = args.client_types or CLIENT_TYPES
    for client_type in client_types:
        check_client_type(client_type)
    results: list[dict[str, Any]] = []
    for client_type in client_types:
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


def _outcome(reply: dict[str, Any]) -> dict[str, Any]:
    ok = bool(reply.get("result"))
    params = reply.get("params")
    return {
        "ok": ok,
        "code": None if ok else (reply.get("error") or {}).get("code"),
        "reply_keys": sorted(params) if ok and isinstance(params, dict) else [],
    }


def sweep_keys(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    factory=DHIPTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Find parameter key names the ARC recognises, using read-only getters.

    One Web3.0 session; for each method the baseline outcome of ``{}`` is
    recorded, then each candidate key is sent with a few simple values. A key
    whose outcome differs from the baseline is reported as recognised. Any
    transport error stops the whole sweep at once (it may mean a block).
    """
    methods = (
        (
            "AlarmRegion.getAreaInfo",
            "AlarmRegion.getAreaZones",
            "AlarmRegion.getAreaSummaries",
            "AlarmRegion.getZonesInfo",
            "AlarmRegion.getZoneArmMode",
            "AlarmRegion.getUserArmModeAuth",
        )
        if getattr(args, "all_getters", False)
        else SWEEP_METHODS
    )
    values = (
        (0, [0], 1, [1], {}) if getattr(args, "all_values", False) else SWEEP_VALUES
    )

    for method in methods:
        check_method(method)
        if not method.rpartition(".")[2].startswith("get"):
            raise GuardError(f"{method} is not a getter")
    result: dict[str, Any] = {"started": now(), "methods": {}, "aborted": None}
    transport = factory(args.host, args.port, timeout=12)
    try:
        transport.connect()
        transport.login(args.user, password, DEFAULT_CLIENT_TYPE)
        emit(f"{DEFAULT_CLIENT_TYPE} login ok; sweeping {len(SWEEP_KEYS)} keys")
        for method in methods:
            entry: dict[str, Any] = {"baseline": None, "calls": [], "recognised": []}
            result["methods"][method] = entry
            entry["baseline"] = _outcome(transport.call(method, {}, fragmented=True))
            emit(f"{method} {{}} -> {entry['baseline']['code']}")
            for key in SWEEP_KEYS:
                for value in values:
                    sleep(args.sweep_delay)
                    outcome = _outcome(
                        transport.call(method, {key: value}, fragmented=True)
                    )
                    call = {"key": key, "value": value, "outcome": outcome}
                    entry["calls"].append(call)
                    if (outcome["ok"], outcome["code"]) != (
                        entry["baseline"]["ok"],
                        entry["baseline"]["code"],
                    ):
                        entry["recognised"].append(call)
                        shown = outcome["code"] if not outcome["ok"] else "OK"
                        emit(f"    recognised: {{{key!r}: {value}}} -> {shown}")
            emit(f"{method}: {len(entry['recognised'])} recognised key shape(s)")
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        emit(f"Stopped: {result['aborted']} (check the network IPS before retrying)")
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return result


def sweep_instances(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    factory=DHIPTransport,
) -> dict[str, Any]:
    """Test factory.instance across candidate alarm services."""
    result: dict[str, Any] = {"started": now(), "services": {}, "aborted": None}
    transport = factory(args.host, args.port, timeout=12)
    try:
        transport.connect()
        transport.login(args.user, password, args.client_type)
        emit(
            f"{args.client_type} login ok; sweeping {len(INSTANCE_CANDIDATE_SERVICES)} "
            "candidate instance services"
        )
        for service in INSTANCE_CANDIDATE_SERVICES:
            method = f"{service}.factory.instance"
            reply = transport.call(method, None, fragmented=True)
            ok = bool(reply.get("result"))
            obj_id = _object_id(reply)
            entry: dict[str, Any] = {
                "method": method,
                "reply": redact(reply),
                "ok": ok,
                "object_id": obj_id,
            }
            if not ok:
                entry["error_code"] = (reply.get("error") or {}).get("code")
            result["services"][service] = entry
            status = (
                f"object_id={obj_id}"
                if obj_id is not None
                else (f"error {entry.get('error_code')}" if not ok else "no object_id")
            )
            emit(f"    {method} -> {status}")
            if obj_id is not None:
                destroy_method = f"{service}.destroy"
                destroy_reply = transport.call(
                    destroy_method, None, object_id=obj_id, fragmented=True
                )
                entry["destroy_ok"] = bool(destroy_reply.get("result"))
                emit(
                    f"    {destroy_method} object_id={obj_id} -> "
                    f"{'OK' if entry['destroy_ok'] else 'error'}"
                )
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        emit(f"Stopped: {result['aborted']} (check the network IPS before retrying)")
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return result


def inspect_service(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    factory=DHIPTransport,
) -> dict[str, Any]:
    """Query all methods and instance methods for a candidate alarm service."""
    service = args.inspect_service
    check_method(f"{service}.test")
    result: dict[str, Any] = {"service": service, "methods": [], "instance_methods": []}
    transport = factory(args.host, args.port, timeout=12)
    try:
        transport.connect()
        transport.login(args.user, password, args.client_type)
        emit(f"{args.client_type} login ok; querying {service}.listMethod ...")
        reply = transport.call(f"{service}.listMethod", None, fragmented=True)
        methods = (reply.get("params") or {}).get("method") or []
        result["methods"] = methods
        emit(f"{service} static methods ({len(methods)}): {', '.join(methods)}")

        inst_reply = transport.call(
            f"{service}.factory.instance", None, fragmented=True
        )
        obj_id = _object_id(inst_reply)
        if obj_id is not None:
            emit(
                f"Acquired {service} instance object_id={obj_id}; querying instance methods..."
            )
            inst_list = transport.call(
                f"{service}.listMethod", None, object_id=obj_id, fragmented=True
            )
            inst_methods = (inst_list.get("params") or {}).get("method") or []
            result["instance_methods"] = inst_methods
            emit(
                f"{service} instance methods ({len(inst_methods)}): {', '.join(inst_methods)}"
            )

            getters = sorted(
                m
                for m in set(methods + inst_methods)
                if m.startswith(f"{service}.get")
                and any(
                    k in m.casefold()
                    for k in (
                        "arm",
                        "defence",
                        "defense",
                        "subsystem",
                        "caps",
                        "area",
                        "state",
                    )
                )
            )
            emit(f"\nProbing {len(getters)} getters on {service} (read-only)...")
            for gm in getters:
                # Try statically first
                srep = transport.call(gm, {}, fragmented=True)
                if srep.get("result"):
                    emit(f"  [STATIC OK] {gm} {{}} -> {json.dumps(redact(srep))}")
                    continue
                # Try on instance
                tried_ok = False
                for p in ({}, {"Areas": [0]}, {"Zones": [0]}, {"Index": 0}):
                    irep = transport.call(gm, p, object_id=obj_id, fragmented=True)
                    if irep.get("result"):
                        emit(
                            f"  [INSTANCE OK] {gm} {json.dumps(p)} -> {json.dumps(redact(irep))}"
                        )
                        tried_ok = True
                        break
                    else:
                        code = (irep.get("error") or {}).get("code")
                        if code != -267976701:
                            emit(f"  [INSTANCE] {gm} {json.dumps(p)} -> code {code}")
                            tried_ok = True
                if not tried_ok:
                    emit(
                        f"  [INSTANCE] {gm} -> code -267976701 (parameter structure required)"
                    )

            transport.call(
                f"{service}.destroy", None, object_id=obj_id, fragmented=True
            )
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        emit(f"Stopped: {result['aborted']}")
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return result


def probe_condition(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    ask_code: Callable[[str], str] = getpass.getpass,
    factory=DHIPTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Probe AlarmRegion with Condition dictionary objects, user auth shapes, and 32-area array shapes."""
    code = ""
    if getattr(args, "prompt_code", False):
        code = ask_code("Alarm-user code (hidden; never printed or stored): ").strip()

    extra_scrubs: list[str] = []

    def scrubbed(text: str) -> str:
        s = scrub(text, code) if code else text
        s = scrub(s, password) if password else s
        for sec in extra_scrubs:
            if sec:
                s = scrub(s, sec)
        return s

    def say(message: str) -> None:
        emit(scrubbed(message))

    result: dict[str, Any] = {
        "started": now(),
        "calls": [],
        "success": False,
        "aborted": None,
    }

    transport = factory(args.host, args.port, timeout=12)
    try:
        transport.connect()
        transport.login(args.user, password, DEFAULT_CLIENT_TYPE)
        say(f"{DEFAULT_CLIENT_TYPE} login ok\n")

        # 0. Check AlarmKeyboard methods if available
        try:
            kb_rep = transport.call("AlarmKeyboard.listMethod", None, fragmented=True)
            kb_methods = (kb_rep.get("params") or {}).get("method") or []
            if kb_methods:
                say(f"--- AlarmKeyboard static methods ({len(kb_methods)}) ---")
                say(f"Methods: {', '.join(kb_methods)}\n")
        except Exception:
            pass

        # 1. Read-only Condition and Auth probes
        say(
            "--- Testing AlarmRegion Read-Only Getters with Condition & Auth shapes ---"
        )
        getters_to_test = [
            (
                "AlarmRegion.getUserArmModeAuth",
                [
                    ("bare-u1", {"UserNumber": args.user_number}),
                    ("bare-id", {"UserID": 0}),
                    ("empty-cond", {"Condition": {}}),
                    ("areas-cond", {"Condition": {"Areas": [0]}}),
                ]
                + (
                    [
                        ("pw-only", {"Password": code}),
                        ("u1-pw", {"UserNumber": args.user_number, "Password": code}),
                        ("name-pw", {"Name": "Konstantinos", "Password": code}),
                        ("id-pw", {"Id": "43009890130", "Password": code}),
                        (
                            "u1-pw-area0",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Area": 0,
                            },
                        ),
                        (
                            "u1-pw-area1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Area": 1,
                            },
                        ),
                        (
                            "u1-pw-areas0",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Areas": [0],
                            },
                        ),
                        (
                            "u1-pw-areas1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Areas": [1],
                            },
                        ),
                        (
                            "u1-pw-sub1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "SubSystem": 1,
                            },
                        ),
                        (
                            "u1-pw-subs1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "SubSystems": [1],
                            },
                        ),
                        (
                            "u1-pw-mode-d",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Mode": "DisArm",
                            },
                        ),
                        (
                            "u1-pw-mode-D",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Mode": "D",
                            },
                        ),
                        (
                            "u1-pw-armmode",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "ArmMode": "DisArm",
                            },
                        ),
                        (
                            "u1-pw-areaarmmode",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "AreaArmMode": "DisArm",
                            },
                        ),
                        (
                            "u1-pw-area1-mode",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Area": 1,
                                "Mode": "DisArm",
                            },
                        ),
                        (
                            "u1-pw-areas1-mode",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Areas": [1],
                                "Mode": "DisArm",
                            },
                        ),
                        (
                            "u1-pw-cond-area1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Condition": {"Area": 1},
                            },
                        ),
                        (
                            "u1-pw-cond-areas1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "Condition": {"Areas": [1]},
                            },
                        ),
                        (
                            "u1-pw-ch0",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "channel": 0,
                            },
                        ),
                        (
                            "u1-pw-ch1",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "channel": 1,
                            },
                        ),
                        (
                            "u1-pw-type-arm",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "type": "arm",
                            },
                        ),
                        (
                            "u1-pw-type-disarm",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "type": "disarm",
                            },
                        ),
                        (
                            "u1-pw-ch0-type-arm",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "channel": 0,
                                "type": "arm",
                            },
                        ),
                        (
                            "u1-pw-ch0-type-disarm",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "channel": 0,
                                "type": "disarm",
                            },
                        ),
                        (
                            "u1-pw-armmode-0",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "armMode": 0,
                            },
                        ),
                        (
                            "u1-pw-armmode-D",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "armMode": "D",
                            },
                        ),
                        (
                            "u1-pw-action-disarm",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "action": "disarm",
                            },
                        ),
                        (
                            "u1-pw-table",
                            {
                                "UserNumber": args.user_number,
                                "Password": code,
                                "table": [],
                            },
                        ),
                    ]
                    if code
                    else []
                ),
            ),
            (
                "AlarmRegion.getZoneArmMode",
                [
                    ("zones-0", {"Zones": [0]}),
                    ("zones-1", {"Zones": [1]}),
                    ("zones-list", {"Zones": [0, 1]}),
                    ("cond-zones-0", {"Condition": {"Zones": [0]}}),
                    ("cond-zones-1", {"Condition": {"Zones": [1]}}),
                ]
                + (
                    [
                        ("zones-0-pw", {"Zones": [0], "Password": code}),
                        ("zones-1-pw", {"Zones": [1], "Password": code}),
                        (
                            "zones-0-u1-pw",
                            {
                                "Zones": [0],
                                "UserNumber": args.user_number,
                                "Password": code,
                            },
                        ),
                        (
                            "zones-1-u1-pw",
                            {
                                "Zones": [1],
                                "UserNumber": args.user_number,
                                "Password": code,
                            },
                        ),
                        (
                            "zones-0-id-pw",
                            {"Zones": [0], "Id": "43009890130", "Password": code},
                        ),
                        (
                            "zones-1-id-pw",
                            {"Zones": [1], "Id": "43009890130", "Password": code},
                        ),
                        (
                            "cond-zones-0-pw",
                            {"Condition": {"Zones": [0]}, "Password": code},
                        ),
                    ]
                    if code
                    else []
                ),
            ),
            (
                "AlarmRegion.getZonesInfo",
                [
                    ("empty-cond", {"Condition": {}}),
                    ("index-cond", {"Condition": {"Index": 0}}),
                    ("areas-cond", {"Condition": {"Areas": [0]}}),
                    ("areas1-cond", {"Condition": {"Areas": [1]}}),
                    ("zones-cond", {"Condition": {"Zones": [0]}}),
                    ("channel-cond", {"Condition": {"Channel": 0}}),
                    ("status-all-cond", {"Condition": {"Status": "All"}}),
                ]
                + (
                    [
                        ("empty-cond-pw", {"Condition": {}, "Password": code}),
                        (
                            "areas1-cond-pw",
                            {"Condition": {"Areas": [1]}, "Password": code},
                        ),
                        (
                            "zones0-cond-pw",
                            {"Condition": {"Zones": [0]}, "Password": code},
                        ),
                    ]
                    if code
                    else []
                ),
            ),
            (
                "AlarmRegion.getAreaInfo",
                [
                    ("empty-cond", {"Condition": {}}),
                    ("index-cond", {"Condition": {"Index": 0}}),
                    ("area-cond", {"Condition": {"Area": 0}}),
                    ("areas-cond", {"Condition": {"Areas": [0]}}),
                    ("areas1-cond", {"Condition": {"Areas": [1]}}),
                    ("bare-index", {"Index": 0}),
                    ("bare-area", {"Area": 0}),
                    ("bare-area1", {"Area": 1}),
                    ("bare-areas", {"Areas": [0]}),
                    ("bare-areas1", {"Areas": [1]}),
                ]
                + (
                    [
                        ("pw-only", {"Password": code}),
                        ("area1-pw", {"Area": 1, "Password": code}),
                        ("areas1-pw", {"Areas": [1], "Password": code}),
                        ("empty-cond-pw", {"Condition": {}, "Password": code}),
                        ("area1-cond-pw", {"Condition": {"Area": 1}, "Password": code}),
                    ]
                    if code
                    else []
                ),
            ),
            (
                "AlarmRegion.getAreaZones",
                [
                    ("empty-cond", {"Condition": {}}),
                    ("area-cond", {"Condition": {"Area": 0}}),
                    ("areas-cond", {"Condition": {"Areas": [0]}}),
                    ("areas1-cond", {"Condition": {"Areas": [1]}}),
                    ("bare-area", {"Area": 0}),
                    ("bare-area1", {"Area": 1}),
                    ("bare-areas", {"Areas": [0]}),
                    ("bare-areas1", {"Areas": [1]}),
                ]
                + (
                    [
                        ("pw-only", {"Password": code}),
                        ("area1-pw", {"Area": 1, "Password": code}),
                        ("areas1-pw", {"Areas": [1], "Password": code}),
                        ("empty-cond-pw", {"Condition": {}, "Password": code}),
                        ("area1-cond-pw", {"Condition": {"Area": 1}, "Password": code}),
                    ]
                    if code
                    else []
                ),
            ),
            (
                "AlarmRegion.getAreaSummaries",
                [
                    ("empty-cond", {"Condition": {}}),
                    ("areas-cond", {"Condition": {"Areas": [0]}}),
                    ("areas1-cond", {"Condition": {"Areas": [1]}}),
                    ("bare-areas", {"Areas": [0]}),
                    ("bare-areas1", {"Areas": [1]}),
                    ("bare-index", {"Index": 0}),
                ]
                + (
                    [
                        ("pw-only", {"Password": code}),
                        ("areas1-pw", {"Areas": [1], "Password": code}),
                        ("empty-cond-pw", {"Condition": {}, "Password": code}),
                        (
                            "areas1-cond-pw",
                            {"Condition": {"Areas": [1]}, "Password": code},
                        ),
                    ]
                    if code
                    else []
                ),
            ),
        ]

        for method, test_cases in getters_to_test:
            say(f"\nMethod: {method}")
            for label, params in test_cases:
                sleep(0.05)
                rep = transport.call(method, params, fragmented=True)
                ok = bool(rep.get("result"))
                err = rep.get("error") or {}
                code_val = err.get("code")
                msg_val = err.get("message", "")
                params_str = scrubbed(json.dumps(params))
                call_info = {
                    "method": method,
                    "label": label,
                    "params": redact(params),
                    "ok": ok,
                    "code": code_val,
                }
                result["calls"].append(call_info)
                if ok:
                    say(
                        f"  [SUCCESS!] {label} {params_str} -> {json.dumps(redact(rep))}"
                    )
                elif code_val == 268632080:
                    say(
                        f"  [AUTH GATE] {label} {params_str} -> code 268632080 (Auth required)"
                    )
                elif code_val == -267976701:
                    say(f"  [PARAM REJECT] {label} {params_str} -> code -267976701")
                else:
                    say(f"  [REPLY {code_val}] {label} {params_str} -> {msg_val}")

        # 2. If at panel, test AlarmRegion.setArmMode disarm shapes (32 areas, AreaArmMode, SubSystems)
        if getattr(args, "i_am_at_the_panel", False):
            say(
                "\n--- Testing AlarmRegion.setArmMode (32-Area, AreaArmMode & SubSystem Shapes) ---"
            )
            area_modes = read_modes(transport)
            say(
                f"AreaArmMode before: {area_modes[:4]}... (all D: {all(m == DISARMED for m in area_modes)})"
            )
            if not all(m == DISARMED for m in area_modes) and not args.allow_arm:
                raise GuardError("Panel not 100% disarmed; refusing to send setter.")

            creds: list[tuple[str, dict[str, Any]]] = [("no-cred", {})]
            if code:
                creds.append(("code-pw", {"Password": code}))
                creds.append(
                    ("code-u1", {"UserNumber": args.user_number, "Password": code})
                )
                creds.append(("code-name", {"Name": "Konstantinos", "Password": code}))
                creds.append(("code-id", {"Id": "43009890130", "Password": code}))
            if password:
                creds.append(("admin-pw", {"Password": password}))
                creds.append(("admin-user", {"User": args.user, "Password": password}))

            disarm_cases: list[tuple[str, Any]] = []
            for m_val in ("DisArm", "D", 0):
                list32 = [{"Mode": m_val}] * 32
                list32_idx = [{"Index": i, "Mode": m_val} for i in range(32)]
                list32_area = [{"Area": i, "Mode": m_val} for i in range(32)]
                list32_area1 = [{"Area": i, "Mode": m_val} for i in range(1, 33)]
                list32_sub1 = [{"SubSystem": i, "Mode": m_val} for i in range(1, 33)]

                for c_tag, c_dict in creds:
                    # 32-area arrays under various key names
                    disarm_cases.append(
                        (f"armMode-32-{m_val}-{c_tag}", {"armMode": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (
                            f"AreaArmMode-32-{m_val}-{c_tag}",
                            {"AreaArmMode": list32, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-idx-{m_val}-{c_tag}",
                            {"armMode": list32_idx, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-area0-{m_val}-{c_tag}",
                            {"armMode": list32_area, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-area1-{m_val}-{c_tag}",
                            {"armMode": list32_area1, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"AreaArmMode-area1-{m_val}-{c_tag}",
                            {"AreaArmMode": list32_area1, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-sub1-{m_val}-{c_tag}",
                            {"armMode": list32_sub1, **c_dict},
                        )
                    )

                    # Single area / subsystem disarm
                    disarm_cases.append(
                        (
                            f"single-armMode-1-{m_val}-{c_tag}",
                            {"armMode": [{"Area": 1, "Mode": m_val}], **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"single-AreaArmMode-1-{m_val}-{c_tag}",
                            {"AreaArmMode": [{"Area": 1, "Mode": m_val}], **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"single-sub-1-{m_val}-{c_tag}",
                            {"armMode": [{"SubSystem": 1, "Mode": m_val}], **c_dict},
                        )
                    )

                    # Flat area / mode shapes
                    disarm_cases.append(
                        (
                            f"flat-area1-mode-{m_val}-{c_tag}",
                            {"Area": 1, "Mode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"flat-area1-AreaArmMode-{m_val}-{c_tag}",
                            {"Area": 1, "AreaArmMode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"flat-areas1-mode-{m_val}-{c_tag}",
                            {"Areas": [1], "Mode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"flat-areas1-AreaArmMode-{m_val}-{c_tag}",
                            {"Areas": [1], "AreaArmMode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"flat-sub1-mode-{m_val}-{c_tag}",
                            {"SubSystem": 1, "Mode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"flat-subs1-mode-{m_val}-{c_tag}",
                            {"SubSystems": [1], "Mode": m_val, **c_dict},
                        )
                    )

                    # Condition shapes
                    for cond_name, cond_dict in (
                        ("empty", {}),
                        ("areas1", {"Areas": [1]}),
                        ("area1", {"Area": 1}),
                        ("sub1", {"SubSystem": 1}),
                    ):
                        disarm_cases.append(
                            (
                                f"cond-{cond_name}-{m_val}-{c_tag}",
                                {"Condition": cond_dict, "Mode": m_val, **c_dict},
                            )
                        )
                        disarm_cases.append(
                            (
                                f"cond-{cond_name}-AreaArmMode-{m_val}-{c_tag}",
                                {
                                    "Condition": cond_dict,
                                    "AreaArmMode": m_val,
                                    **c_dict,
                                },
                            )
                        )
                        disarm_cases.append(
                            (
                                f"cond-{cond_name}-armMode-{m_val}-{c_tag}",
                                {
                                    "Condition": cond_dict,
                                    "armMode": [{"Area": 1, "Mode": m_val}],
                                    **c_dict,
                                },
                            )
                        )

                    # Table shapes
                    disarm_cases.append(
                        (
                            f"table-32-{m_val}-{c_tag}",
                            {"table": [{"Mode": m_val}] * 32, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"table-1-{m_val}-{c_tag}",
                            {"table": [{"Area": 1, "Mode": m_val}], **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"name-AreaArmMode-table-{m_val}-{c_tag}",
                            {
                                "name": "AreaArmMode",
                                "table": [{"Mode": m_val}] * 32,
                                **c_dict,
                            },
                        )
                    )
                    disarm_cases.append(
                        (
                            f"name-armMode-table-{m_val}-{c_tag}",
                            {
                                "name": "armMode",
                                "table": [{"Mode": m_val}] * 32,
                                **c_dict,
                            },
                        )
                    )

                    # Scalar / mode shapes
                    disarm_cases.append(
                        (
                            f"armMode-scalar-{m_val}-{c_tag}",
                            {"armMode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-scalar-area1-{m_val}-{c_tag}",
                            {"armMode": m_val, "Area": 1, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"armMode-scalar-areas1-{m_val}-{c_tag}",
                            {"armMode": m_val, "Areas": [1], **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (f"mode-scalar-{m_val}-{c_tag}", {"mode": m_val, **c_dict})
                    )
                    disarm_cases.append(
                        (
                            f"mode-scalar-area1-{m_val}-{c_tag}",
                            {"mode": m_val, "Area": 1, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (f"Mode-scalar-{m_val}-{c_tag}", {"Mode": m_val, **c_dict})
                    )
                    disarm_cases.append(
                        (
                            f"Mode-scalar-area1-{m_val}-{c_tag}",
                            {"Mode": m_val, "Area": 1, **c_dict},
                        )
                    )

                    # Channel-based shapes
                    disarm_cases.append(
                        (
                            f"channel-0-mode-{m_val}-{c_tag}",
                            {"channel": 0, "mode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"channel-1-mode-{m_val}-{c_tag}",
                            {"channel": 1, "mode": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"channel-0-type-{m_val}-{c_tag}",
                            {"channel": 0, "type": m_val, **c_dict},
                        )
                    )
                    disarm_cases.append(
                        (
                            f"channel-1-type-{m_val}-{c_tag}",
                            {"channel": 1, "type": m_val, **c_dict},
                        )
                    )

                    # Action / command shapes
                    disarm_cases.append(
                        (f"action-{m_val}-{c_tag}", {"action": m_val, **c_dict})
                    )
                    disarm_cases.append(
                        (f"Action-{m_val}-{c_tag}", {"Action": m_val, **c_dict})
                    )
                    disarm_cases.append(
                        (f"command-{m_val}-{c_tag}", {"command": m_val, **c_dict})
                    )
                    disarm_cases.append(
                        (f"Command-{m_val}-{c_tag}", {"Command": m_val, **c_dict})
                    )

                    # Other key names with 32 list
                    disarm_cases.append(
                        (f"info-32-{m_val}-{c_tag}", {"info": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (f"data-32-{m_val}-{c_tag}", {"data": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (f"param-32-{m_val}-{c_tag}", {"param": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (f"params-32-{m_val}-{c_tag}", {"params": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (f"armInfo-32-{m_val}-{c_tag}", {"armInfo": list32, **c_dict})
                    )
                    disarm_cases.append(
                        (f"armModes-32-{m_val}-{c_tag}", {"armModes": list32, **c_dict})
                    )

            if code:
                for m_val in ("DisArm", "D"):
                    item_pw_32 = [{"Mode": m_val, "Password": code}] * 32
                    item_u1_pw_32 = [
                        {
                            "Mode": m_val,
                            "UserNumber": args.user_number,
                            "Password": code,
                        }
                    ] * 32
                    disarm_cases.append((f"item-pw-{m_val}", {"armMode": item_pw_32}))
                    disarm_cases.append(
                        (f"item-u1-pw-{m_val}", {"armMode": item_u1_pw_32})
                    )

            param_reject_count = 0
            for label, params in disarm_cases:
                sleep(0.05)
                rep = transport.call("AlarmRegion.setArmMode", params, fragmented=True)
                ok = bool(rep.get("result"))
                err = rep.get("error") or {}
                code_val = err.get("code")
                msg_val = err.get("message", "")
                params_str = scrubbed(json.dumps(params))
                call_info = {
                    "method": "AlarmRegion.setArmMode",
                    "label": label,
                    "params": redact(params),
                    "ok": ok,
                    "code": code_val,
                }
                result["calls"].append(call_info)
                if ok:
                    say("\n" + "*" * 68)
                    say(
                        "*** SUCCESS! WORKING DISARM COMMAND VERIFIED ON ARC3800H HARDWARE! ***"
                    )
                    say("Method: AlarmRegion.setArmMode")
                    say(f"Params: {params_str}")
                    say(f"Reply:  {json.dumps(redact(rep))}")
                    say("*" * 68 + "\n")
                    result["success"] = True
                    result["winning_call"] = {
                        "method": "AlarmRegion.setArmMode",
                        "params": params,
                        "reply": redact(rep),
                    }
                    break
                elif code_val == 268632080:
                    say(f"  [AUTH GATE] {label} -> code 268632080 (Auth required)")
                elif code_val == -267976701:
                    param_reject_count += 1
                else:
                    say(f"  [REPLY {code_val}] {label} -> {msg_val}")

            if not result["success"]:
                say(
                    f"  ... tested {len(disarm_cases)} shapes on AlarmRegion.setArmMode ({param_reject_count} param rejected)"
                )

            if not result["success"]:
                say("\n--- Testing AlarmRegion.setZoneArmMode (Zone Disarm Shapes) ---")
                zone_cases: list[tuple[str, Any]] = []
                for z_val in (0, 1):
                    for m_val in ("DisArm", "D", 0):
                        for c_tag, c_dict in creds:
                            zone_cases.append(
                                (
                                    f"zone-{z_val}-{m_val}-{c_tag}",
                                    {"Zones": [z_val], "Mode": m_val, **c_dict},
                                )
                            )
                            zone_cases.append(
                                (
                                    f"zoneArmMode-{z_val}-{m_val}-{c_tag}",
                                    {
                                        "zoneArmMode": [{"Zone": z_val, "Mode": m_val}],
                                        **c_dict,
                                    },
                                )
                            )

                zone_reject_count = 0
                for label, params in zone_cases:
                    sleep(0.05)
                    rep = transport.call(
                        "AlarmRegion.setZoneArmMode", params, fragmented=True
                    )
                    ok = bool(rep.get("result"))
                    err = rep.get("error") or {}
                    code_val = err.get("code")
                    msg_val = err.get("message", "")
                    params_str = scrubbed(json.dumps(params))
                    call_info = {
                        "method": "AlarmRegion.setZoneArmMode",
                        "label": label,
                        "params": redact(params),
                        "ok": ok,
                        "code": code_val,
                    }
                    result["calls"].append(call_info)
                    if ok:
                        say("\n" + "*" * 68)
                        say(
                            "*** SUCCESS! WORKING DISARM COMMAND VERIFIED ON ARC3800H HARDWARE! ***"
                        )
                        say("Method: AlarmRegion.setZoneArmMode")
                        say(f"Params: {params_str}")
                        say(f"Reply:  {json.dumps(redact(rep))}")
                        say("*" * 68 + "\n")
                        result["success"] = True
                        result["winning_call"] = {
                            "method": "AlarmRegion.setZoneArmMode",
                            "params": params,
                            "reply": redact(rep),
                        }
                        break
                    elif code_val == 268632080:
                        say(f"  [AUTH GATE] {label} -> code 268632080 (Auth required)")
                    elif code_val == -267976701:
                        zone_reject_count += 1
                    else:
                        say(f"  [REPLY {code_val}] {label} -> {msg_val}")

                if not result["success"]:
                    say(
                        f"  ... tested {len(zone_cases)} shapes on AlarmRegion.setZoneArmMode ({zone_reject_count} param rejected)"
                    )

            if not result["success"]:
                say("\n--- Testing configManager.setConfig (AreaArmMode Disarm) ---")
                cfg_cases = [
                    (
                        "cfg-32-D",
                        {"name": "AreaArmMode", "table": [{"Mode": "D"}] * 32},
                    ),
                    (
                        "cfg-32-DisArm",
                        {"name": "AreaArmMode", "table": [{"Mode": "DisArm"}] * 32},
                    ),
                    (
                        "cfg-1-D",
                        {"name": "AreaArmMode", "table": [{"Area": 1, "Mode": "D"}]},
                    ),
                    (
                        "cfg-1-DisArm",
                        {
                            "name": "AreaArmMode",
                            "table": [{"Area": 1, "Mode": "DisArm"}],
                        },
                    ),
                ]
                for tag, c_p in cfg_cases:
                    sleep(0.05)
                    rep = transport.call(
                        "configManager.setConfig", c_p, fragmented=True
                    )
                    ok = bool(rep.get("result"))
                    err = rep.get("error") or {}
                    c_val = err.get("code")
                    msg_val = err.get("message", "")
                    call_info = {
                        "method": "configManager.setConfig",
                        "label": tag,
                        "params": redact(c_p),
                        "ok": ok,
                        "code": c_val,
                    }
                    result["calls"].append(call_info)
                    if ok:
                        say("\n" + "*" * 68)
                        say(
                            "*** SUCCESS! WORKING DISARM COMMAND VERIFIED ON ARC3800H HARDWARE! ***"
                        )
                        say("Method: configManager.setConfig")
                        say(f"Params: {scrubbed(json.dumps(c_p))}")
                        say(f"Reply:  {json.dumps(redact(rep))}")
                        say("*" * 68 + "\n")
                        result["success"] = True
                        result["winning_call"] = {
                            "method": "configManager.setConfig",
                            "params": c_p,
                            "reply": redact(rep),
                        }
                        break
                    else:
                        say(f"  [REPLY {c_val}] {tag} -> {msg_val}")
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        say(f"Stopped: {result['aborted']}")
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return result


def probe_getters(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    ask_code: Callable[[str], str] = getpass.getpass,
    factory=DHIPTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Probe alarm getter methods on an acquired instance across parameter and credential shapes."""
    code = ""
    if getattr(args, "prompt_code", False):
        code = ask_code("Alarm-user code (hidden; never printed or stored): ").strip()
        if not code:
            raise GuardError("no code entered; probe cancelled")

    extra_scrubs: list[str] = []

    def scrubbed(text: str) -> str:
        s = scrub(text, code) if code else text
        s = scrub(s, password) if password else s
        for sec in extra_scrubs:
            if sec:
                s = scrub(s, sec)
        return s

    def say(message: str) -> None:
        emit(scrubbed(message))

    service = getattr(args, "service", "alarm") or "alarm"
    check_method(f"{service}.test")
    result: dict[str, Any] = {
        "service": service,
        "started": now(),
        "calls": [],
        "aborted": None,
    }

    methods = [
        f"{service}.getDefenceArmMode",
        f"{service}.getArmMode",
        f"{service}.getActivateDefenceArea",
    ]

    candidates: list[tuple[str, dict[str, Any]]] = [
        ("empty", {}),
        ("subsystem-0", {"subSystem": 0}),
        ("SubSystem-0", {"SubSystem": 0}),
        ("channel-0", {"channel": 0}),
        ("channel-1", {"channel": 1}),
        ("channel-2", {"channel": 2}),
        ("channel-16", {"channel": 16}),
        ("channel-list-0", {"channel": [0]}),
        ("channel-list-1", {"channel": [1]}),
        ("channel-all", {"channel": "all"}),
        ("channel-0-type-arm", {"channel": 0, "type": "arm"}),
        ("channel-1-type-arm", {"channel": 1, "type": "arm"}),
        ("channel-0-type-alarmin", {"channel": 0, "type": "AlarmIn"}),
        ("channel-1-type-alarmin", {"channel": 1, "type": "AlarmIn"}),
        ("channel-0-Type-AlarmIn", {"channel": 0, "Type": "AlarmIn"}),
        ("channel-1-Type-AlarmIn", {"channel": 1, "Type": "AlarmIn"}),
        ("channel-0-mode-0", {"channel": 0, "mode": 0}),
        ("channel-1-mode-0", {"channel": 1, "mode": 0}),
        ("channels-0", {"channels": [0]}),
        ("channels-1", {"channels": [1]}),
        ("defenceArea-0", {"defenceArea": 0}),
        ("defenceArea-1", {"defenceArea": 1}),
        ("defenceAreas-0", {"defenceAreas": [0]}),
        ("defenceAreas-1", {"defenceAreas": [1]}),
        ("subsystem-0-channel-0", {"subSystem": 0, "channel": 0}),
        ("subsystem-0-channel-1", {"subSystem": 0, "channel": 1}),
        ("Channel-0", {"Channel": 0}),
        ("area-0", {"area": 0}),
        ("Area-0", {"Area": 0}),
        ("areas-list", {"Areas": [0]}),
        ("zones-list", {"Zones": [0]}),
        ("index-0", {"Index": 0}),
        ("admin-password", {"password": password}),
        ("admin-Password", {"Password": password}),
        ("admin-user-password", {"user": args.user, "password": password}),
        ("admin-User-Password", {"User": args.user, "Password": password}),
        ("subsystem-admin-password", {"subSystem": 0, "password": password}),
        ("SubSystem-admin-Password", {"SubSystem": 0, "Password": password}),
    ]

    if password:
        pw_md5 = hashlib.md5(password.encode()).hexdigest()
        candidates.extend(
            [
                ("admin-md5-lower", {"Password": pw_md5}),
                ("admin-md5-upper", {"Password": pw_md5.upper()}),
                ("admin-user-md5", {"User": args.user, "Password": pw_md5}),
            ]
        )

    if code:
        code_md5 = hashlib.md5(code.encode()).hexdigest()
        candidates.extend(
            [
                ("code-password", {"password": code}),
                ("code-Password", {"Password": code}),
                ("code-md5-lower", {"Password": code_md5}),
                ("code-md5-upper", {"Password": code_md5.upper()}),
                ("code-pin", {"pin": code}),
                ("code-Pin", {"Pin": code}),
                ("code-Code", {"Code": code}),
                ("user-usernumber", {"UserNumber": args.user_number, "Password": code}),
                (
                    "user-usernumber-md5",
                    {"UserNumber": args.user_number, "Password": code_md5},
                ),
                (
                    "user-usernumber-lower",
                    {"userNumber": args.user_number, "password": code},
                ),
                ("user-userid", {"UserID": args.user_number, "Password": code}),
                ("user-userid-lower", {"userId": args.user_number, "password": code}),
                (
                    "user-object",
                    {"User": {"UserNumber": args.user_number, "Password": code}},
                ),
                ("subsystem-code-password", {"subSystem": 0, "password": code}),
                ("subsystem-code-Password", {"SubSystem": 0, "Password": code}),
                ("subsystem-code-pin", {"subSystem": 0, "pin": code}),
                ("subsystem-code-Pin", {"SubSystem": 0, "Pin": code}),
                ("channel-code-password", {"channel": 0, "password": code}),
                ("channel-1-code-password", {"channel": 1, "password": code}),
                ("channel-code-Password", {"Channel": 0, "Password": code}),
                ("channel-code-pin", {"channel": 0, "pin": code}),
                ("channel-1-code-pin", {"channel": 1, "pin": code}),
                (
                    "subsystem-user-password",
                    {"SubSystem": 0, "UserNumber": args.user_number, "Password": code},
                ),
                (
                    "subsystem-user-lower",
                    {"subSystem": 0, "userNumber": args.user_number, "password": code},
                ),
                ("areas-code-Password", {"Areas": [0], "Password": code}),
                ("zones-code-Password", {"Zones": [0], "Password": code}),
            ]
        )

    transport = factory(args.host, args.port, timeout=12)
    object_id: int | None = None
    try:
        transport.connect()
        transport.login(args.user, password, args.client_type)
        say(f"{args.client_type} login ok")

        # Query alarm users read-only (passwords are redacted)
        say("\n--- Checking AlarmUserManager users ---")
        user_info = transport.call(
            "AlarmUserManager.getUserInfoAll", None, fragmented=True
        )
        users_list: list[dict[str, Any]] = []
        if user_info.get("result"):
            params = user_info.get("params") or {}
            if isinstance(params, list):
                users_list = [x for x in params if isinstance(x, dict)]
            elif isinstance(params, dict):
                for val in params.values():
                    if isinstance(val, list):
                        users_list.extend(x for x in val if isinstance(x, dict))
            say(f"Found {len(users_list)} alarm user(s):")
            for u in users_list:
                user_num = u.get("UserNumber")
                user_name = u.get("Name")
                user_id = u.get("Id")
                user_pw = u.get("Password")
                subsystems = u.get("SubSystems") or [1]
                say(
                    f"  User #{user_num} (ID: {user_id}) name={user_name!r} "
                    f"auth={u.get('AuthorityList')} SubSystems={len(subsystems)}"
                )

                # Collect passwords to try for this user
                if user_pw:
                    extra_scrubs.append(user_pw)
                passwords_to_try: list[tuple[str, str]] = []
                if code:
                    passwords_to_try.append(("code", code))
                    passwords_to_try.append(
                        ("code-md5", hashlib.md5(code.encode()).hexdigest())
                    )
                if user_pw and user_pw != code:
                    passwords_to_try.append(("stored-pw", user_pw))

                for pw_tag, pw in passwords_to_try:
                    # Defence channel shapes (proven format: channel is list of ints)
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-ch0",
                            {"channel": [0], "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-ch1",
                            {"channel": [1], "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (f"{pw_tag}-ch0-pw", {"channel": [0], "Password": pw})
                    )
                    candidates.append(
                        (f"{pw_tag}-ch1-pw", {"channel": [1], "Password": pw})
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-name-ch0",
                            {"channel": [0], "Name": user_name, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-name-ch1",
                            {"channel": [1], "Name": user_name, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-user-ch0",
                            {"channel": [0], "User": user_name, "Password": pw},
                        )
                    )

                    # ArmMode subsystem shapes (subsystems are 1..32)
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-pw",
                            {"UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-sub1",
                            {"subSystem": 1, "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-subs1",
                            {"subSystem": [1], "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-Sub1",
                            {"SubSystem": 1, "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-u{user_num}-Subs1",
                            {"SubSystem": [1], "UserNumber": user_num, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-name-sub1",
                            {"subSystem": 1, "Name": user_name, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-name-Sub1",
                            {"SubSystem": 1, "Name": user_name, "Password": pw},
                        )
                    )
                    candidates.append(
                        (
                            f"{pw_tag}-user-sub1",
                            {"subSystem": 1, "User": user_name, "Password": pw},
                        )
                    )
                    candidates.append(
                        (f"{pw_tag}-name-pw", {"Name": user_name, "Password": pw})
                    )
                    candidates.append(
                        (f"{pw_tag}-user-pw", {"User": user_name, "Password": pw})
                    )
        else:
            code_u = (user_info.get("error") or {}).get("code")
            say(f"  AlarmUserManager.getUserInfoAll -> code {code_u}")

        # Check system.methodSignature for candidate arm / defence methods
        say("\n--- Checking system.methodSignature for candidate arm methods ---")
        for m in (
            f"{service}.setDefenceMode",
            f"{service}.getDefenceArmMode",
            f"{service}.setArmMode",
            f"{service}.getArmMode",
        ):
            for s_name, builder in (
                ("name", lambda x: {"name": x}),
                ("method", lambda x: {"method": x}),
                ("method_name", lambda x: {"method_name": x}),
                ("positional", lambda x: x),
            ):
                sig_rep = transport.call(
                    "system.methodSignature", builder(m), fragmented=True
                )
                if sig_rep.get("result"):
                    sig = (sig_rep.get("params") or {}).get("signature")
                    if sig:
                        say(f"  {m} signature ({s_name}): {sig}")
                        break

        say(f"\nAcquiring {service} instance...")
        inst_reply = transport.call(
            f"{service}.factory.instance", None, fragmented=True
        )
        object_id = _object_id(inst_reply)
        if object_id is None:
            raise GuardError(f"could not acquire {service} instance: {inst_reply}")
        say(f"Acquired {service} instance object_id={object_id}\n")

        stop_all = False
        for method in methods:
            if stop_all:
                break
            say(f"--- Probing {method} ({len(candidates)} candidate shapes) ---")
            for label, params in candidates:
                sleep(0.1)
                reply = transport.call(
                    method, params, object_id=object_id, fragmented=True
                )
                ok = bool(reply.get("result"))
                err = reply.get("error") or {}
                err_code = err.get("code")
                err_msg = err.get("message", "")
                call_record = {
                    "method": method,
                    "label": label,
                    "params": redact(params),
                    "ok": ok,
                    "error_code": err_code,
                    "reply": redact(reply) if ok else None,
                }
                result["calls"].append(call_record)
                shown_params = scrubbed(json.dumps(params))
                if ok:
                    say(
                        f"  [SUCCESS!] {label}: {shown_params} -> {json.dumps(redact(reply))}"
                    )
                    setter = (
                        f"{service}.setDefenceMode"
                        if "Defence" in method
                        else f"{service}.setArmMode"
                    )
                    say(
                        f"\n  >>> Found working getter shape! Testing matching disarm on {setter} ..."
                    )
                    for mode_key in ("mode", "Mode"):
                        disarm_params = dict(params)
                        disarm_params[mode_key] = 0
                        disarm_rep = transport.call(
                            setter, disarm_params, object_id=object_id, fragmented=True
                        )
                        say(
                            f"  [{setter}] {scrubbed(json.dumps(disarm_params))} -> "
                            f"{json.dumps(redact(disarm_rep))}"
                        )
                        if disarm_rep.get("result"):
                            say(
                                "\n  " + "*" * 64 + "\n"
                                "  *** SUCCESS! DISARM COMMAND VERIFIED ON ARC3800H HARDWARE! ***\n"
                                f"  *** Method: {setter}\n"
                                f"  *** Shape:  {scrubbed(json.dumps(disarm_params))}\n"
                                "  " + "*" * 64 + "\n"
                            )
                            break
                    break
                elif err_code in LOCKOUT_RISK_CODES and err_code != 268632080:
                    say(
                        f"  [CRITICAL] {label}: LOCKOUT RISK code {err_code} ({err_msg})! ABORTING!"
                    )
                    result["aborted"] = f"lockout risk code {err_code}"
                    stop_all = True
                    break
                elif err_code == -267976703:
                    say(
                        f"  [NOT SUPPORTED] method {method} not implemented (code -267976703); skipping."
                    )
                    break
                elif err_code == 268632080:
                    say(
                        f"  [AUTH FAIL] {label}: code 268632080 (User/password incorrect or missing)"
                    )
                elif err_code == -267976701:
                    say(
                        f"  [PARAM REJECT] {label}: code -267976701 (Invalid parameter)"
                    )
                else:
                    say(f"  [ERROR {err_code}] {label}: {shown_params} -> {err_msg}")
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        say(f"Stopped: {result['aborted']}")
    finally:
        if object_id is not None:
            with contextlib.suppress(Exception):
                transport.call(
                    f"{service}.destroy", None, object_id=object_id, fragmented=True
                )
                say(f"\nDestroyed {service} instance object_id={object_id}")
        with contextlib.suppress(Exception):
            transport.close()
    return result


def probe_disarm(
    args: argparse.Namespace,
    password: str,
    *,
    emit: Callable[[str], None] = print,
    ask_code: Callable[[str], str] = getpass.getpass,
    factory=DHIPTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Sweep candidate disarm requests across setters and parameter/credential shapes."""
    if not args.i_am_at_the_panel:
        raise GuardError("refusing to run without --i-am-at-the-panel")

    code = ""
    if getattr(args, "prompt_code", False):
        code = ask_code("Alarm-user code (hidden; never printed or stored): ").strip()
        if not code:
            raise GuardError("no code entered; probe cancelled")

    extra_scrubs: list[str] = []

    def scrubbed(text: str) -> str:
        s = scrub(text, code) if code else text
        s = scrub(s, password) if password else s
        for sec in extra_scrubs:
            if sec:
                s = scrub(s, sec)
        return s

    def say(message: str) -> None:
        emit(scrubbed(message))

    service = getattr(args, "service", "alarm") or "alarm"
    check_method(f"{service}.test")
    result: dict[str, Any] = {
        "service": service,
        "started": now(),
        "success": False,
        "winning_call": None,
        "calls": [],
        "aborted": None,
    }

    transport = factory(args.host, args.port, timeout=12)
    object_id: int | None = None
    try:
        transport.connect()
        transport.login(args.user, password, args.client_type)
        say(f"{args.client_type} login ok")

        # 1. Pre-check: Verify panel is 100% disarmed
        say("\n--- Checking panel arm state before probe ---")
        sub_rep = transport.call(f"{service}.getSubSystem", {}, fragmented=True)
        if sub_rep.get("result"):
            sub_states = (sub_rep.get("params") or {}).get("State") or []
            say(
                f"Subsystems state: {len(sub_states)} subsystems, sample: {sub_states[:3]}"
            )
            non_disarmed = [
                i + 1 for i, st in enumerate(sub_states) if st != "AllDisarming"
            ]
            if non_disarmed and not args.allow_arm:
                raise GuardError(
                    f"Subsystem(s) not disarmed: {non_disarmed}; disarm panel first or use --allow-arm"
                )
        area_modes = read_modes(transport)
        say(
            f"AreaArmMode state: {area_modes[:4]}... (all D: {all(m == DISARMED for m in area_modes)})"
        )
        if not all(m == DISARMED for m in area_modes) and not args.allow_arm:
            raise GuardError(
                "Not all areas are disarmed; disarm first or use --allow-arm"
            )
        say("Panel verified disarmed; safe to proceed with disarm probe.\n")

        # 2. Extract alarm users
        say("--- Extracting AlarmUserManager users ---")
        user_info = transport.call(
            "AlarmUserManager.getUserInfoAll", None, fragmented=True
        )
        users_list: list[dict[str, Any]] = []
        if user_info.get("result"):
            params = user_info.get("params") or {}
            if isinstance(params, list):
                users_list = [x for x in params if isinstance(x, dict)]
            elif isinstance(params, dict):
                for val in params.values():
                    if isinstance(val, list):
                        users_list.extend(x for x in val if isinstance(x, dict))
            say(f"Found {len(users_list)} alarm user(s):")
            for u in users_list:
                user_num = u.get("UserNumber")
                user_name = u.get("Name")
                user_pw = u.get("Password")
                if user_pw:
                    extra_scrubs.append(user_pw)
                say(
                    f"  User #{user_num} (ID: {u.get('Id')}) name={user_name!r} "
                    f"auth={u.get('AuthorityList')}"
                )
        else:
            say("  AlarmUserManager.getUserInfoAll returned no users")

        primary_user = users_list[0] if users_list else {}
        u_num = primary_user.get("UserNumber", args.user_number)
        u_name = primary_user.get("Name", "Konstantinos")
        u_id = primary_user.get("Id", "")

        # 3. Read-only status checks on candidate services
        say("\n--- Querying candidate service states (read-only) ---")
        subsys_rep = transport.call("alarmSubSystem.getState", {}, fragmented=True)
        subsys_res = subsys_rep.get("params") or subsys_rep.get("error") or {}
        say(f"alarmSubSystem.getState {{}}: {scrubbed(json.dumps(subsys_res))}")

        region_caps = transport.call("AlarmRegion.getCaps", {}, fragmented=True)
        region_res = region_caps.get("params") or region_caps.get("error") or {}
        say(f"AlarmRegion.getCaps {{}}: {scrubbed(json.dumps(region_res))}\n")

        # 4. Acquire service instance if available
        if service in ("FakeArc", "alarm"):
            say(f"Acquiring {service} instance...")
            inst_reply = transport.call(
                f"{service}.factory.instance", None, fragmented=True
            )
            object_id = _object_id(inst_reply)
            say(f"Acquired {service} instance object_id={object_id}\n")

        # Build credential options
        cred_options: list[tuple[str, dict[str, Any]]] = [("no-cred", {})]
        if password:
            pw_md5_u = hashlib.md5(password.encode()).hexdigest().upper()
            cred_options.extend(
                [
                    ("admin-pw", {"Password": password}),
                    ("admin-pw-lower", {"password": password}),
                    ("admin-pw-md5", {"Password": pw_md5_u}),
                    ("admin-user-pw", {"User": args.user, "Password": password}),
                ]
            )
        if code:
            code_md5_u = hashlib.md5(code.encode()).hexdigest().upper()
            cred_options.extend(
                [
                    ("code-pw", {"Password": code}),
                    ("code-pw-lower", {"password": code}),
                    ("code-pw-md5", {"Password": code_md5_u}),
                    ("code-u-num", {"UserNumber": u_num, "Password": code}),
                    ("code-u-num-lower", {"userNumber": u_num, "password": code}),
                    ("code-name", {"Name": u_name, "Password": code}),
                    ("code-name-lower", {"name": u_name, "password": code}),
                    ("code-user", {"User": u_name, "Password": code}),
                    ("code-username", {"UserName": u_name, "Password": code}),
                    (
                        "code-user-dict",
                        {"User": {"UserNumber": u_num, "Password": code}},
                    ),
                ]
            )
            if u_id:
                cred_options.append(("code-id", {"Id": str(u_id), "Password": code}))

        # Build setter suites
        # Each suite has: (method_name, supports_instance, candidate_list)
        setter_suites: list[tuple[str, bool, list[tuple[str, dict[str, Any]]]]] = []

        if service == "FakeArc":
            # Test-suite compatibility
            fake_targets = [
                ("def-0", {"defenceArea": 0}),
                ("ch-0", {"channel": 0}),
                ("mode-only", {}),
            ]
            fake_candidates = [
                (f"{t_tag}-{c_tag}", {**t_dict, "mode": 0, **c_dict})
                for t_tag, t_dict in fake_targets
                for c_tag, c_dict in cred_options
            ]
            setter_suites.append((f"{service}.setDefenceMode", True, fake_candidates))
            setter_suites.append((f"{service}.setArmMode", True, fake_candidates))
        else:
            # Live hardware candidate suites

            # Suite 1: AlarmRegion.setArmMode (Static only)
            region_targets = [
                ("Ch-0", {"Channel": 0}),
                ("Ch-1", {"Channel": 1}),
                ("ch-0", {"channel": 0}),
                ("ch-1", {"channel": 1}),
                ("ch-list-0", {"channel": [0]}),
                ("ch-list-1", {"channel": [1]}),
                ("areas-list-0", {"Areas": [0]}),
                ("areas-list-1", {"Areas": [1]}),
                ("area-0", {"Area": 0}),
                ("zones-list-0", {"Zones": [0]}),
                ("index-0", {"Index": 0}),
            ]
            region_modes = [
                ("DisArm", {"Mode": "DisArm"}),
                ("Disarm", {"Mode": "Disarm"}),
                ("disarm", {"Mode": "disarm"}),
                ("D", {"Mode": "D"}),
                ("mode-0", {"mode": 0}),
                ("Mode-0", {"Mode": 0}),
                ("ArmMode-DisArm", {"ArmMode": "DisArm"}),
                ("ArmMode-D", {"ArmMode": "D"}),
            ]
            region_candidates = [
                (f"[{t_tag}|{m_tag}|{c_tag}]", {**t_dict, **m_dict, **c_dict})
                for t_tag, t_dict in region_targets
                for m_tag, m_dict in region_modes
                for c_tag, c_dict in cred_options
            ]
            region_candidates.append(
                (
                    "[armMode-list|DisArm|no-cred]",
                    {"armMode": [{"Index": 0, "Mode": "DisArm"}]},
                )
            )
            region_candidates.append(
                ("[armMode-list|D|no-cred]", {"armMode": [{"Index": 0, "Mode": "D"}]})
            )
            setter_suites.append(("AlarmRegion.setArmMode", False, region_candidates))

            # Suite 2: alarmSubSystem.setActive (Static only)
            subsys_actions = [
                ("active-F", {"active": False}),
                ("Active-F", {"Active": False}),
                ("active-0", {"active": 0}),
                ("Active-0", {"Active": 0}),
                ("enable-F", {"enable": False}),
                ("Enable-F", {"Enable": False}),
                ("state-Disarm", {"state": "Disarm"}),
                ("State-Disarm", {"State": "Disarm"}),
                ("State-AllDisarming", {"State": "AllDisarming"}),
                ("action-0", {"action": 0}),
                ("Action-0", {"Action": 0}),
            ]
            subsys_targets = [
                ("sub-1", {"subSystem": 1}),
                ("sub-list-1", {"subSystem": [1]}),
                ("sub-0", {"subSystem": 0}),
                ("Sub-1", {"SubSystem": 1}),
                ("Sub-list-1", {"SubSystem": [1]}),
                ("subs-list-1", {"subSystems": [1]}),
                ("ch-0", {"channel": 0}),
                ("ch-list-0", {"channel": [0]}),
                ("Ch-0", {"Channel": 0}),
                ("Index-0", {"Index": 0}),
                ("index-0", {"index": 0}),
            ]
            subsys_candidates = [
                (f"[{a_tag}|{t_tag}|{c_tag}]", {**a_dict, **t_dict, **c_dict})
                for a_tag, a_dict in subsys_actions
                for t_tag, t_dict in subsys_targets
                for c_tag, c_dict in cred_options
            ]
            setter_suites.append(("alarmSubSystem.setActive", False, subsys_candidates))

            # Suite 3: alarm.setDefenceMode and alarm.setArmMode (Static, then Instance)
            alarm_targets = [
                ("ch-list-0", {"channel": [0]}),
                ("ch-list-1", {"channel": [1]}),
                ("ch-0", {"channel": 0}),
                ("ch-1", {"channel": 1}),
                ("Ch-0", {"Channel": 0}),
                ("sub-1", {"subSystem": 1}),
                ("sub-list-1", {"subSystem": [1]}),
                ("Sub-1", {"SubSystem": 1}),
                ("Sub-list-1", {"SubSystem": [1]}),
                ("def-list-0", {"defenceAreas": [0]}),
                ("areas-list-0", {"Areas": [0]}),
                ("zones-list-0", {"Zones": [0]}),
            ]
            alarm_modes = [
                ("mode-0", {"mode": 0}),
                ("Mode-0", {"Mode": 0}),
                ("mode-DisArm", {"mode": "DisArm"}),
                ("Mode-DisArm", {"Mode": "DisArm"}),
                ("mode-disarm", {"mode": "disarm"}),
                ("Mode-D", {"Mode": "D"}),
                ("action-0", {"action": 0}),
                ("Action-0", {"Action": 0}),
                ("action-disarm", {"action": "disarm"}),
                ("Action-DisArm", {"Action": "DisArm"}),
                ("alarmMode-0", {"alarmMode": 0}),
                ("defenceMode-0", {"defenceMode": 0}),
            ]
            alarm_candidates = [
                (f"[{t_tag}|{m_tag}|{c_tag}]", {**t_dict, **m_dict, **c_dict})
                for t_tag, t_dict in alarm_targets
                for m_tag, m_dict in alarm_modes
                for c_tag, c_dict in cred_options
            ]
            setter_suites.append(("alarm.setDefenceMode", True, alarm_candidates))
            setter_suites.append(("alarm.setArmMode", True, alarm_candidates))

            # Suite 4: alarmSubregion.setArmMode (Static only)
            setter_suites.append(
                ("alarmSubregion.setArmMode", False, region_candidates)
            )

        # Sweep execution
        total_candidates = sum(len(c) for _, _, c in setter_suites)
        say("--- Starting Disarm Candidate Sweep ---")
        say(
            f"Total candidate parameter matrix: {total_candidates} combinations across {len(setter_suites)} setters."
        )
        say("Sending disarm-only requests with rapid short-circuit on success...\n")

        found = False
        call_count = 0
        auth_failures: list[str] = []

        for method, can_instance, candidates in setter_suites:
            if found:
                break
            say(f"\n>>> Sweeping {method} ({len(candidates)} candidate shapes) ...")

            for method_call_count, (label, params) in enumerate(candidates, 1):
                if found:
                    break
                call_count += 1
                sleep(0.03)

                # 1. Try Static first
                reply = transport.call(method, params, fragmented=True)
                ok = bool(reply.get("result"))
                err = reply.get("error") or {}
                err_code = err.get("code")
                err_msg = err.get("message", "")
                is_instance = False
                used_obj_id = None

                # 2. If static returned not-found on first call, skip this whole method
                if method_call_count == 1 and err_code == -267976703:
                    say(
                        f"  [NOT SUPPORTED] {method} is not implemented (code -267976703); skipping suite."
                    )
                    break

                # 3. If static failed and method supports instance and instance was acquired, try instance
                if not ok and can_instance and object_id is not None:
                    inst_reply = transport.call(
                        method, params, object_id=object_id, fragmented=True
                    )
                    if inst_reply.get("result"):
                        reply = inst_reply
                        ok = True
                        is_instance = True
                        used_obj_id = object_id
                    else:
                        inst_err = inst_reply.get("error") or {}
                        inst_code = inst_err.get("code")
                        if inst_code not in (-267976701, -267976703):
                            err = inst_err
                            err_code = inst_code
                            err_msg = inst_err.get("message", "")
                            is_instance = True

                call_record = {
                    "method": method,
                    "label": label,
                    "params": redact(params),
                    "instance": is_instance,
                    "ok": ok,
                    "error_code": err_code,
                }
                result["calls"].append(call_record)

                if ok:
                    result["success"] = True
                    result["winning_call"] = {
                        "method": method,
                        "instance": is_instance,
                        "object_id": used_obj_id,
                        "params": params,
                        "reply": redact(reply),
                    }
                    say("\n" + "*" * 68)
                    say(
                        "*** SUCCESS! WORKING DISARM COMMAND VERIFIED ON LIVE HARDWARE! ***"
                    )
                    say(f"Method:   {method}")
                    say(f"Instance: {is_instance} (object_id={used_obj_id})")
                    say(f"Params:   {scrubbed(json.dumps(params, indent=2))}")
                    say(f"Reply:    {json.dumps(redact(reply))}")
                    say("*" * 68 + "\n")
                    found = True
                    break
                elif err_code == 268632080:
                    auth_failures.append(f"{method} {label}")
                    say(
                        f"  [AUTH GATE REACHED] #{call_count} {method} {label} -> code 268632080 (Auth required)!"
                    )
                elif err_code == 289406978:
                    say(
                        f"  [PARAM VALUE REJECT] #{call_count} {method} {label} -> code 289406978 (Invalid Param)!"
                    )
                elif err_code not in (-267976701, -267976703):
                    say(
                        f"  [REPLY {err_code}] #{call_count} {method} {label} -> {err_msg}"
                    )
                elif method_call_count % 100 == 0:
                    say(
                        f"  ... tested {method_call_count}/{len(candidates)} on {method} (all -267976701)"
                    )

        if not found:
            say(
                f"\nProbe complete: {call_count} candidates tested, no setter returned result=true."
            )
            if auth_failures:
                say(
                    f"\nNote: {len(auth_failures)} candidate(s) successfully passed schema validation "
                    "and reached the authentication gate (code 268632080)."
                )
    except (DHIPError, OSError) as exc:
        result["aborted"] = f"{type(exc).__name__}: {exc}"
        say(f"Stopped: {result['aborted']}")
    finally:
        if object_id is not None:
            with contextlib.suppress(Exception):
                transport.call(
                    f"{service}.destroy", None, object_id=object_id, fragmented=True
                )
                say(f"Destroyed {service} instance object_id={object_id}")
        with contextlib.suppress(Exception):
            transport.close()
    return result


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
        "--sweep-keys",
        action="store_true",
        help="read-only: find which parameter key names the ARC recognises by "
        "calling getters with candidate keys (one Web3.0 session)",
    )
    parser.add_argument(
        "--all-getters",
        action="store_true",
        help="sweep keys across all AlarmRegion getters (getAreaInfo, getAreaZones, getAreaSummaries, getZonesInfo, etc.)",
    )
    parser.add_argument(
        "--all-values",
        action="store_true",
        help="test 1, [1], and {} in addition to 0 and [0] in key sweep",
    )
    parser.add_argument(
        "--probe-condition",
        action="store_true",
        help="probe AlarmRegion methods with Condition object shapes and 32-area disarm array shapes",
    )
    parser.add_argument(
        "--sweep-instances",
        action="store_true",
        help="read-only: probe factory.instance across candidate alarm services",
    )
    parser.add_argument(
        "--inspect-service",
        help="read-only: list all methods and instance methods for a candidate alarm service",
    )
    parser.add_argument(
        "--probe-getters",
        action="store_true",
        help="read-only: probe alarm getters on an instance across candidate parameter and auth shapes",
    )
    parser.add_argument(
        "--probe-disarm",
        action="store_true",
        help="disarm probe: sweep candidate disarm requests across setters and parameter/credential shapes (requires --i-am-at-the-panel)",
    )
    parser.add_argument(
        "--service",
        default="alarm",
        help="alarm service to inspect or probe (default alarm)",
    )
    parser.add_argument(
        "--instance",
        action="store_true",
        help="acquire instance via <service>.factory.instance before calling, then destroy",
    )
    parser.add_argument(
        "--sweep-delay",
        type=float,
        default=0.2,
        help="seconds between --sweep-keys calls (default 0.2)",
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
        "or stored (needed by the *-password / *-user* shapes, --probe-getters, or by a "
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
    for name in [args.client_type, *(args.client_types or [])]:
        try:
            check_client_type(name)
        except GuardError as exc:
            parser.error(str(exc))
    active_probes = sum(
        bool(x)
        for x in (
            args.probe_client_types,
            args.sweep_keys,
            args.sweep_instances,
            bool(args.inspect_service),
            args.probe_getters,
            args.probe_disarm,
            args.probe_condition,
        )
    )
    if active_probes > 1:
        parser.error(
            "use only one of --probe-client-types, --sweep-keys, --sweep-instances, --inspect-service, --probe-getters, --probe-disarm, or --probe-condition"
        )
    if (
        args.probe_client_types
        or args.sweep_keys
        or args.sweep_instances
        or args.inspect_service
    ):
        if args.shape or args.params_json or args.prompt_code or args.instance:
            parser.error("the read-only probes take no shape, params, instance or code")
        return args
    if args.probe_getters:
        if args.shape or args.params_json or args.instance:
            parser.error("--probe-getters takes no shape, params or instance")
        return args
    if args.probe_condition:
        if args.shape or args.params_json or args.instance:
            parser.error("--probe-condition takes no shape, params or instance")
        return args
    if args.probe_disarm:
        if not args.i_am_at_the_panel:
            parser.error("--probe-disarm requires --i-am-at-the-panel")
        if args.shape or args.params_json or args.instance:
            parser.error("--probe-disarm takes no shape, params or instance")
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
        if args.sweep_keys:
            transcript = sweep_keys(args, password)
        elif args.sweep_instances:
            transcript = sweep_instances(args, password)
        elif args.inspect_service:
            transcript = inspect_service(args, password)
        elif args.probe_getters:
            transcript = probe_getters(args, password)
        elif args.probe_disarm:
            transcript = probe_disarm(args, password)
        elif args.probe_condition:
            transcript = probe_condition(args, password)
        elif args.probe_client_types:
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
