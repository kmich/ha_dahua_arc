# 03 — Home Assistant integration design

## 1. Opt-in

| Constant (`const.py`) | Type | Default | Meaning |
|---|---|---|---|
| `CONF_ENABLE_ARM_CONTROL = "enable_arm_control"` | bool | `False` | Master switch. Off: no panel entities, no `ArmController`, no write RPC |
| `CONF_ARM_CODE_HASH = "arm_code_hash"` | str \| None | `None` | `pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>` of the HA code |
| `CONF_CODE_DISARM_REQUIRED = "code_disarm_required"` | bool | `True` | Only meaningful when a code is set |
| `CONF_CODE_ARM_REQUIRED = "code_arm_required"` | bool | `False` | Only meaningful when a code is set |
| `CONF_ARM_MODES = "arm_modes"` | list[str] | `["armed_home", "armed_away"]` | Which arm services to offer. `armed_night` (`p2`) is listed only once verified |

All of these are **options** (`entry.options`), changed through the options
flow, which already reloads the entry (`OptionsFlowWithReload`).

`PLATFORMS` gains `Platform.ALARM_CONTROL_PANEL`. Its `async_setup_entry`
returns at once when the option is off or `hub.arm_control is None`, as
`button.py` does for research mode.

Turning the option off leaves any existing panel registry entries in place,
shown by HA as "no longer provided". Customizations survive a toggle, in line
with the README's "entity is kept" policy. They are not deleted automatically.

## 2. Entities

### 2.1 Which panels exist

| Condition | Panels |
|---|---|
| 0 enabled areas (`AlarmSubSystem` unreadable) | none; a log warning. Arm control needs areas |
| 1 enabled area | **system panel only** (it controls that area) |
| ≥ 2 enabled areas | system panel **and** one panel per enabled area |

### 2.2 Identity

| Panel | `unique_id` | Name (`has_entity_name=True`) | Expected `entity_id` |
|---|---|---|---|
| System | `{uid}_alarm_panel` | `None` (uses device name) | `alarm_control_panel.arc3800h` |
| Area | `{uid}_area_{area_id}_alarm_panel` | translation `area_alarm_panel` → `"{area}"` | `alarm_control_panel.arc3800h_ground_floor` |

`{uid}` is `entry_uid(entry)` and `area_id` is `ArmArea.area_id` (one-based),
the same scheme as the existing `{uid}_area_{area_id}_arm_state`. All panels
attach to the root ARC device (`root_device_info`), like the other arm and alarm
entities.

### 2.3 Base class

```text
DahuaArcAlarmPanel(DahuaArcArmStateEntity, AlarmControlPanelEntity)
  - inherits arm-listener wiring and availability (hub.available)
  - available = hub.available and hub.arm_control is not None and not hub.auth_failed
  - _pending: Literal["arming", "disarming"] | None      # set only while a command runs
  - _target_areas: tuple[int, ...]                       # all enabled areas, or one
```

### 2.4 State mapping

Precedence, top wins:

| # | Condition | `alarm_state` |
|---|---|---|
| 1 | a command from this panel is in flight, target ≠ `D` | `ARMING` |
| 2 | a command from this panel is in flight, target = `D` | `DISARMING` |
| 3 | any target area `alarm is True` | `TRIGGERED` |
| 4 | any target area state unknown | `None` (HA shows Unknown) |
| 5 | all target areas `disarmed` | `DISARMED` |
| 6 | all target areas `armed_home` | `ARMED_HOME` |
| 7 | all target areas `armed_away` | `ARMED_AWAY` |
| 8 | all target areas `armed_partial_2` | `ARMED_NIGHT` |
| 9 | target areas differ (system panel only) | `ARMED_CUSTOM_BYPASS` + attribute `mixed: true` |

Notes:

- `ARMING`/`DISARMING` here mean "a command is being confirmed". They are
  cleared in a `finally` block whatever the outcome. If Phase 0 (Q9) finds
  exit-delay events, phase 3 maps them to `ARMING` and entry delay to `PENDING`.
- A disarmed area can be in alarm (24-hour zones, tamper). Rule 3 comes before
  rule 5 on purpose.
- The existing **Arm state** enum sensor is unchanged and still reports
  `mixed`. Automations that need per-area truth should use the area sensors.

### 2.5 Features and code

| Property | Value |
|---|---|
| `supported_features` | `ARM_HOME` and `ARM_AWAY` by default; `ARM_NIGHT` only if `armed_night` is in `CONF_ARM_MODES` (after verification). Never `TRIGGER`, `ARM_VACATION` or `ARM_CUSTOM_BYPASS` |
| `code_format` | `CodeFormat.NUMBER` when a code hash is set, else `None` |
| `code_arm_required` | `CONF_CODE_ARM_REQUIRED` when a code is set, else `False` |
| `changed_by` | `"Home Assistant"` after a confirmed command from this integration; otherwise the event's `TriggerMode` (for example `Remote`, `Keyfob`, `Keypad`) once Q8 is answered |

