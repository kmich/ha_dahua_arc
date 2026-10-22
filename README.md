# Dahua ARC for Home Assistant

Local-only Home Assistant integration for Dahua ARC3800H alarm-input state. It uses CGI to discover the configured `Alarm[]` table and DHIP TCP/5000 for an authoritative snapshot and `AlarmInputSourceSignal` events. No cloud or SIA dependency is used for zone state.

In normal operation the integration is **read-only**: it never arms, disarms, triggers outputs or changes ARC configuration. The single exception is the opt-in research mode's detector-test buttons (see [Research mode](#research-mode)).

## Supported scope

- Wired MultiIO inputs and verified primary wireless sensor inputs appear as binary sensors. Input state continues to update while the ARC is disarmed.
- A 256-entry snapshot establishes state on startup and reconciles missed events. The engine handles fragmented snapshots, duplicate events, keepalive, reconnect, and stale-state rejection.
- A liveness watchdog marks inputs unavailable and reconnects if the realtime stream goes silent for about two keepalive intervals (about two minutes), for example when the ARC loses power or its network cable is pulled. Stale state is never shown as live.
- Paired wireless/radio devices remain physical Home Assistant devices. Each MultiIO input is a logical Home Assistant child device of its physical board on HA 2026.9+, allowing independent area placement.
- Arm state is shown read-only, from the ARC's `AreaArmModeChange` events: an **Arm state** sensor for the whole system (`disarmed`, `armed_home`, `armed_away`, `armed_partial_2`, or `mixed` when areas differ) and a **<area> arm state** sensor for every enabled Dahua area. Attributes show whether an arm was forced past open zones and which zones were bypassed. A **Last arming failure** sensor records refused arm attempts and the open zones that caused them.
- The current arm state is read from the ARC's `AreaArmMode` table when the realtime stream connects or reconnects and on every periodic resync, so it is known at startup and a missed event is corrected. A table read never overrides a newer arm event. If the table cannot be read, arm state stays **unknown** until the next arm or disarm. Home (`p1`), Away (`T`) and disarm (`D`) are verified on hardware; the second partial mode (`p2`) is not yet. An unrecognised mode leaves the state unknown and appears in diagnostics.
- Alarms are shown read-only, from the ARC's `AlarmLocal` events: an **Alarm** binary sensor for the whole system and an **<area> alarm** binary sensor for every enabled Dahua area. An area's alarm turns on when a zone triggers it and stays on until the area is disarmed (or the ARC sends `AlarmClear`), even after the input restores. Attributes list the zones that triggered it, with times. Verified on hardware for an instant-zone intrusion while armed Home.
- The ARC has no readable "alarm in progress" table, so alarm state comes from events alone. It is **unknown** until the area's arm state is known, then off until an alarm arrives. An alarm that is on when the realtime stream reconnects stays on until a table read shows the area disarmed. An alarm that *starts* while Home Assistant is disconnected is not reported.
- The integration does **not** expose an alarm-control-panel entity and cannot arm or disarm. The ARC's arm call could not be found over the local DHIP connection on an ARC3800H: more than a dozen disarm-only trials and a parameter-name sweep all returned the same generic error, and the ARC appears to accept arm requests only from its own internal/cloud channel. The evidence is recorded in [docs/arm-control/02-protocol.md](docs/arm-control/02-protocol.md#2c-arm-search-results-2026-10-22-what-was-tried-and-what-it-showed). Arming and disarming stay in the Dahua DMSS app or at a keypad, unless you build the optional [keyfob relay](#arming-with-a-keyfob-relay-optional-experimental).
- PIRCam / LowRateWPAN research tools are isolated behind `enable_research_features`, which defaults to **off**. Wireless PIR motion remains unverified.

## Installation

This repository targets Home Assistant 2026.9+ and Python 3.14. Add `https://github.com/kmich/ha_dahua_arc` to HACS as a custom **Integration** repository, or install manually by copying `custom_components/dahua_arc` into the Home Assistant configuration's `custom_components` directory. Restart Home Assistant, then use **Settings → Devices & services → Add integration → Dahua ARC**. The ARC should be reachable only over a trusted LAN.

