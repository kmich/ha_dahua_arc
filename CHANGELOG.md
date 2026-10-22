# Changelog

All notable user-visible changes to Dahua ARC will be documented in this file.

The project uses semantic versioning for release tags where practical.

## [Unreleased]

### Added

- Internal groundwork for arming through a paired keyfob driven by relay entities (design in `docs/arm-control/06-keyfob-relay.md`): the arm controller can issue a command through a sender instead of an RPC, and a new `KeyfobSender` presses one of three mapped buttons (Home, Away, Disarm; never SOS) once, never retrying, with the result confirmed from the ARC's events. Nothing in Home Assistant uses it yet, so there is no behaviour change.

### Documentation

- Design note `docs/arm-control/06-keyfob-relay.md`: arming and disarming the ARC from Home Assistant by pressing a spare paired keyfob through relay-driven button entities, with confirmation from the ARC's own events. Design only; nothing is implemented yet.

### Documentation

- Recorded the arm/disarm search results in `docs/arm-control/02-protocol.md` (all disarm-only trials, the key sweep, login client types, capabilities and user fields, and what is left) and clarified in the README why the integration cannot arm or disarm.

### Changed

- `scripts/arm_probe.py` (developer tool, not shipped): new disarm-only shapes built on the `Zones` key, the one parameter name the ARC recognised in the key sweep (`zones-mode`, `zones-armmode`, `zones-objects`, `zones-mode-1based`, and code-bearing `zones-password`, `zones-usernumber`, `zones-userid`, `zones-user-object`).

### Changed

- `scripts/arm_probe.py` (developer tool, not shipped): a read-only `--sweep-keys` mode calls two getters on one `Web3.0` session with about 50 candidate parameter key names and reports the keys whose error differs from the baseline, i.e. names the ARC recognises. It stops at once on any transport error.

### Changed

- `scripts/arm_probe.py` (developer tool, not shipped): the client-type probe now tries only `Web3.0` and `Local`, and refuses `NetKeyboard` and `Dahua3.0` client types, because a NetKeyboard login matches a Dahua-exploit IPS signature and gets the traffic blocked.

### Changed

- `scripts/arm_probe.py` (developer tool, not shipped): `--client-type` sets the DHIP login client type, and the read-only `--probe-client-types` mode logs in under several client types and calls the failing getters once each, to see whether the arm methods depend on how the session identifies itself.

### Changed

- `scripts/arm_probe.py` (developer tool, not shipped): `--prompt-code` asks for an alarm-user code without echo for code-bearing disarm-only shapes; the code is never printed, logged or stored.

## [0.4.5] - 2026-10-22

### Added

- Diagnostics now include the structure of `AlarmUserManager.getUserInfoAll` (`arm_state_probe.user_info_structure`): only key names, value types and counts, to see which fields an ARC user has. Every value is dropped, so no user names, codes or passwords are recorded.

## [0.4.4] - 2026-10-22

### Added

- Diagnostics now include `AlarmRegion.getCaps` and the method names (plus signature/help metadata) of the `AlarmUserManager` and `AlarmKeyboard` services (`arm_state_probe.alarm_region_caps`, `user_service_methods`, `user_service_metadata`), to find out how the ARC models arming users. Only `listMethod` and metadata calls are made for those services; no user data or codes are read.
- `scripts/arm_probe.py`: a standalone, never-shipped script for supervised single-call arm/disarm trials against a real ARC (developer tool; not part of the integration).

## [0.4.3] - 2026-10-21

### Added

- Diagnostics now include a read-only getter probe (`arm_state_probe.getter_probe`): `AlarmRegion.getArmMode`, `getZoneArmMode` and `getUserArmModeAuth` are called with several guessed parameter shapes and every result is recorded, to learn how the ARC addresses areas and zones. Only `get*` methods are called; nothing is changed on the ARC.

## [0.4.2] - 2026-10-21

### Fixed

- The arm-method metadata probe added in 0.4.1 was rejected by the ARC (`Request invalid param!`). It now tries several parameter shapes on one method, records every result under `arm_state_probe.method_metadata_probe`, and uses the first shape that works for the rest. Metadata only; no discovered method is called.

## [0.4.1] - 2026-10-21

### Added

- Groundwork for opt-in arm/disarm control (design in `docs/arm-control/`): a new **Allow Home Assistant to arm and disarm the ARC** option (off by default), an optional Home Assistant code stored only as a salted hash, standard alarm control panels, and a repair when arm control is on without a code.
- Diagnostics now include `system.methodSignature` and `system.methodHelp` results for the arm-related methods of the `alarm`, `alarmSubSystem`, `alarmSubregion` and `AlarmRegion` services (`arm_state_probe.method_metadata`), to help identify the ARC's arm command. Metadata only; no discovered method is called.

### Known limitations

- No arm or disarm command exists yet. The ARC's arm RPC has not been verified on hardware, so turning the option on only raises a repair saying arm control is not yet supported; no panels are created and nothing is sent to the ARC. The integration stays read-only.

## [0.4.0] - 2026-10-09

### Added

- Read-only alarm state from the ARC's `AlarmLocal` events: a system **Alarm** binary sensor and an **alarm** binary sensor per enabled Dahua area. An alarm stays on until the area is disarmed or the ARC sends `AlarmClear`, and its attributes list the zones that triggered it. Diagnostics include the alarm tracker.
- README section with example automations for arm-state and alarm alerts.

### Known limitations