Code check, inside the entity, before anything is sent:

1. No code hash configured → accept.
2. Disarm and `code_disarm_required`, or arm and `code_arm_required` → the code
   must be present and `arm_code.verify(code, hash)` must be true (PBKDF2 with
   `hmac.compare_digest`).
3. A failure raises `ServiceValidationError(translation_key="invalid_code")`.
   Nothing reaches the ARC.
4. Five wrong codes within 60 s lock the panel for 60 s (`invalid_code_locked`),
   which slows brute force from a shared dashboard.

The code is an **HA-side** gate. It is not the ARC user PIN. If Phase 0 shows
the ARC requires a password in the command parameters, the controller uses the
configured ARC login password (already stored in `entry.data`); the HA code is
never sent to the ARC.

### 2.6 Attributes

| Attribute | Panels | Source | Recorded? |
|---|---|---|---|
| `areas` | system | target area names | yes |
| `mixed` | system | rule 9 | yes |
| `area_states` | system | `{name: state}` | no (`_unrecorded_attributes`) |
| `area_id` | area | `ArmArea.area_id` | no |
| `open_zones` | all | **live**: flat, sorted list of zone **names** in the target areas whose input is currently active (from `StateEngine` zone state and `extract_area_zones`). Empty list when none; `None` when zone state is unavailable | no |
| `last_arming_failure` / `last_arming_failure_open_zones` | all | tracker, same helper as `sensor.py` (`_failure_attributes`) | no |
| `last_command` | all | `{at, mode, outcome, confirmed_by, error}` of the last command from this panel | no |
| `forced`, `bypassed_zones` | area | as on the area arm-state sensor | no |

`open_zones` is informational. HA never blocks an arm because of it, because the
ARC decides (some zones may be excluded from Home mode). It exists so the UI can
warn **before** the user presses Arm (see 04).

## 3. Actions

### 3.1 Standard actions (phase 2)

`alarm_control_panel.alarm_disarm`, `alarm_arm_home`, `alarm_arm_away` (and
`alarm_arm_night` once `p2` is verified). They map to:

| HA method | `ArmCommand.mode` | `areas` |
|---|---|---|
| `async_alarm_disarm` | `D` | panel's target areas |
| `async_alarm_arm_home` | `p1` | panel's target areas |
| `async_alarm_arm_away` | `T` | panel's target areas |
| `async_alarm_arm_night` | `p2` | panel's target areas |

Flow inside each method:

```text
check availability (raise HomeAssistantError "arm_unavailable")
check code (raise ServiceValidationError)
if every target area already has the requested state -> return (no RPC)
self._pending = arming|disarming; self.async_write_ha_state()
try:
    result = await hass.async_add_executor_job(hub.arm_control.execute, cmd)
finally:
    self._pending = None; self.async_write_ha_state()
raise the mapped error unless result.outcome is CONFIRMED
```

The "already in that state" short-circuit avoids pointless RPCs and makes
repeated automation calls idempotent.

### 3.2 Custom entity action (phase 3, after Q7 is answered)

`dahua_arc.arm` registered with `async_register_entity_service`, for the panel
entities only:

| Field | Type | Required | Notes |
|---|---|---|---|
| `mode` | select: `home`, `away` (`night` once verified) | yes | |
| `force` | bool | no, default `false` | arm despite open zones (ARC `Profile: Force`) |
| `code` | string | per code policy | same check as the standard actions |

Needs `services.yaml`, `strings` under `services` in `translations/en.json`, and
an `icons.json` entry (`"services": {"arm": {"service": "mdi:shield-lock"}}`),
so Hassfest passes.

Zone **bypass** as a standalone action is out of scope for this project.

## 4. Errors (`translations/en.json` → `exceptions`)

Raise `HomeAssistantError(translation_domain=DOMAIN, translation_key=...,
translation_placeholders=...)` so messages are translated and appear as frontend
toasts. Wording is final; it is reviewed in 04.

| Key | Class | Message |
|---|---|---|
| `invalid_code` | `ServiceValidationError` | "The code is incorrect." |
| `invalid_code_locked` | `ServiceValidationError` | "Too many incorrect codes. Try again in {seconds} seconds." |
| `code_required` | `ServiceValidationError` | "A code is required." |
| `arm_unavailable` | `HomeAssistantError` | "Arm and disarm are unavailable while the ARC is offline." |
| `arm_busy` | `HomeAssistantError` | "Another arm or disarm command is still running. Try again in a few seconds." |
| `arm_refused_open_zones` | `HomeAssistantError` | "The ARC refused to arm {areas}: {zones} open. Close them, or use Force arm." |
| `arm_refused` | `HomeAssistantError` | "The ARC refused to arm {areas}." |
| `arm_not_permitted` | `HomeAssistantError` | "The ARC user configured in Home Assistant is not allowed to arm or disarm. Use an ARC user with arm/disarm rights." |
| `arm_unconfirmed` | `HomeAssistantError` | "The ARC accepted the command but did not confirm the change within {seconds} seconds. Check the panel before trying again." |
| `arm_unreachable` | `HomeAssistantError` | "Could not reach the ARC to send the command: {error}" |
| `arm_auth_failed` | `HomeAssistantError` | "The ARC rejected the stored credentials. Reauthenticate the integration." |
| `arm_failed` | `HomeAssistantError` | "The ARC rejected the command (error {code}: {message})." |

