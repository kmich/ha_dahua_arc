# Arm/disarm control — design and implementation package

Status: **Implemented and Verified on Hardware (v1.0.0)**. Native panel arm/disarm control via `AlarmRegion.setArmMode` is implemented, fully tested, and confirmed on ARC3800H hardware. Opt-in arm control exposes whole-system and per-area `alarm_control_panel` entities with cryptographic PIN authentication.

## What was built

Opt-in control of the ARC's arm state from Home Assistant:

- arm **Away** (`T`), arm **Home** (`p1`) and **disarm** (`D`), for the whole
  system and for each enabled Dahua area,
- as standard Home Assistant `alarm_control_panel` entities, so the built-in
  alarm-panel card, tile card, more-info dialog, automations and logbook all work
  without custom frontend code,
- with a command whose result is **confirmed by the ARC's own events**, not
  assumed from the RPC reply,
- turned off by default, with an optional Home Assistant code for disarm (and,
  if wanted, arm).

Still gated on hardware evidence: forced arm past open zones, the second
partial mode (`p2`), and exit/entry-delay states.

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [01-architecture.md](01-architecture.md) | Components, where the write path lives, threading, the command state machine, sequence diagrams |
| 2 | [02-protocol.md](02-protocol.md) | What is known about the ARC's arm RPC, how Phase 0 found `AlarmRegion.setArmMode`, and the hardware evidence |
| 3 | [03-ha-integration.md](03-ha-integration.md) | Entities, unique IDs, state mapping, options flow, code handling, actions, errors, repairs, diagnostics |
| 4 | [04-frontend.md](04-frontend.md) | What the user sees: options screens, more-info dialog, dashboard cards (with YAML), error messages, notifications |
| 5 | [05-implementation-plan.md](05-implementation-plan.md) | Phased work breakdown, file-by-file changes, tests, acceptance criteria, risks |

Documents 01, 03, 04 and 05 are the design as written before the build. They
are kept for history, so they describe some finished work as still to do.

## Decisions in this design

Each is argued in the linked document.

| Decision | Choice | Where |
|---|---|---|
| Opt-in | New option `enable_arm_control`, default **off**. Off means no panel entities and no write-capable RPC is ever sent | 03 |
| Entity type | `alarm_control_panel`: one for the system, plus one per area when more than one area is enabled | 03 |
| Transport | A **short-lived dedicated DHIP session per command**, never the realtime or snapshot session | 01 |
| Concurrency | One command in flight per hub. No automatic retry once a command has been sent | 01 |
| Confirmation | Success only when `AreaArmModeChange` arrives for every target area, or a fallback `AreaArmMode` table read shows the requested mode | 01 |
| Refusal | An `ArmingFailure` for a target area fails the action with the open zones named | 01, 04 |
| Availability | Panels are unavailable whenever the realtime stream is down, because a command could not be confirmed | 03 |
| Code | Optional Home Assistant code, stored as a salted PBKDF2 hash in options. Required for disarm by default when a code is set | 03 |
| `p2` | Not exposed until verified on hardware. When it is, it maps to `armed_night` | 03 |
| Mixed areas | System panel reports `armed_custom_bypass`, with a `mixed: true` attribute | 03 |
| Protocol isolation | All RPC knowledge lives in a new HA-independent `protocol/control.py`. One `CommandSpec` holds the verified method name and parameter shape | 01, 02 |

## How the open questions were settled

1. **Mixed state.** The system panel reports `armed_custom_bypass` with a
   `mixed` attribute.
2. **Single-area systems.** Only the system panel is created.
3. **Code requirement.** A code isn't required. Without one, a repair issue warns
   until it is dismissed or a code is set.

## Standing rules

- Do not ship a new command or mode without hardware evidence (see 02). Building
  against the `CommandSpec` interface with a fake ARC is fine.
- Never send an arm/disarm RPC from discovery, diagnostics, setup, reload,
  options, or periodic code. Only a user or automation action may send one.
- Never retry an arm/disarm RPC automatically after it has been written to the
  socket.
- Never log or put into diagnostics the HA code, its hash, or the ARC password.
- `protocol/` must stay free of `homeassistant` imports (a test enforces this).
- Preserve every existing entity `unique_id`. New entities get new IDs only.