- Alarms are verified on hardware only for an instant zone while armed Home. Entry-delay zones, 24-hour zones and PIR-camera alarms are expected to behave the same but are not yet verified.
- An alarm that starts while Home Assistant is disconnected from the ARC is not reported.

## [0.3.1] - 2026-09-29

### Fixed

- Arm state is now known at startup and after a reconnect instead of staying unknown until the next arm or disarm. It is read from the ARC's `AreaArmMode` table on every connect and periodic resync, which also corrects a missed arm event. A table read never overrides a newer event.

### Changed

- Away (`T`) is now verified on hardware.

## [0.3.0] - 2026-09-29

### Added

- Read-only arm state from the ARC's arm/disarm events: a system **Arm state** sensor, an **arm state** sensor per enabled Dahua area (with forced-arm and bypassed-zone attributes), and a **Last arming failure** sensor listing the open zones that refused an arm. States are unknown until the first arm or disarm after startup or a reconnect, because the ARC reports changes only.
- Diagnostics always include the arm-state tracker and a fresh read of the candidate arm-state tables and arm-related RPC method names.

## [0.2.0] - 2026-09-25

### Fixed

- A silently dead realtime connection (ARC power loss, pulled cable) is now detected within about two keepalive intervals. Inputs become unavailable and the integration reconnects, instead of showing stale state as live for up to an hour.
- Credentials rejected while running now start a reauthentication flow, and all background logins stop so the ARC account is not locked by repeated retries.
- A different ARC answering at the configured address now raises a repair issue instead of an endless reauthentication loop.
- Startup cleanup of pre-public v0.3 phantom entities no longer deletes a real input's entity when that input is briefly missing from the snapshot or was renamed.
- Zone names with non-Latin characters (for example Greek) are now decoded as UTF-8 instead of being garbled.
- Research mode: the PIRCam HTTP image fallback never succeeded, and JSON metadata containing escaped quotes broke file downloads.
- The device's "Visit" link now includes a non-default HTTP port.
- Running area matching from the options while the ARC was offline no longer wipes stored decisions.

### Changed

- Password fields are masked. The reconfigure form no longer sends the stored password to the browser; leave it empty to keep it.
- Diagnostics redact more personal data (user names, contact details, network addresses, locations, tokens). Research-only configuration tables are read only in research mode.
- Research mode offers detector-test buttons for every paired PIR camera, not just one hard-coded camera. Existing button entities keep their IDs.
- The docs now say that research mode's detector-test buttons write to the ARC.
- Area matching recognises possessive labels generically ("Annas Window" → "Anna Office"). The household-specific names are gone and the default confidence is 90% everywhere.
- The options flow keeps existing area decisions unless "Re-evaluate zones that already have a decision" is ticked.
- A zone event now redraws only the entities it affects. Diagnostic counters refresh every 30 seconds, and volatile attributes are no longer written to the recorder.
- Research-oriented diagnostic sensors are disabled by default for new installations. The last-event and last-frame sensors are proper timestamps. "Last realtime error" and "DHIP reconnects" stay available while offline.
- Stale devices that are no longer paired with the ARC can be deleted from the device page.
- Internal restructuring: the HA-independent `protocol/` and opt-in `research/` packages replace `api.py`, and the vendored DHIP transport gained public request helpers.
- CI runs one test suite, with end-to-end tests against an in-process fake ARC and a coverage floor. The release workflow reuses the validation workflow.

## [0.1.0] - 2026-09-21

### Added

- Local-only, read-only Home Assistant integration for Dahua ARC3800H alarm-input state.
- CGI discovery of the configured `Alarm[]` table.
- DHIP TCP/5000 authoritative snapshots and realtime `AlarmInputSourceSignal` processing.
- Wired MultiIO input support and verified primary wireless sensor inputs.
- Startup reconciliation, fragmented snapshot handling, duplicate-event handling, keepalive, reconnect, and stale-state rejection.
- Home Assistant config flow and reauthentication.
- Diagnostics with sensitive-data redaction.
- Repairs support.
- Smart Home Assistant area matching with persisted per-zone decisions and protection for manual user area assignments.
- Logical child-device handling for MultiIO inputs on Home Assistant 2026.9+.
- Experimental PIRCam / LowRateWPAN research tooling behind an explicit opt-in feature flag.
- Python 3.14 / Home Assistant 2026.9 runtime tests, protocol tests, Ruff checks, security lint, package validation, Hassfest, and HACS validation.
- Contributor guide with protocol-evidence, privacy, compatibility, and testing requirements.
- Structured GitHub issue forms for bugs, feature requests, and new device/protocol observations.
- Pull request checklist.
- Release automation with tag/manifest validation, dynamic package naming, preflight validation, changelog notes, and GitHub Release creation/update.

### Known limitations

- No alarm-control-panel entity is exposed because partition arm/disarm state and control are not yet verified.
- Wireless PIR motion is not claimed as a production-supported path.
- Research-mode PIRCam / LowRateWPAN behavior does not feed production wired-zone state.
- Live hardware restart/outage and upgrade behavior should still be validated on each relevant ARC/firmware combination before relying on the integration for alarm-dependent automations.

[Unreleased]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.5...HEAD
[0.4.5]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/kmich/ha_dahua_arc/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/kmich/ha_dahua_arc/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/kmich/ha_dahua_arc/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kmich/ha_dahua_arc/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kmich/ha_dahua_arc/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kmich/ha_dahua_arc/releases/tag/v0.1.0
