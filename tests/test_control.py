"""ArmController against the in-process fake ARC (FAKE_COMMAND_SPEC only)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from custom_components.dahua_arc.protocol.arming import ArmingTracker
from custom_components.dahua_arc.protocol.control import (
    FAKE_COMMAND_SPEC,
    ArmCommand,
    ArmController,
    CommandOutcome,
)
from custom_components.dahua_arc.protocol.inventory import (
    extract_area_zones,
    extract_arm_areas,
)
from custom_components.dahua_arc.protocol.snapshot import SnapshotClient
from custom_components.dahua_arc.vendor.dahua import const
from custom_components.dahua_arc.vendor.dahua.exceptions import LoginError
from tests.fake_arc import ARM_METHOD, FakeArc

PASSWORD = "test-only"
NAMES = ["Living Room", "Garage", "Office"]
OFFICE_WINDOW = {"Index": 191, "Name": "Office Window", "Reason": "Open"}


def _arc() -> FakeArc:
    return FakeArc(
        password=PASSWORD,
        inventory={
            "AreaArmMode": {"Areas": [{"Mode": "D"} for _ in NAMES]},
            "AlarmSubSystem": [
                {"Enable": True, "AreaId": i + 1, "Name": name, "Zone": []}
                for i, name in enumerate(NAMES)
            ],
        },
    )


@dataclass
class Rig:
    arc: FakeArc
    tracker: ArmingTracker
    snapshot: SnapshotClient
    stop: threading.Event = field(default_factory=threading.Event)
    available: bool = True
    auth_failed: bool = False
    auth_callbacks: list[LoginError] = field(default_factory=list)
    password: str = PASSWORD
    controller: ArmController | None = None

    def build(self, **kwargs: Any) -> ArmController:
        def on_auth_failed(exc: LoginError) -> None:
            self.auth_failed = True
            self.auth_callbacks.append(exc)

        kwargs.setdefault("wait_timeout", 1.0)
        kwargs.setdefault("reply_timeout", 1.0)
        self.controller = ArmController(
            host="192.0.2.10",
            port=5000,
            username="admin",
            password=self.password,
            spec=FAKE_COMMAND_SPEC,
            tracker=self.tracker,
            read_table=lambda: self.snapshot.read_config("AreaArmMode"),
            hub_available=lambda: self.available,
            auth_failed=lambda: self.auth_failed,
            on_auth_failed=on_auth_failed,
            stop_event=self.stop,
            allow_fake_spec=True,
            **kwargs,
        )
        return self.controller

    def arm_calls(self) -> int:
        return self.arc.calls.count(ARM_METHOD)


@pytest.fixture
def rig() -> Iterator[Rig]:
    arc = _arc()
    tracker = ArmingTracker(dict(enumerate(NAMES)), quiet_seconds=0)
    tracker.apply_table({0: "D", 1: "D", 2: "D"}, tracker.watermark())
    arc.event_listeners.append(tracker.apply_event)
    with arc.patch():
        snapshot = SnapshotClient("192.0.2.10", 5000, "admin", PASSWORD)
        rig = Rig(arc, tracker, snapshot)
        rig.build()
        yield rig
        snapshot.close()
        arc.close_timers()


def _cmd(mode: str = "p1", areas: tuple[int, ...] = (0, 1, 2)) -> ArmCommand:
    return ArmCommand(mode=mode, areas=areas, origin="ha:test")  # type: ignore[arg-type]


def _run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    return thread, out


def _wait(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_fake_method_matches_the_spec() -> None:
    assert FAKE_COMMAND_SPEC.method == ARM_METHOD


def test_fake_spec_is_refused_outside_tests(rig: Rig) -> None:
    with pytest.raises(ValueError, match="tests only"):
        ArmController(
            host="192.0.2.10",
            port=5000,
            username="admin",
            password=PASSWORD,
            spec=FAKE_COMMAND_SPEC,
            tracker=rig.tracker,
            read_table=lambda: None,
            hub_available=lambda: True,
            auth_failed=lambda: False,
            on_auth_failed=lambda exc: None,
            stop_event=threading.Event(),
        )


def test_confirmed_by_events(rig: Rig) -> None:
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.CONFIRMED
    assert result.confirmed_by == "event"
    assert rig.arm_calls() == 1
    assert rig.arc.arm_requests == [{"Mode": "p1", "Areas": [0, 1, 2]}]
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"p1"}
    assert const.LOGOUT in rig.arc.calls
    assert rig.arc.sockets == []  # the control session is closed


def test_single_area_arm_and_disarm(rig: Rig) -> None:
    assert rig.controller.execute(_cmd("T", (1,))).outcome is CommandOutcome.CONFIRMED
    assert [a.raw_mode for a in rig.tracker.areas.values()] == ["D", "T", "D"]
    assert rig.controller.execute(_cmd("D", (1,))).outcome is CommandOutcome.CONFIRMED
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"D"}
    assert rig.arm_calls() == 2


def test_confirmed_by_table_when_events_are_missing(rig: Rig) -> None:
    rig.arc.suppress_arm_events = True
    rig.build(wait_timeout=0.2)
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.CONFIRMED
    assert result.confirmed_by == "table"
    assert rig.arm_calls() == 1
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"T"}


def test_refused_by_event_names_the_open_zones(rig: Rig) -> None:
    rig.arc.open_zones = [OFFICE_WINDOW]
    result = rig.controller.execute(_cmd("p1", (2,)))
    assert result.outcome is CommandOutcome.REFUSED
    assert [z["zone"] for z in result.open_zones] == ["Office Window"]
    assert rig.tracker.areas[2].raw_mode == "D"
    assert rig.arm_calls() == 1


def test_refused_by_reply(rig: Rig) -> None:
    rig.arc.open_zones = [OFFICE_WINDOW]
    rig.arc.arm_reply = "refused"
    result = rig.controller.execute(_cmd("p1", (2,)))
    assert result.outcome is CommandOutcome.REFUSED
    assert result.rpc_error_code == 1001
    assert [z["zone"] for z in result.open_zones] == ["Office Window"]


def test_error_reply_without_zones_fails_with_the_code(rig: Rig) -> None:
    rig.arc.arm_reply = "error"
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.FAILED
    assert result.rpc_error_code == 268894210
    assert result.rpc_error_message == "No permission"
    assert rig.arm_calls() == 1


def test_lost_reply_fails_and_is_never_resent(rig: Rig) -> None:
    rig.arc.arm_reply = "no_reply"
    rig.build(reply_timeout=0.2)
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.FAILED
    assert result.reason == "unreachable"
    assert rig.arm_calls() == 1
    time.sleep(0.1)
    assert rig.arm_calls() == 1


def test_unconfirmed_when_nothing_changes(rig: Rig) -> None:
    rig.arc.arm_takes_effect = False
    rig.build(wait_timeout=0.2)
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.UNCONFIRMED
    assert result.reason == "timeout"
    assert rig.arm_calls() == 1


def test_unconfirmed_when_the_table_cannot_be_read(rig: Rig) -> None:
    rig.arc.suppress_arm_events = True
    rig.arc.arm_takes_effect = False
    rig.arc.config_tables.pop("AreaArmMode", None)
    rig.arc.config_tables["AreaArmMode"] = None
    rig.build(wait_timeout=0.2)
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.UNCONFIRMED
    assert result.reason == "table_unreadable"
    assert rig.tracker.last_table_error is not None


def test_second_command_while_busy_is_rejected_at_once(rig: Rig) -> None:
    rig.arc.arm_delay = 0.5
    rig.build(wait_timeout=3.0)
    first, out = _run_in_thread(lambda: rig.controller.execute(_cmd("p1")))
    assert _wait(lambda: rig.controller.in_flight)
    started = time.monotonic()
    second = rig.controller.execute(_cmd("D"))
    assert time.monotonic() - started < 0.2
    assert second.outcome is CommandOutcome.REJECTED
    assert second.reason == "busy"
    first.join(5)
    assert out[0].outcome is CommandOutcome.CONFIRMED
    assert rig.arm_calls() == 1


def test_a_stale_failure_does_not_refuse_a_new_command(rig: Rig) -> None:
    rig.arc.open_zones = [OFFICE_WINDOW]
    assert rig.controller.execute(_cmd("p1", (2,))).outcome is CommandOutcome.REFUSED
    rig.arc.open_zones = []
    result = rig.controller.execute(_cmd("p1", (2,)))
    assert result.outcome is CommandOutcome.CONFIRMED
    assert rig.tracker.areas[2].last_failure is not None


def test_reconnect_during_the_wait_is_resolved_by_the_table(rig: Rig) -> None:
    rig.arc.suppress_arm_events = True
    rig.build(wait_timeout=5.0)
    started = time.monotonic()
    thread, out = _run_in_thread(lambda: rig.controller.execute(_cmd("T", (0,))))
    assert _wait(lambda: rig.arm_calls() == 1)
    rig.tracker.invalidate()
    rig.tracker.apply_table({0: "T", 1: "D", 2: "D"}, rig.tracker.watermark())
    thread.join(4)
    assert out[0].outcome is CommandOutcome.CONFIRMED
    assert out[0].confirmed_by == "table"
    assert time.monotonic() - started < 3


def test_wrong_credentials_report_auth_failed_once(rig: Rig) -> None:
    rig.password = "wrong"
    rig.build()
    first = rig.controller.execute(_cmd("p1"))
    assert first.outcome is CommandOutcome.AUTH_FAILED
    assert len(rig.auth_callbacks) == 1
    logins = rig.arc.calls.count(const.LOGIN)
    second = rig.controller.execute(_cmd("p1"))
    assert second.outcome is CommandOutcome.AUTH_FAILED
    assert rig.arc.calls.count(const.LOGIN) == logins
    assert len(rig.auth_callbacks) == 1
    assert rig.arm_calls() == 0


def test_auth_already_failed_opens_no_socket(rig: Rig) -> None:
    rig.auth_failed = True
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.AUTH_FAILED
    assert rig.arc.calls == []


def test_unavailable_hub_rejects_without_a_socket(rig: Rig) -> None:
    rig.available = False
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.reason == "unavailable"
    assert rig.arc.calls == []


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (ArmCommand(mode="p1", areas=()), "unknown_area"),
        (ArmCommand(mode="p1", areas=(7,)), "unknown_area"),
        (ArmCommand(mode="X", areas=(0,)), "unsupported_mode"),  # type: ignore[arg-type]
    ],
)
def test_invalid_commands_are_rejected(rig: Rig, command, reason) -> None:
    result = rig.controller.execute(command)
    assert result.outcome is CommandOutcome.REJECTED
    assert result.reason == reason
    assert rig.arc.calls == []


def test_stop_during_the_wait_returns_promptly(rig: Rig) -> None:
    rig.arc.suppress_arm_events = True
    rig.build(wait_timeout=10.0)
    started = time.monotonic()
    thread, out = _run_in_thread(lambda: rig.controller.execute(_cmd("T")))
    assert _wait(lambda: rig.arm_calls() == 1)
    rig.stop.set()
    thread.join(3)
    assert out[0].outcome is CommandOutcome.FAILED
    assert out[0].reason == "unloading"
    assert time.monotonic() - started < 3


def test_stop_before_sending_sends_nothing(rig: Rig) -> None:
    rig.stop.set()
    result = rig.controller.execute(_cmd("T"))
    assert result.reason == "unloading"
    assert rig.arc.calls == []


def test_history_is_redacted_and_bounded(rig: Rig) -> None:
    def boom() -> Any:
        raise RuntimeError(f"cannot talk to {PASSWORD}")

    rig.build(transport_factory=boom)
    for _ in range(25):
        rig.controller.execute(_cmd("p1"))
    history = rig.controller.history()
    assert len(history) == 20
    dump = json.dumps(rig.controller.diagnostics())
    assert PASSWORD not in dump
    assert "***" in dump
    assert history[-1]["outcome"] == "failed"
    assert rig.controller.diagnostics()["command_spec"] == ARM_METHOD


def test_history_records_the_command_without_parameters(rig: Rig) -> None:
    rig.controller.execute(_cmd("p1"))
    entry = rig.controller.history()[0]
    assert entry["outcome"] == "confirmed"
    assert entry["areas"] == [0, 1, 2]
    assert "params" not in entry
    assert "Mode" not in json.dumps(entry)


def test_only_control_py_names_the_arm_method() -> None:
    root = Path(__file__).resolve().parents[1] / "custom_components" / "dahua_arc"
    holders = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if ARM_METHOD in path.read_text(encoding="utf-8")
    }
    assert holders == {"protocol/control.py"}


def test_extract_area_zones() -> None:
    inventory = {
        "candidate_configs": {
            "AlarmSubSystem": {
                "ok": True,
                "response": {
                    "result": True,
                    "params": {
                        "table": [
                            {"AreaId": 1, "Enable": True, "Name": "A", "Zone": [7, 3]},
                            {"AreaId": 2, "Enable": False, "Name": "B", "Zone": [9]},
                            {"AreaId": 3, "Enable": True, "Name": "C", "Zone": ["x"]},
                            {"Enable": True, "Name": "D", "Zone": [3, 3]},
                        ]
                    },
                },
            }
        }
    }
    assert extract_area_zones(inventory) == {0: [3, 7], 2: [], 3: [3]}
    assert set(extract_arm_areas(inventory)) == set(extract_area_zones(inventory))
    assert extract_area_zones({}) == {}
