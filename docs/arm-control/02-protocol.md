# 02 — Protocol: known facts, unknowns, and Phase 0 discovery

The integration's protocol knowledge is evidence-based (see `CONTRIBUTING.md`).
The arm/disarm **command** has never been observed on this firmware. This
document separates what is verified from what is assumed, and defines the
discovery work that must come before any command code is final.

## 1. Verified on ARC3800H hardware (already used by the read path)

| Fact | Source in code |
|---|---|
| Arm changes arrive as `AreaArmModeChange` (one per area, `Index` = zero-based area index) plus a `GlobalAreaArmModeChange` summary | `protocol/arming.py` |
| Refused arms arrive as `ArmingFailure` per area and `GlobalArmingFailure`, with `Data.Abnormal.detail[].ZoneAbnormal[]` listing open zones | `parse_abnormal_zones` |
| `Data.Mode` codes: `D` disarmed, `p1` Home, `T` Away (all verified); `p2` assumed second partial (not verified) | `ARM_MODES` |
| `Data.Profile` is `Auto` or `Force` (forced past open zones); `Data.IsGlobal` marks a whole-system arm; `Data.TriggerMode` names the origin (e.g. `Remote`) | test fixtures |
| Current mode is readable from `configManager.getConfig name=AreaArmMode` → `Areas[i].Mode`, one row per `AlarmSubSystem` row | `parse_area_arm_modes` |
| Areas come from `AlarmSubSystem[]` (`Enable`, `AreaId`, `Name`, `Zone[]`) | `extract_arm_areas`, `extract_zone_area_hints` |
| Disarming ends an alarm; `AlarmClear` (`Type: AlarmArea`) follows | `arming.py` docstring |
| DHIP instance services use `<service>.factory.instance` → method with `object` → `<service>.destroy` | `research/detector_test.py` |
| Diagnostics already list the methods of `alarm`, `alarmSubSystem`, `alarmSubregion`, `AlarmRegion` via `listMethod` (`arm_state_probe.method_lists`) | `InventoryRpcClient.collect_arm_state_probe` |

## 2. Not known: Phase 0 must answer these

| # | Question | Why it matters |
|---|---|---|
| Q1 | Which RPC method arms/disarms areas on this firmware? | Everything depends on it |
| Q2 | Is it a static method or an instance method (`factory.instance` / `destroy`)? | Session sequence |
| Q3 | Exact parameter shape: mode spelling (`T`/`p1`/`D` or something else such as `Total`/`Partial1`), area addressing (zero-based index, one-based `AreaId`, list or single), whole-system form | Builder |
| Q4 | Does it require a user password or PIN in the parameters, in addition to the session login? | Credential handling |
| Q5 | Reply shape on success; on refusal (open zones); on a user without arm rights; on an unknown area | Reply parser and error mapping |
| Q6 | Is a refusal reported in the RPC reply, as an `ArmingFailure` event, or both? | Controller's `REFUSED` logic |
| Q7 | Is there a force flag (arm despite open zones, giving `Profile: Force`)? | Phase 3 force arm |
| Q8 | What `TriggerMode` (and `Data.Name`) do events carry for a DHIP-originated arm? | Attributing `changed_by` to Home Assistant |
| Q9 | Does `AreaArmModeChange` fire at the **start** or the **end** of an exit delay? Are there separate exit/entry-delay event codes? | Whether to show `arming` / `pending` states |
| Q10 | Does `p2` exist on this firmware, and what does the keypad/app call it? | Exposing `armed_night` |
| Q11 | Does disarm need the same method with `D`, or a separate method? | Builder |
| Q12 | Does the method accept arming several areas in one call, and is that atomic? | One call vs one call per area |

## 3. Candidate methods (hypotheses, unverified)

These come from Dahua NetSDK naming and the RPC services the hub already
enumerates. **Do not call any of these until the Phase 0 step that allows it.**

| Candidate | Rationale | Risk |
|---|---|---|
| `AlarmRegion.*` arm-mode setter (for example `AlarmRegion.setArmMode`) | The ARC-generation hubs expose `AlarmRegion.getChannelsState` / `getAccessoryInfo`. NetSDK's ARC-class arm API (`CLIENT_SetAlarmRegionInfo` with an arm-mode request carrying arm type, user password and an area list) most likely maps here | Low, if the signature confirms it |
| `alarm.*` arm-mode setter | Older Dahua alarm-host generation | Medium: may exist but act globally |
| `alarmSubSystem.*` activation setter (instance service) | Older per-subsystem arm API | Medium: semantics may differ (active/inactive rather than modes) |
| `configManager.setConfig name=AreaArmMode` | The mode is stored in a config table | **High, rejected by default.** It rewrites a whole table, may bypass the ARC's arming checks (open zones, exit delay, event log), and may not trigger the same events. Use only with the owner's explicit approval and evidence that the ARC treats it as a real arm |

The method names above are illustrative. The real names are whatever
`listMethod` returns on the user's ARC.

## 4. Phase 0: discovery plan

Each step has an exit artefact. Step 4 is the only one that changes arm state,
and it runs only under the owner's direct control.

### Step 0.1: read the existing diagnostics (no code change)

The owner downloads diagnostics from **Settings → Devices & services → Dahua
ARC → ⋮ → Download diagnostics**. The implementer extracts:

- `arm_state_probe.method_lists` for `alarm`, `alarmSubSystem`,
  `alarmSubregion`, `AlarmRegion`,
- `event_catalog.codes` (any delay or arming-related codes not yet handled),
- `extended_rpc_inventory.service_catalog.services` (in case the arm service
  has another name, for example something with `Arm`, `Defence` or `Area` in it).

**Exit artefact:** the list of candidate setter methods actually present.

### Step 0.2: method introspection (read-only, safe to ship)

Extend `collect_arm_state_probe` so that, for every method whose name contains
`set`, `arm`, `Arm`, `Mode` or `Active` in those services, it also requests
`system.methodSignature` and `system.methodHelp`. These are metadata calls; the
research inventory already makes them. This is a normal, small PR.

**Exit artefact:** signatures/help text for the candidate methods (they may be
empty; the firmware often returns nothing useful).

### Step 0.3: capture a real arm from an official client (best evidence)

If the ARC's local web UI offers arm/disarm, the owner opens the browser
developer tools (Network tab, filter `RPC2`), arms Home on one area, disarms,
and arms Away. Dahua web UIs use the same JSON-RPC methods over HTTP `/RPC2`,
so the request bodies show method, params and replies verbatim.

If the web UI has no arm control, alternatives in order of preference:

1. Dahua ConfigTool / SmartPSS "alarm host" control on the LAN, captured with
   Wireshark (DHIP on TCP 5000 is plaintext JSON after the 32-byte header).
2. Public NetSDK documentation for the arm request structure, to construct the
   request by hand in step 0.4.

**Exit artefact:** sanitized request/response pairs (secrets, serials and
session IDs replaced) committed to `tests/fixtures/arm_control/` with a short
README describing ARC model and firmware.

### Step 0.4: controlled hardware trial (owner present at the panel)

A **standalone script** `scripts/arm_probe.py`, **not** part of the integration
package (`scripts/check_package.py` should confirm it is not shipped). It uses
the vendored `DHIPTransport` and:

- requires `--host`, `--user`, a password from an environment variable, an
  explicit `--method`, `--params-json` and the flag `--i-am-at-the-panel`,
- attaches a second session to `eventManager.attach` and prints every arm,
  alarm and failure event for 20 s after the call,
- reads `AreaArmMode` before and after,
- never loops, never retries, and sends exactly one command per run.

Trial matrix (one test area, everyone informed, siren-safe conditions):

| Run | Command | Expect to record |
|---|---|---|
| T1 | arm Home, one area, all zones closed | reply, events, table, `TriggerMode` |
| T2 | disarm that area | reply, events, `AlarmClear` absence |
| T3 | arm Away, whole system | global vs per-area events, exit-delay behaviour (Q9) |
| T4 | disarm whole system | — |
| T5 | arm Home with a zone open | refusal shape (Q5, Q6) |
| T6 | arm with an ARC user lacking arm rights | permission error code |
| T7 | (if Q7 looks possible) forced arm with a zone open | `Profile: Force`, bypass list |
| T8 | (if `p2` exists) arm `p2` | Q10 |

**Exit artefact:** a filled-in evidence table (below) plus fixtures for each
run. This becomes the "Hardware / protocol evidence" section of the
implementation PR.

### Evidence table to fill in

| Item | Value | Run |
|---|---|---|
| Arm method | | T1 |
| Instance service? | | T1 |
| Params, arm Home one area | | T1 |
| Params, disarm | | T2 |
| Params, whole system | | T3 |
| Password/PIN in params? | | T1 |
| Success reply | | T1 |
| Refusal reply | | T5 |
| Refusal also as `ArmingFailure` event? | | T5 |
| No-permission error code | | T6 |
| `TriggerMode` / `Data.Name` for a DHIP arm | | T1 |
| Event timing vs exit delay | | T3 |
| Force flag | | T7 |
| `p2` | | T8 |
| Firmware version | | — |

## 5. `CommandSpec`: where the evidence goes

All of the above lands in one object in `protocol/control.py`, so the rest of
the code (controller, entities, tests) can be written and reviewed before Phase
0 finishes:

```text
ARM_COMMAND_SPEC = CommandSpec(
    method=<from evidence>,
    needs_instance=<from evidence>,
    build_params=<function: ArmCommand, password -> dict, from evidence>,
    parse_reply=<function: reply -> (ok, code, message, open_zones), from evidence>,
)
```

Until the evidence exists, tests use a `FAKE_COMMAND_SPEC` matching the fake
ARC (method name `FakeArc.setArmMode`). That name must never be sent to real
hardware: the hub refuses to construct a controller with the fake spec outside
tests.

## 6. Error mapping

Fill in the codes from T5/T6. Unknown codes fall through to the generic
message, which includes the code.

| ARC condition | `CommandOutcome` | Translation key (see 03) |
|---|---|---|
| Open zones / not ready | `REFUSED` | `arm_refused_open_zones` |
| User lacks arm rights | `FAILED` | `arm_not_permitted` |
| Bad PIN/password in params | `FAILED` | `arm_bad_device_password` |
| Unknown area | `FAILED` | `arm_failed` |
| Anything else | `FAILED` | `arm_failed` (with code and message) |
| Login rejected | `AUTH_FAILED` | `arm_auth_failed` |
| Socket/connect error, reply timeout | `FAILED` | `arm_unreachable` |
