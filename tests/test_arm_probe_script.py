"""scripts/arm_probe.py against the in-process fake ARC."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from tests.fake_arc import ARM_METHOD, FakeArc

PASSWORD = "test-only"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "arm_probe.py"


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("arm_probe", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def allow_fake_service(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "ALLOWED_SERVICES", ("FakeArc",))


def _arc(modes: list[str] | None = None) -> FakeArc:
    modes = modes or ["D", "D", "D"]
    return FakeArc(
        password=PASSWORD,
        inventory={"AreaArmMode": {"Areas": [{"Mode": m} for m in modes]}},
    )


def _args(probe: ModuleType, params: dict, *extra: str):
    return probe.parse_args(
        [
            "--host",
            "192.0.2.10",
            "--user",
            "admin",
            "--method",
            ARM_METHOD,
            "--params-json",
            json.dumps(params),
            "--i-am-at-the-panel",
            "--yes",
            "--listen",
            "0.5",
            *extra,
        ]
    )


def _run(probe: ModuleType, arc: FakeArc, params: dict, *extra: str, ask=None):
    args = _args(probe, params, *extra)
    lines: list[str] = []
    kwargs = {"ask": ask} if ask else {}
    with arc.patch():
        try:
            transcript = probe.run(args, PASSWORD, emit=lines.append, **kwargs)
        finally:
            arc.close_timers()
    return transcript, lines


def test_disarm_on_disarmed_area_is_sent_once_and_recorded(probe) -> None:
    arc = _arc()
    transcript, lines = _run(probe, arc, {"Mode": "D", "Areas": [0]})
    assert arc.arm_requests == [{"Mode": "D", "Areas": [0]}]
    assert transcript["reply"]["result"] is True
    assert transcript["modes_before"] == transcript["modes_after"] == ["D", "D", "D"]
    assert transcript["modes_changed"] is False
    assert [e["Code"] for e in transcript["events"]] == ["AreaArmModeChange"]
    assert any("event AreaArmModeChange" in line for line in lines)
    assert PASSWORD not in json.dumps(transcript)


def test_arm_like_parameters_refused_without_allow_arm(probe) -> None:
    arc = _arc()
    with pytest.raises(probe.GuardError, match="arm-like"):
        _run(probe, arc, {"Mode": "p1", "Areas": [0]})
    assert arc.arm_requests == []


def test_params_without_disarm_mode_refused(probe) -> None:
    arc = _arc()
    with pytest.raises(probe.GuardError, match="disarm mode"):
        _run(probe, arc, {"Areas": [0]})
    assert arc.arm_requests == []


def test_refused_when_an_area_is_not_disarmed(probe) -> None:
    arc = _arc(["D", "p1", "D"])
    with pytest.raises(probe.GuardError, match="area 1: p1"):
        _run(probe, arc, {"Mode": "D", "Areas": [0]})
    assert arc.arm_requests == []


def test_allow_arm_sends_the_arm(probe) -> None:
    arc = _arc()
    transcript, _ = _run(probe, arc, {"Mode": "p1", "Areas": [0]}, "--allow-arm")
    assert arc.arm_requests == [{"Mode": "p1", "Areas": [0]}]
    assert transcript["modes_after"] == ["p1", "D", "D"]
    assert transcript["modes_changed"] is True
    assert transcript["allow_arm"] is True


def test_must_type_send_to_continue(probe) -> None:
    arc = _arc()
    args = _args(probe, {"Mode": "D", "Areas": [0]})
    args.yes = False
    with arc.patch(), pytest.raises(probe.GuardError, match="not confirmed"):
        probe.run(args, PASSWORD, emit=lambda _: None, ask=lambda _: "no")
    assert arc.arm_requests == []


def test_error_reply_is_recorded_and_never_retried(probe) -> None:
    arc = _arc()
    arc.arm_reply = "error"
    transcript, _ = _run(probe, arc, {"Mode": "D", "Areas": [0]})
    assert transcript["reply"]["result"] is False
    assert transcript["reply"]["error"]["code"] == arc.arm_error_code
    assert len(arc.arm_requests) == 1
    assert transcript["events"] == []


def test_only_alarm_services_may_be_called(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    for method in ("configManager.setConfig", "system.reboot", "magicBox.reboot"):
        with pytest.raises(probe.GuardError, match="not allowed"):
            probe.check_method(method)
    with pytest.raises(probe.GuardError, match=r"service\.method"):
        probe.check_method("AlarmRegion")
    for method in ("AlarmRegion.setArmMode", "alarm.setArmMode"):
        probe.check_method(method)


def test_invalid_params_json_refused(probe) -> None:
    arc = _arc()
    args = _args(probe, {})
    args.params_json = "{nope"
    with pytest.raises(probe.GuardError, match="valid JSON"), arc.patch():
        probe.run(args, PASSWORD, emit=lambda _: None)
    assert arc.calls == []


def test_refuses_to_run_away_from_the_panel(probe) -> None:
    with pytest.raises(SystemExit):
        probe.parse_args(
            ["--host", "h", "--user", "u", "--method", "a.b", "--params-json", "{}"]
        )


def test_main_needs_the_password_from_the_environment(
    probe, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("ARC_PASSWORD", raising=False)
    code = probe.main(
        [
            "--host",
            "192.0.2.10",
            "--user",
            "admin",
            "--method",
            ARM_METHOD,
            "--params-json",
            "{}",
            "--i-am-at-the-panel",
        ]
    )
    assert code == 2
    assert "ARC_PASSWORD" in capsys.readouterr().out


def test_transcript_redacts_identifying_fields(probe) -> None:
    redacted = probe.redact(
        {
            "session": 1,
            "Data": {
                "SN": "ABC",
                "OperatorInfo": {"Name": "x"},
                "NetClientAddr": "10.0.0.1",
                "Mode": "D",
            },
        }
    )
    assert redacted["session"] == "**REDACTED**"
    assert redacted["Data"]["SN"] == "**REDACTED**"
    assert redacted["Data"]["OperatorInfo"] == "**REDACTED**"
    assert redacted["Data"]["NetClientAddr"] == "**REDACTED**"
    assert redacted["Data"]["Mode"] == "D"


def test_script_is_not_in_the_release_package() -> None:
    from scripts import check_package

    shipped = [
        p.relative_to(check_package.ROOT).as_posix()
        for p in check_package.package_paths()
    ]
    assert all(not path.startswith("scripts/") for path in shipped)
    assert not any("arm_probe" in path for path in shipped)