Use a release package only after its CI checks have passed. Back up Home Assistant before upgrading an existing installation. The integration preserves legacy entity unique IDs based on the existing config-entry ID while recording the physical serial for future identity checks. New installations require a reported ARC serial and use it as the config-entry unique ID.

## Alerts and automations

The arm-state and alarm entities are ordinary Home Assistant entities, so alerts are plain automations. Entity IDs below assume the ARC device is named `ARC3800H`; check yours under **Settings → Entities**.

Notify when the alarm is triggered (`from: "off"` avoids a notification when Home Assistant restarts or reconnects):

```yaml
alias: Alarm triggered
triggers:
  - trigger: state
    entity_id: binary_sensor.arc3800h_alarm
    from: "off"
    to: "on"
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "Alarm"
      message: >
        {{ state_attr('binary_sensor.arc3800h_alarm', 'last_alarm_zone') }}
        ({{ state_attr('binary_sensor.arc3800h_alarm', 'alarm_areas') | join(', ') }})
```

Notify on every arm or disarm:

```yaml
alias: Arm state changed
triggers:
  - trigger: state
    entity_id: sensor.arc3800h_arm_state
    not_from: [unknown, unavailable]
    not_to: [unknown, unavailable]
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "Alarm"
      message: "{{ state_translated(trigger.to_state) }}"
```

To hear about refused arm attempts, trigger on any change of **Last arming failure**; its `open_zones` attribute lists the zones that blocked the arm. Per-area alarm and arm-state entities work the same way.

## Arming with a keyfob relay (optional, experimental)

The ARC's own arm command is not available to Home Assistant, but the ARC does accept arm and disarm from a **paired wireless keyfob**. If a *spare* keyfob's Home, Away and Disarm buttons are wired to relays that Home Assistant controls, the integration can press them for you. The ARC then arms itself with all its own checks, and the result is confirmed from the ARC's own events like everything else. This has **not been verified on hardware yet**; follow the acceptance checklist in [docs/arm-control/06-keyfob-relay.md](docs/arm-control/06-keyfob-relay.md) before relying on it.

1. Pair a spare keyfob to the ARC in DMSS and try its buttons by hand. Do not modify the keyfob you use every day.
2. Bridge the spare's Home, Away and Disarm contacts with isolated outputs (a relay or opto-coupler, for example on an ESPHome board or a Shelly). Leave **SOS unwired**.
3. Make Home Assistant entities that pulse each relay: a `button`, a `switch` (the integration turns it on, holds it, and turns it off) or a `script`.
4. In the integration's options, enable arm control, set **How commands are sent** to *Paired keyfob pressed by relays*, and choose the entity for each button.

What to expect:

- Only the whole-system alarm panel exists in this mode: a keyfob arms the areas it is assigned to in the ARC, not areas one by one.
- One press per command, and it is **never repeated** automatically. If the ARC does not confirm in time, the action fails with a message saying the press may still have worked; check the panel before trying again.
- An arm refused because a zone is open reports the zone, as the ARC does.
- **Disarm is off by default.** Turn it on only if you want Home Assistant to be able to disarm the house, and keep a code set. A relay that is unavailable is never pressed, and a missing relay entity raises a repair.
- Treat the relay entities like the alarm panel itself: keep your Home Assistant account secured and do not expose them to voice assistants or shared dashboards.

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
- If an alarm is missed or never clears, check `arming.last_alarm`, `arming.unknown_clear_types` and `event_catalog.codes` in the diagnostics.
- If arm state stays unknown, check `arming.last_table_error` in the diagnostics. Every diagnostics download also re-reads the `AreaArmMode`/`DefenceStatus` tables, the arm-related RPC method names and the alarm-user field names (`arm_state_probe`): well over a hundred read-only calls, none of which changes anything on the ARC. User names, codes and passwords are never recorded; only field names and types.
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
