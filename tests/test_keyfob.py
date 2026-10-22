"""KeyfobSender and ArmController-with-a-sender against the in-process fake ARC."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from custom_components.dahua_arc.protocol import keyfob
from custom_components.dahua_arc.protocol.arming import ArmingTracker
from custom_components.dahua_arc.protocol.control import (
    FAKE_COMMAND_SPEC,
    ArmCommand,
    ArmController,
    CommandOutcome,
    SendFailure,
)
from custom_components.dahua_arc.protocol.keyfob import KeyfobButtons, KeyfobSender
from custom_components.dahua_arc.protocol.snapshot import SnapshotClient
from tests.fake_arc import ARM_METHOD, FakeArc

PASSWORD = "test-only"
NAMES = ["Living Room", "Garage", "Office"]
HOME, AWAY, DISARM = "button.keyfob_home", "button.keyfob_away", "button.keyfob_disarm"
ENTITY_MODE = {HOME: "p1", AWAY: "T", DISARM: "D"}
OFFICE_WINDOW = {"Index": 191, "Name": "Office Window", "Reason": "Open"}


def _cmd(mode: str = "p1", areas: tuple[int, ...] = (0, 1, 2)) -> ArmCommand:
    return ArmCommand(mode=mode, areas=areas, origin="ha:test")  # type: ignore[arg-type]


def _wait(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# -- KeyfobSender on its own -------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _sender(
    pressed: list[str] | None = None,
    *,
    buttons: KeyfobButtons | None = None,
    ready: Callable[[str], bool] = lambda _e: True,
    min_gap: float = 5.0,
    clock: Callable[[], float] | None = None,
    press: Callable[[str], None] | None = None,
) -> KeyfobSender:
    log = pressed if pressed is not None else []
    return KeyfobSender(
        buttons or KeyfobButtons(home=HOME, away=AWAY, disarm=DISARM),
        press or log.append,
        ready,
        min_gap=min_gap,
        clock=clock or time.monotonic,
    )


def test_each_mode_presses_its_own_button() -> None:
    pressed: list[str] = []
    sender = _sender(pressed, min_gap=0)
    for mode in ("p1", "T", "D"):
        assert sender.send(_cmd(mode)) is None
    assert pressed == [HOME, AWAY, DISARM]


def test_a_mode_with_no_button_presses_nothing() -> None:
    pressed: list[str] = []
    sender = _sender(pressed, buttons=KeyfobButtons(home=HOME))
    failure = sender.send(_cmd("T"))
    assert failure == SendFailure("mode_not_configured", "T")
    assert failure.issued is False
    assert sender.send(_cmd("p2")) == SendFailure("mode_not_configured", "p2")
    assert pressed == []


def test_an_unavailable_relay_is_not_pressed() -> None:
    pressed: list[str] = []
    sender = _sender(pressed, ready=lambda entity: entity != AWAY)
    assert sender.send(_cmd("T")) == SendFailure("relay_unavailable", AWAY)
    assert pressed == []
    assert sender.send(_cmd("p1")) is None  # the other relays still work


def test_a_second_press_inside_the_gap_is_refused() -> None:
    pressed: list[str] = []
    clock = Clock()
    sender = _sender(pressed, min_gap=5.0, clock=clock)
    assert sender.send(_cmd("p1")) is None
    clock.now += 4.9
    assert sender.send(_cmd("D")) == SendFailure("too_soon", "5s")
    clock.now += 0.2
    assert sender.send(_cmd("D")) is None
    assert pressed == [HOME, DISARM]


def test_a_press_that_raises_is_reported_issued_and_not_retried() -> None:
    calls: list[str] = []

    def press(entity: str) -> None:
        calls.append(entity)
        raise RuntimeError("service timed out")

    clock = Clock()
    sender = _sender(press=press, min_gap=5.0, clock=clock)
    failure = sender.send(_cmd("p1"))
    assert failure is not None
    assert failure.reason == "press_failed"
    assert failure.issued is True
    assert "service timed out" in (failure.message or "")
    assert calls == [HOME]
    # The press may have happened, so the gap applies to the next command too.
    assert sender.send(_cmd("p1")) == SendFailure("too_soon", "5s")
    assert calls == [HOME]


def test_buttons_cover_only_home_away_and_disarm() -> None:
    """There is no way to name an SOS button."""
    assert {f.name for f in dataclasses.fields(KeyfobButtons)} == {
        "home",
        "away",
        "disarm",
    }
    assert set(keyfob.MODE_BUTTON) == {"p1", "T", "D"}
    assert KeyfobButtons(home=HOME, disarm=DISARM).mapped_modes() == ["p1", "D"]


def test_description_lists_modes_but_not_entity_ids() -> None:
    description = _sender().describe()
    assert description == {
        "name": "keyfob",
        "mapped_modes": ["p1", "T", "D"],
        "min_gap_seconds": 5.0,
    }
    for entity in (HOME, AWAY, DISARM):
        assert entity not in json.dumps(description)


# -- ArmController with the sender, against the fake ARC ----------------------


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
    ready: bool = True
    raise_on_press: bool = False
    controller: ArmController | None = None
    sender: KeyfobSender | None = None

    def build(self, **kwargs: Any) -> ArmController:
        buttons = kwargs.pop(
            "buttons", KeyfobButtons(home=HOME, away=AWAY, disarm=DISARM)
        )
        min_gap = kwargs.pop("min_gap", 0.0)

        def press(entity: str) -> None:
            if self.raise_on_press:
                raise RuntimeError("press failed")
            self.arc.keyfob_press(ENTITY_MODE[entity])

        self.sender = KeyfobSender(
            buttons, press, lambda _e: self.ready, min_gap=min_gap
        )
        kwargs.setdefault("wait_timeout", 1.0)
        self.controller = ArmController(
            host="192.0.2.10",
            port=5000,
            username="admin",
            password=PASSWORD,
            sender=self.sender,
            tracker=self.tracker,
            read_table=lambda: self.snapshot.read_config("AreaArmMode"),
            hub_available=lambda: self.available,
            auth_failed=lambda: False,
            on_auth_failed=lambda exc: None,
            stop_event=self.stop,
            **kwargs,
        )
        return self.controller


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


def test_arm_home_is_confirmed_by_events_and_sends_no_rpc(rig: Rig) -> None:
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.CONFIRMED
    assert result.confirmed_by == "event"
    assert rig.arc.keyfob_presses == ["p1"]
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"p1"}
    # The keyfob path never writes an arm RPC to the ARC.
    assert rig.arc.arm_requests == []
    assert ARM_METHOD not in rig.arc.calls


def test_arm_away_then_disarm(rig: Rig) -> None:
    assert rig.controller.execute(_cmd("T")).outcome is CommandOutcome.CONFIRMED
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"T"}
    assert rig.controller.execute(_cmd("D")).outcome is CommandOutcome.CONFIRMED
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"D"}
    assert rig.arc.keyfob_presses == ["T", "D"]


def test_a_refused_arm_names_the_open_zones(rig: Rig) -> None:
    rig.arc.open_zones = [OFFICE_WINDOW]
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.REFUSED
    assert {z["zone"] for z in result.open_zones} == {"Office Window"}
    assert rig.arc.keyfob_presses == ["T"]
    assert {a.raw_mode for a in rig.tracker.areas.values()} == {"D"}


def test_lost_events_are_resolved_by_one_table_read(rig: Rig) -> None:
    rig.arc.suppress_arm_events = True
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.CONFIRMED
    assert result.confirmed_by == "table"
    assert rig.arc.keyfob_presses == ["p1"]  # still pressed only once


def test_no_effect_is_unconfirmed_and_never_pressed_again(rig: Rig) -> None:
    rig.arc.arm_takes_effect = False  # the keyfob press is "accepted", nothing happens
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.UNCONFIRMED
    assert result.reason == "timeout"
    assert rig.arc.keyfob_presses == ["p1"]


def test_a_second_command_while_one_is_waiting_is_rejected_busy(rig: Rig) -> None:
    rig.arc.arm_takes_effect = False
    results: list[Any] = []
    thread = threading.Thread(
        target=lambda: results.append(rig.controller.execute(_cmd("p1"))), daemon=True
    )
    thread.start()
    assert _wait(lambda: rig.controller.in_flight)
    busy = rig.controller.execute(_cmd("D"))
    assert busy.outcome is CommandOutcome.REJECTED
    assert busy.reason == "busy"
    thread.join(timeout=5)
    assert results[0].outcome is CommandOutcome.UNCONFIRMED
    assert rig.arc.keyfob_presses == ["p1"]


def test_an_unavailable_relay_rejects_before_any_press(rig: Rig) -> None:
    rig.ready = False
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.reason == "relay_unavailable"
    assert result.rpc_error_message == HOME
    assert rig.arc.keyfob_presses == []


def test_a_press_error_fails_the_command_without_a_retry(rig: Rig) -> None:
    rig.raise_on_press = True
    result = rig.controller.execute(_cmd("p1"))
    assert result.outcome is CommandOutcome.FAILED
    assert result.reason == "press_failed"
    assert "press failed" in (result.rpc_error_message or "")
    assert rig.arc.keyfob_presses == []


def test_the_minimum_gap_rejects_a_quick_second_command(rig: Rig) -> None:
    rig.build(min_gap=60.0)
    assert rig.controller.execute(_cmd("p1")).outcome is CommandOutcome.CONFIRMED
    result = rig.controller.execute(_cmd("D"))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.reason == "too_soon"
    assert rig.arc.keyfob_presses == ["p1"]


def test_a_mode_without_a_button_is_rejected(rig: Rig) -> None:
    rig.build(buttons=KeyfobButtons(home=HOME))
    result = rig.controller.execute(_cmd("T"))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.reason == "mode_not_configured"
    assert rig.arc.keyfob_presses == []


def test_existing_guards_still_apply_with_a_sender(rig: Rig) -> None:
    rig.available = False
    assert rig.controller.execute(_cmd("p1")).reason == "unavailable"
    rig.available = True
    assert rig.controller.execute(_cmd("p1", (9,))).reason == "unknown_area"
    assert rig.controller.execute(_cmd("p1", ())).reason == "unknown_area"
    rig.stop.set()
    assert rig.controller.execute(_cmd("p1")).reason == "unloading"
    assert rig.arc.keyfob_presses == []


def test_history_and_diagnostics_describe_the_sender(rig: Rig) -> None:
    rig.controller.execute(_cmd("p1"))
    diagnostics = rig.controller.diagnostics()
    assert diagnostics["command_spec"] is None
    assert diagnostics["sender"]["name"] == "keyfob"
    assert diagnostics["sender"]["mapped_modes"] == ["p1", "T", "D"]
    assert diagnostics["history"][-1]["outcome"] == "confirmed"
    dumped = json.dumps(diagnostics)
    assert PASSWORD not in dumped
    for entity in (HOME, AWAY, DISARM):
        assert entity not in dumped


def test_the_controller_needs_exactly_one_of_spec_or_sender(rig: Rig) -> None:
    common: dict[str, Any] = {
        "host": "192.0.2.10",
        "port": 5000,
        "username": "admin",
        "password": PASSWORD,
        "tracker": rig.tracker,
        "read_table": lambda: None,
        "hub_available": lambda: True,
        "auth_failed": lambda: False,
        "on_auth_failed": lambda exc: None,
        "stop_event": threading.Event(),
    }
    with pytest.raises(ValueError, match="exactly one"):
        ArmController(**common)
    with pytest.raises(ValueError, match="exactly one"):
        ArmController(
            **common,
            spec=FAKE_COMMAND_SPEC,
            sender=rig.sender,
            allow_fake_spec=True,
        )
