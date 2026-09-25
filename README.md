# Dahua ARC for Home Assistant

Local-only Home Assistant integration for Dahua ARC3800H alarm-input state. It uses CGI to discover the configured `Alarm[]` table and DHIP TCP/5000 for an authoritative snapshot and `AlarmInputSourceSignal` events. No cloud or SIA dependency is used for zone state.

In normal operation the integration is **read-only**: it never arms, disarms, triggers outputs or changes ARC configuration. The single exception is the opt-in research mode's detector-test buttons (see [Research mode](#research-mode)).

## Supported scope

- Wired MultiIO inputs and verified primary wireless sensor inputs appear as binary sensors. Input state continues to update while the ARC is disarmed.
- A 256-entry snapshot establishes state on startup and reconciles missed events. The engine handles fragmented snapshots, duplicate events, keepalive, reconnect, and stale-state rejection.
- A liveness watchdog marks inputs unavailable and reconnects if the realtime stream goes silent for about two keepalive intervals (about two minutes), for example when the ARC loses power or its network cable is pulled. Stale state is never shown as live.
- Paired wireless/radio devices remain physical Home Assistant devices. Each MultiIO input is a logical Home Assistant child device of its physical board on HA 2026.9+, allowing independent area placement.
- The integration does **not** expose an alarm-control-panel entity: Dahua partition arm/disarm state and control are not verified.
- PIRCam / LowRateWPAN research tools are isolated behind `enable_research_features`, which defaults to **off**. Wireless PIR motion remains unverified.

## Installation

This repository targets Home Assistant 2026.9+ and Python 3.14. Add `https://github.com/kmich/ha_dahua_arc` to HACS as a custom **Integration** repository, or install manually by copying `custom_components/dahua_arc` into the Home Assistant configuration's `custom_components` directory. Restart Home Assistant, then use **Settings → Devices & services → Add integration → Dahua ARC**. The ARC should be reachable only over a trusted LAN.

Use a release package only after its CI checks have passed. Back up Home Assistant before upgrading an existing installation. The integration preserves legacy entity unique IDs based on the existing config-entry ID while recording the physical serial for future identity checks. New installations require a reported ARC serial and use it as the config-entry unique ID.

## Smart areas

Area matching is optional. The setup/options flow enumerates existing Home Assistant areas and previews normalized name, alias, token, Dahua hint, and fuzzy matches (90% minimum confidence by default). High-confidence decisions are stored by immutable `Alarm[]` index as `zone_area_decisions` and reused at runtime; a rename cannot silently rematch an existing zone. Newly discovered indexes receive new decisions on setup. Re-running matching from the options flow keeps existing decisions unless **Re-evaluate zones that already have a decision** is ticked, and the preview always shows the result before it is saved. Auto-placement only changes an unassigned child device or an area the integration can identify as its own previous assignment. A user-changed or user-cleared area is protected.

## Research mode

Leave research mode disabled for normal wired-zone use. When explicitly enabled, it exposes the experimental PIRCam snapshot camera, starts LowRateWPAN research polling, reads extra configuration tables for diagnostics, and adds **Start/Stop detector test** buttons for every paired PIR camera. These features are not a reliable wireless motion path and do not feed production wired-zone state.

The detector-test buttons are the only feature that **writes** to the ARC: they toggle the selected PIR camera's sensitivity-test flag through `LowRateWPAN.setAccessoryParam`, and a started test stops automatically after 120 seconds. They never arm, disarm or trigger sirens/outputs. A repair issue is raised while research mode is enabled, with a one-click fix to turn it off.

## Troubleshooting and recovery

- If setup reports **Cannot connect**, check ARC LAN reachability, CGI HTTP port (normally 80), and DHIP port (normally 5000).
- If setup reports **Invalid authentication**, verify the local ARC credentials. Reauthentication updates the existing entry without replacing its entity IDs.
- If the ARC password changes while Home Assistant is running, the integration stops all background logins immediately (to avoid locking the ARC account) and asks for new credentials through a reauthentication notification.
- If an ARC is replaced, do not reconfigure an existing serial-bound entry to the replacement unit. Add it as a new integration after reviewing automations and areas. If a different ARC answers at the configured address, the entry is not loaded and a repair issue explains why.
- If an input becomes unavailable, inspect the integration diagnostics for DHIP snapshot/realtime health (including watchdog disconnects) and wait for automatic reconnect; do not infer `off` from an unavailable entity.
- If a real input disappears from the ARC, its entity is kept (shown unavailable) so customizations are not lost. Devices no longer paired with the ARC can be deleted from the device page.
- If an auto-assigned area is unsuitable, change or clear it in Home Assistant. That manual choice will be preserved.
- For a bad upgrade, restore the Home Assistant backup and the last known-good integration version, then restart.

## Development checks

The main CI runs Python 3.14 compile, Ruff (including security and async rules), protocol unit tests, end-to-end tests against an in-process fake ARC that speaks DHIP, Home Assistant 2026.9 runtime/platform/config-flow tests, Hassfest, HACS validation and package checks. A coverage floor is enforced (see `pyproject.toml`). The release workflow reuses the same validation. A passing CI run does not replace an on-hardware soak test for the ARC3800H.

Code layout: `protocol/` holds the Home Assistant-independent CGI/DHIP engine, `research/` holds the opt-in PIRCam/LowRateWPAN features, `hub.py` ties them together, and the platform modules plus `entity.py` form the Home Assistant adapter.

The vendored DHIP transport retains its own MIT license under `custom_components/dahua_arc/vendor/`.


## Contributing

Bug reports, feature requests, and new device/protocol observations have dedicated GitHub issue forms. For development setup, protocol-evidence requirements, privacy rules, compatibility expectations, and the pull-request checklist, see [CONTRIBUTING.md](CONTRIBUTING.md).

Release history and known limitations are tracked in [CHANGELOG.md](CHANGELOG.md). Security-sensitive reports should follow [SECURITY.md](SECURITY.md) and must not be filed as public issues.
