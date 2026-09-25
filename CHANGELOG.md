# Changelog

All notable user-visible changes to Dahua ARC will be documented in this file.

The project uses semantic versioning for release tags where practical.

## [Unreleased]

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

[Unreleased]: https://github.com/kmich/ha_dahua_arc/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kmich/ha_dahua_arc/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kmich/ha_dahua_arc/releases/tag/v0.1.0
