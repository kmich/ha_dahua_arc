"""scripts/arm_probe.py against the in-process fake ARC."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
    monkeypatch.setattr(probe, "ALLOWED_SERVICES", ("FakeArc", "AlarmRegion"))


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


def test_script_runs_without_home_assistant_installed() -> None:
    """It must not import the integration package (which needs Home Assistant)."""
    imports = [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not any("custom_components" in line for line in imports)
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--i-am-at-the-panel" in result.stdout


def test_transcript_does_not_keep_a_pin_in_the_params(probe) -> None:
    arc = _arc()
    transcript, _ = _run(probe, arc, {"Mode": "D", "Areas": [0], "PIN": "1234"})
    assert transcript["params"]["PIN"] == "**REDACTED**"
    assert "1234" not in json.dumps(transcript)


def test_every_builtin_shape_is_disarm_only(probe) -> None:
    for name, build in probe.SHAPES.items():
        params = build(3)
        assert probe.string_leaves(params) == ["D"], name
        # Passes the guard on an all-disarmed panel.
        probe.check_disarm_only(params, ["D", "D", "D", "D"])


def test_shape_option_sends_the_shape_for_the_chosen_area(probe) -> None:
    arc = _arc()
    args = probe.parse_args(
        [
            "--host",
            "192.0.2.10",
            "--user",
            "admin",
            "--method",
            ARM_METHOD,
            "--shape",
            "areas-mode",
            "--area",
            "2",
            "--i-am-at-the-panel",
            "--yes",
            "--listen",
            "0.3",
        ]
    )
    with arc.patch():
        try:
            probe.run(args, PASSWORD, emit=lambda _: None)
        finally:
            arc.close_timers()
    assert arc.arm_requests == [{"Mode": "D", "Areas": [2]}]


def test_shape_and_params_json_are_mutually_exclusive(probe) -> None:
    with pytest.raises(SystemExit):
        probe.parse_args(
            [
                "--host",
                "h",
                "--user",
                "u",
                "--shape",
                "areas-mode",
                "--params-json",
                "{}",
                "--i-am-at-the-panel",
            ]
        )


def test_list_shapes_prints_and_exits(probe, capsys) -> None:
    with pytest.raises(SystemExit) as stop:
        probe.parse_args(["--list-shapes"])
    assert stop.value.code == 0
    assert "areas-mode" in capsys.readouterr().out


CODE = "S3cretCode77"


def _code_args(probe: ModuleType, *extra: str):
    return probe.parse_args(
        [
            "--host",
            "192.0.2.10",
            "--user",
            "admin",
            "--method",
            ARM_METHOD,
            "--i-am-at-the-panel",
            "--yes",
            "--listen",
            "0.3",
            *extra,
        ]
    )


def _run_code(probe: ModuleType, arc: FakeArc, *extra: str, code: str = CODE):
    args = _code_args(probe, *extra)
    lines: list[str] = []
    with arc.patch():
        try:
            transcript = probe.run(
                args, PASSWORD, emit=lines.append, ask_code=lambda _prompt: code
            )
        finally:
            arc.close_timers()
    return transcript, lines


def test_code_reaches_the_arc_but_is_never_printed_or_stored(probe) -> None:
    arc = _arc()
    transcript, lines = _run_code(
        probe,
        arc,
        "--shape",
        "areas-usernumber",
        "--user-number",
        "3",
        "--prompt-code",
    )
    # The ARC got the real code, once.
    assert arc.arm_requests == [
        {"Mode": "D", "Areas": [0], "UserNumber": 3, "Password": CODE}
    ]
    # It appears nowhere that is printed or written.
    assert CODE not in "\n".join(lines)
    assert CODE not in json.dumps(transcript)
    assert transcript["params"]["Password"] == "**REDACTED**"
    assert any("**CODE**" in line for line in lines)  # the request is shown masked


@pytest.mark.parametrize(
    "shape", ["areas-password", "areas-userid", "areas-user-object"]
)
def test_code_shapes_send_the_code_under_the_expected_key(probe, shape) -> None:
    arc = _arc()
    _run_code(probe, arc, "--shape", shape, "--prompt-code")
    (request,) = arc.arm_requests
    leaves = probe.string_leaves(request)
    assert CODE in leaves
    assert request["Mode"] == "D"


def test_params_json_code_token_is_replaced_by_the_prompted_code(probe) -> None:
    arc = _arc()
    transcript, lines = _run_code(
        probe,
        arc,
        "--params-json",
        '{"Mode": "D", "Areas": [0], "Pwd": "__CODE__"}',
        "--prompt-code",
    )
    assert arc.arm_requests == [{"Mode": "D", "Areas": [0], "Pwd": CODE}]
    assert CODE not in "\n".join(lines)
    assert CODE not in json.dumps(transcript)


def test_code_echoed_in_a_reply_is_scrubbed(probe) -> None:
    assert probe.scrub(f'{{"echo": "{CODE}"}}', CODE) == '{"echo": "**CODE**"}'
    assert probe.scrub("nothing here", "") == "nothing here"


def test_empty_code_sends_nothing(probe) -> None:
    arc = _arc()
    with pytest.raises(probe.GuardError, match="no code"):
        _run_code(probe, arc, "--shape", "areas-password", "--prompt-code", code="  ")
    assert arc.arm_requests == []


def test_code_shape_still_obeys_the_disarm_guards(probe) -> None:
    arc = _arc(["D", "p1", "D"])
    with pytest.raises(probe.GuardError, match="area 1: p1"):
        _run_code(probe, arc, "--shape", "areas-password", "--prompt-code")
    assert arc.arm_requests == []


def test_a_code_shape_needs_prompt_code(probe) -> None:
    with pytest.raises(SystemExit):
        _code_args(probe, "--shape", "areas-password")


def test_prompt_code_needs_something_that_uses_it(probe) -> None:
    with pytest.raises(SystemExit):
        _code_args(probe, "--shape", "areas-mode", "--prompt-code")
    with pytest.raises(SystemExit):
        _code_args(probe, "--params-json", "{}", "--prompt-code")


def test_main_refuses_prompt_code_without_a_terminal(
    probe, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(probe.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setenv("ARC_PASSWORD", PASSWORD)
    code = probe.main(
        [
            "--host",
            "192.0.2.10",
            "--user",
            "admin",
            "--method",
            ARM_METHOD,
            "--shape",
            "areas-password",
            "--prompt-code",
            "--i-am-at-the-panel",
        ]
    )
    assert code == 2
    assert "needs a terminal" in capsys.readouterr().out


def test_code_shapes_are_disarm_only_with_the_code_masked(probe) -> None:
    for name, build in probe.CODE_SHAPES.items():
        masked = build(2, probe.CODE_PLACEHOLDER, 1)
        assert "D" in probe.string_leaves(masked), name
        probe.check_disarm_only(masked, ["D", "D", "D"])


def _probe_args(probe: ModuleType, *extra: str):
    return probe.parse_args(
        ["--host", "192.0.2.10", "--user", "admin", "--probe-client-types", *extra]
    )


def _client_type_arc() -> FakeArc:
    arc = _arc()
    arc.login_client_types = {"Web3.0", "Local"}
    arc.getters["AlarmRegion.getArmMode"] = lambda _p: {"armMode": [{"Mode": "D"}]}
    arc.getters["AlarmRegion.getZoneArmMode"] = lambda _p: {"Zones": [1]}
    # Only a Local session may call it; others get the generic error.
    arc.getter_client_types["AlarmRegion.getZoneArmMode"] = {"Local"}
    return arc


def test_client_type_probe_shows_which_type_the_arc_accepts(probe) -> None:
    arc = _client_type_arc()
    lines: list[str] = []
    args = _probe_args(probe)
    with arc.patch():
        results = probe.probe_client_types(args, PASSWORD, emit=lines.append)
    by_type = {r["client_type"]: r for r in results}
    assert list(by_type) == list(probe.CLIENT_TYPES)

    def outcome(entry, method, params):
        (call,) = [
            c for c in entry["calls"] if c["method"] == method and c["params"] == params
        ]
        return call

    web = by_type["Web3.0"]
    assert web["login"] == "ok"
    assert outcome(web, "AlarmRegion.getArmMode", {})["ok"] is True
    zone = outcome(web, "AlarmRegion.getZoneArmMode", {})
    assert zone["ok"] is False
    assert zone["error_code"] == -267976701
    local = by_type["Local"]
    zone_local = outcome(local, "AlarmRegion.getZoneArmMode", {})
    assert zone_local["ok"] is True
    assert zone_local["reply_keys"] == ["Zones"]
    # Only key names are kept: no reply value is stored.
    assert "[1]" not in json.dumps(results)
    assert any("Local" in line and "login ok" in line for line in lines)


def test_client_type_probe_is_read_only(probe) -> None:
    arc = _client_type_arc()
    with arc.patch():
        probe.probe_client_types(_probe_args(probe), PASSWORD, emit=lambda _m: None)
    assert arc.arm_requests == []
    sent = [c for c in arc.calls if c not in ("global.login", "global.logout")]
    assert sent
    assert all(c.rsplit(".", 1)[-1].startswith("get") for c in sent), sent


def test_client_type_probe_stops_on_a_lockout_risk(probe) -> None:
    arc = _client_type_arc()
    lines: list[str] = []
    with arc.patch():
        results = probe.probe_client_types(
            _probe_args(probe), "wrong-password", emit=lines.append
        )
    assert len(results) == 1  # one wrong-password login, then it stops
    assert results[0]["login"] == "failed (code 268632080)"
    assert any("Stopping" in line for line in lines)


def test_custom_client_types_are_used(probe) -> None:
    arc = _client_type_arc()
    args = _probe_args(probe, "--client-types", "Local, Web3.0")
    with arc.patch():
        results = probe.probe_client_types(args, PASSWORD, emit=lambda _m: None)
    assert [r["client_type"] for r in results] == ["Local", "Web3.0"]


def test_client_type_option_is_used_for_the_trial_login(probe) -> None:
    arc = _arc()
    arc.login_client_types = {"Local"}
    with pytest.raises(probe.LoginError), arc.patch():
        probe.run(_args(probe, {"Mode": "D", "Areas": [0]}), PASSWORD, emit=print)
    transcript, _ = _run(
        probe, arc, {"Mode": "D", "Areas": [0]}, "--client-type", "Local"
    )
    assert transcript["reply"]["result"] is True
    assert len(arc.arm_requests) == 1


def test_probe_mode_takes_no_shape_and_needs_no_panel_flag(probe) -> None:
    args = _probe_args(probe)
    assert args.probe_client_types is True
    with pytest.raises(SystemExit):
        _probe_args(probe, "--shape", "areas-mode")
    with pytest.raises(SystemExit):
        probe.parse_args(["--host", "h", "--user", "u", "--i-am-at-the-panel"])


def test_main_runs_the_client_type_probe_and_writes_a_transcript(
    probe, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    arc = _client_type_arc()
    monkeypatch.setenv("ARC_PASSWORD", PASSWORD)
    out = tmp_path / "ct.json"
    with arc.patch():
        code = probe.main(
            [
                "--host",
                "192.0.2.10",
                "--user",
                "admin",
                "--probe-client-types",
                "--out",
                str(out),
            ]
        )
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [r["client_type"] for r in written["results"]] == list(probe.CLIENT_TYPES)
    assert PASSWORD not in out.read_text(encoding="utf-8")
    assert "Local" in capsys.readouterr().out


def test_every_client_type_probe_call_is_a_getter(probe) -> None:
    for method, _params in probe.CLIENT_TYPE_GETTERS:
        assert method.rsplit(".", 1)[-1].startswith("get"), method


def test_default_client_types_are_the_two_safe_ones(probe) -> None:
    assert probe.CLIENT_TYPES == ("Web3.0", "Local")


@pytest.mark.parametrize(
    "name", ["NetKeyboard", "netkeyboard", "Dahua3.0", "Dahua3.0-Web3.0"]
)
def test_exploit_flagged_client_types_are_refused(probe, name) -> None:
    arc = _client_type_arc()
    with pytest.raises(probe.GuardError, match="IPS"):
        probe.check_client_type(name)
    # Refused by the argument parser, for both options ...
    with pytest.raises(SystemExit):
        _probe_args(probe, "--client-types", f"Web3.0,{name}")
    with pytest.raises(SystemExit):
        _code_args(probe, "--shape", "areas-mode", "--client-type", name)
    # ... and by the probe itself, before any connection is made.
    args = _probe_args(probe)
    args.client_types = [name]
    with pytest.raises(probe.GuardError), arc.patch():
        probe.probe_client_types(args, PASSWORD, emit=lambda _m: None)
    assert arc.calls == []


def test_ordinary_client_types_are_accepted(probe) -> None:
    for name in ("Web3.0", "Local", "web3.0"):
        probe.check_client_type(name)
