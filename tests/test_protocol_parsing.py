"""Parsing and classification helpers of the protocol layer."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from custom_components.dahua_arc.protocol import cgi, files, inventory, util
from custom_components.dahua_arc.protocol.snapshot import SnapshotClient

ROOT = Path(__file__).resolve().parents[1]


# -- file download payloads -----------------------------------------------


def test_split_json_prefix_handles_escaped_quotes_and_nesting() -> None:
    meta = {"a": 'x"}y', "n": {"deep": [1, {"b": "\\"}]}}
    payload = json.dumps(meta).encode() + b"\xff\xd8JPEG"
    obj, rest = files.split_json_prefix(payload)
    assert obj == meta
    assert rest == b"\xff\xd8JPEG"


@pytest.mark.parametrize(
    "payload", [b"\xff\xd8raw", b"{not json}", b'{"open": "string', b"[1, 2]"]
)
def test_split_json_prefix_leaves_non_json_untouched(payload: bytes) -> None:
    assert files.split_json_prefix(payload) == (None, payload)


def test_jpeg_helpers() -> None:
    assert files.is_jpeg(b"\xff\xd8\xff\xe0data")
    assert not files.is_jpeg(b"\\xff\\xd8")  # the old literal bug
    assert not files.is_jpeg(b"\xff\xd8")
    jpeg, info = files.extract_embedded_jpeg(b"hdr\xff\xd8\xff\xe0body\xff\xd9tail")
    assert jpeg == b"\xff\xd8\xff\xe0body\xff\xd9"
    assert info["prefix_bytes"] == 3
    assert info["suffix_bytes"] == 4
    assert files.extract_embedded_jpeg(b"\xff\xd8\xff no end")[0] is None


def test_extract_download_length_scans_known_shapes() -> None:
    assert files.extract_download_length({"params": {"fileSize": "2048"}}) == 2048
    assert files.extract_download_length({"length": 0, "result": {"size": 5}}) == 5
    assert files.extract_download_length({"params": {}}) is None


# -- CGI -------------------------------------------------------------------


def test_cgi_decodes_utf8_names_and_falls_back_to_latin1() -> None:
    greek = "table.Alarm[0].Name=Σαλόνι\n".encode()
    assert cgi.decode_cgi_text(greek).endswith("Σαλόνι\n")
    assert cgi.decode_cgi_text(b"Caf\xe9") == "Café"


def test_parse_alarm_config() -> None:
    text = (
        "table.Alarm[0].Name=Kitchen Window\n"
        "table.Alarm[0].SenseMethod=MultiIOTransmitterP\n"
        "table.Alarm[12].Name=Zone13\n"
        "unrelated=1\n"
    )
    records = cgi.parse_alarm_config(text)
    assert records == {
        0: {"Name": "Kitchen Window", "SenseMethod": "MultiIOTransmitterP"},
        12: {"Name": "Zone13"},
    }
    with pytest.raises(RuntimeError, match="Unexpected CGI response"):
        cgi.parse_alarm_config("Error 401")


# -- Alarm[] classification --------------------------------------------------


def _cfg(**fields: str) -> dict[str, str]:
    base = {"Enable": "true", "Level1": "1", "Level2": "0", "Slot": "0"}
    return {**base, **fields}


def test_record_classification() -> None:
    placeholder = _cfg(
        Name="Zone12", SenseMethod="Default", Level1="-1", Level2="-1", Slot="-1"
    )
    assert not inventory.record_is_physical(placeholder)
    assert inventory.classify_alarm_record(placeholder) == "placeholder"
    assert inventory.classify_alarm_record(_cfg(SenseMethod="PIRCam")) == "motion"
    assert (
        inventory.classify_alarm_record(
            _cfg(SenseMethod="MultiIOTransmitterP", Level2="3")
        )
        == "multiio_input"
    )
    assert inventory.classify_alarm_record(_cfg(SenseMethod="AlarmBell")) == "siren"


def test_peripheral_detection_ignores_user_editable_names() -> None:
    keyfob = _cfg(SenseMethod="RemoteControl", Name="Front Door")
    assert inventory.record_is_peripheral_or_placeholder(keyfob)
    # A contact sensor renamed to something without "contact" is still an input.
    renamed = _cfg(SenseMethod="MagneticContact", Name="Anna")
    assert not inventory.record_is_peripheral_or_placeholder(renamed)
    board = _cfg(SenseMethod="MultiIOTransmitterP", Level2="0")
    assert inventory.record_is_peripheral_or_placeholder(board)
    wired = _cfg(SenseMethod="MultiIOTransmitterP", Level2="2")
    assert not inventory.record_is_peripheral_or_placeholder(wired)


def test_redaction_covers_credentials_contacts_and_network() -> None:
    raw = {
        "Name": "Kitchen",
        "SerialNo": "S1",
        "SN": "S2",
        "WifiPassword": "p",
        "User": {"Name": "owner"},
        "UserName": "owner",
        "PhoneNumber": "+30000",
        "EmailAddress": "a@example.invalid",
        "IPAddress": "198.51.100.1",
        "Latitude": 1.0,
        "AccessToken": "t",
        "OperatorInfo": {"Nick": "n"},
        "keepAliveInterval": 60,
        "list": [{"MAC": "00:11"}],
    }
    redacted = inventory.redact_sensitive(raw)
    assert redacted["Name"] == "Kitchen"
    assert redacted["keepAliveInterval"] == 60
    text = json.dumps(redacted)
    for secret in ("S1", "S2", '"p"', "owner", "+30000", "example", "198.51", "00:11"):
        assert secret not in text
    assert redacted["OperatorInfo"] == {"Nick": inventory.REDACTED}


def test_extract_radio_devices_links_repeater_children() -> None:
    records = {
        1: _cfg(SenseMethod="ProRepeater", Name="Repeater", Level1="1"),
        2: _cfg(SenseMethod="PIRCam", Name="Garden PIR", Level1="2"),
    }
    inv = {
        "candidate_configs": {
            "_AirFlyDeviceMap_": {
                "ok": True,
                "response": {
                    "params": {
                        "table": {
                            "DeviceInfo": [
                                {"ShotAddr": 1, "State": 1, "SN": "REP"},
                                {
                                    "ShotAddr": 2,
                                    "State": 1,
                                    "SN": "PIR",
                                    "ParentNodeId": "REP",
                                    "ModelName": "ARD1731",
                                },
                            ]
                        }
                    }
                },
            }
        }
    }
    devices = inventory.extract_radio_devices(inv, records, {2: "Garden"})
    assert devices[2].parent_level1 == 1
    assert devices[2].model == "ARD1731"
    assert devices[2].area_hint == "Garden"
    assert devices[2].device_key.startswith("radio:")
    assert "PIR" not in devices[2].device_key


def test_snapshot_parse_states() -> None:
    states = SnapshotClient.parse_states(
        {
            "result": True,
            "params": {
                "States": [
                    {"Index": 8, "AlarmState": "Alarm", "OnlineState": 1},
                    {"Index": 0},
                    "garbage",
                    {
                        "Index": 9,
                        "AlarmState": "Normal",
                        "SensorState": {"Tamper": 1, "LowPowerState": 0},
                    },
                ]
            },
        }
    )
    assert [(s["array_pos"], s["alarm_state"]) for s in states] == [(7, 1), (8, 5)]
    assert states[1]["tamper"] == 1
    with pytest.raises(RuntimeError, match="no States"):
        SnapshotClient.parse_states({"result": True, "params": {"States": []}})


def test_timestamps_are_timezone_aware() -> None:
    parsed = util.parse_timestamp(util.timestamp())
    assert isinstance(parsed, datetime)
    assert parsed.tzinfo is not None
    assert util.parse_timestamp(None) is None
    assert util.parse_timestamp("not a time") is None


def test_protocol_layer_does_not_import_home_assistant() -> None:
    """The protocol package must stay extractable into a standalone library."""
    code = (
        "import sys, types\n"
        "sys.modules['homeassistant'] = None\n"
        "root = sys.argv[1]\n"
        "for name, path in (('custom_components', 'custom_components'),"
        " ('custom_components.dahua_arc', 'custom_components/dahua_arc')):\n"
        "    mod = types.ModuleType(name); mod.__path__ = [f'{root}/{path}']\n"
        "    sys.modules[name] = mod\n"
        "import custom_components.dahua_arc.protocol.realtime\n"
        "import custom_components.dahua_arc.protocol.cgi\n"
        "import custom_components.dahua_arc.research.wpan\n"
        "import custom_components.dahua_arc.research.pircam\n"
        "import custom_components.dahua_arc.research.detector_test\n"
    )
    subprocess.run([sys.executable, "-c", code, str(ROOT)], check=True)
