"""Protocol data models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Zone:
    index: int
    name: str = ""
    enabled: bool = False
    level1: int | None = None
    level2: int | None = None
    parent_name: str = ""
    sense_method: str = ""
    classification: str = ""
    is_multiio: bool = False
    sensor_type: str = ""
    termination: str = ""
    defence_area_type: str = ""
    area_hint: str = ""
    snapshot_index: int | None = None
    online_state: int | None = None
    raw_alarm_state: int | None = None
    tamper: int | None = None
    low_power: int | None = None
    active: bool | None = None
    parent_sn: str = ""
    event_exchannel: int | None = None
    last_source: str = ""
    last_action: str = ""
    last_changed: str = ""
    last_event_seq: int = 0
    raw_config: dict[str, str] | None = None