## 5. Options flow changes

`_behavior_schema` (options step `init`) gains:

```text
enable_arm_control: bool  (default from options, False)
```

When it is ticked, the flow continues to a new step `arm_control`, before the
existing area-match step when that is also enabled. The steps chain: `init` →
`arm_control` (if enabled) → `area_match` (if enabled) → `area_preview`.

Step `arm_control` schema:

| Field | Selector | Default | Validation |
|---|---|---|---|
| `arm_code` | `TextSelector(type=PASSWORD)`; never pre-filled | empty | empty = keep the existing hash; digits only, 4–8 long |
| `clear_arm_code` | bool | `False` | removes the hash |
| `code_disarm_required` | bool | `True` | — |
| `code_arm_required` | bool | `False` | — |
| `arm_modes` | `SelectSelector(multiple, options=[armed_home, armed_away] (+ armed_night when verified))` | both | at least one |
| `acknowledge_control` | bool | `False` | must be `True` the first time arm control is turned on (`errors={"base": "acknowledge_required"}`) |

On submit, hash a non-empty `arm_code` with `arm_code.hash_code()` and store only
`arm_code_hash`. The plain code is dropped from the options dict before
`async_create_entry`.

The **initial config flow** (`behavior` step) does not offer arm control. It is
an options-only feature, enabled after the user has seen the read-only
integration working. This keeps first-time setup unchanged.

## 6. Repairs

| Issue id | When | Severity | Fixable | Fix |
|---|---|---|---|---|
| `{entry_id}_arm_control_without_code` | `enable_arm_control` and no `arm_code_hash` and not `arm_code_ack_no_code` | WARNING | yes | One-step repair flow. Its description tells the user to set a code under **Configure** (a repair flow cannot open the options flow itself). Submitting it means "keep without code": persist `arm_code_ack_no_code=True` and delete the issue. Setting a code later clears the flag |

Create or delete it in `_update_repair_issues`, next to the research-mode
issue. `repairs.async_create_fix_flow` dispatches on the suffix.

## 7. Diagnostics

`hub.diagnostics()` gains:

```text
"arm_control": None                                  # when disabled
"arm_control": {
    "enabled": true,
    "command_spec": "<method name>",
    "in_flight": false,
    "history": [ CommandResult as dict, last 20, params redacted ],
}
```

`diagnostics.py` must redact `arm_code_hash` from options (add it to the
redaction set). A test asserts that neither the ARC password nor a test code
appears anywhere in the diagnostics JSON.

## 8. Logging and audit

- `INFO` per command: `"ARC arm command %s areas=%s origin=%s -> %s (%s)"`, with
  mode, area names, HA context id, outcome and `confirmed_by` or the error.
- `WARNING` on `UNCONFIRMED`, `FAILED`, `AUTH_FAILED`.
- Never log params, the code or the password.
- The HA logbook already records the panel's state change with the calling
  user's context. No custom logbook platform is needed.

## 9. Security notes for users (README text, phase 4)

- Arm control is off by default. Enabling it lets anything that can call HA
  actions (automations, scripts, dashboards, voice assistants, MCP clients)
  disarm the alarm unless a code is required.
- Set a disarm code, and do not expose the panel entities to Assist or other
  conversation agents unless you require a code.
- Prefer a dedicated ARC user with arm/disarm rights only, not the ARC admin
  account.
- DHIP is unencrypted. Keep the ARC on a trusted, segmented LAN (already in
  `SECURITY.md`).

## 10. Files touched (HA side)

| File | Change |
|---|---|
| `const.py` | new `CONF_*`, defaults, `ISSUE_ARM_CONTROL_WITHOUT_CODE`, `Platform.ALARM_CONTROL_PANEL` |
| `alarm_control_panel.py` | **new** platform |
| `arm_code.py` | **new**: `hash_code`, `verify_code`, attempt limiter |
| `__init__.py` | pass the option to `ArcHub`; repair issue |
| `hub.py` | construct `ArmController`; diagnostics; stop |
| `config_flow.py` | options step `arm_control`; chaining |
| `repairs.py` | new fix flow |
| `diagnostics.py` | redact `arm_code_hash` |
| `translations/en.json` | options step, errors, `entity.alarm_control_panel.area_alarm_panel`, `exceptions`, issue, (phase 3) `services` |
| `services.yaml`, `icons.json` | phase 3 only |
| `manifest.json` | version bump |
