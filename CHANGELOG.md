# Changelog

All notable user-visible changes to Dahua ARC will be documented in this file.

The project uses semantic versioning for release tags where practical.

## [Unreleased]

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

[Unreleased]: https://github.com/kmich/ha_dahua_arc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kmich/ha_dahua_arc/releases/tag/v0.1.0
