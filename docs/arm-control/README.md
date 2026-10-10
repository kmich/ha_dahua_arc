# Arm/disarm control — design package

Status: **design only, nothing implemented.** This folder is the handoff for the
agent (or person) who will implement arm/disarm control for the Dahua ARC
integration. Nothing here changes runtime behaviour. Until the work lands, the
integration stays read-only, as the README says.

## What is being built

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

Later phases, gated on hardware evidence: forced arm past open zones, the second
partial mode (`p2`), and exit/entry-delay states.

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [01-architecture.md](01-architecture.md) | Components, where the write path lives, threading, the command state machine, sequence diagrams |
| 2 | [02-protocol.md](02-protocol.md) | What is known about the ARC's arm RPC, what is not, and the **Phase 0 discovery plan** that must run on hardware before any command code is final |
| 3 | [03-ha-integration.md](03-ha-integration.md) | Entities, unique IDs, state mapping, options flow, code handling, actions, errors, repairs, diagnostics |
| 4 | [04-frontend.md](04-frontend.md) | What the user sees: options screens, more-info dialog, dashboard cards (with YAML), error messages, notifications |
| 5 | [05-implementation-plan.md](05-implementation-plan.md) | Phased work breakdown, file-by-file changes, tests, acceptance criteria, risks |

## Decisions already taken in this design

These are the design's recommendations. The implementer should follow them
unless the repository owner says otherwise. Each is argued in the linked
document.

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

## Open questions for the repository owner

1. **Mixed state.** The system panel needs one HA state when areas differ.
   This design uses `armed_custom_bypass` plus a `mixed` attribute. The
   alternative is to show the "most armed" state. Confirm the choice.
2. **Single-area systems.** With one enabled area, this design creates only the
   system panel, not a duplicate area panel. Confirm.
3. **Code requirement.** Should enabling arm control *require* a code to be set,
   or only warn (repair issue) when there is none? This design warns.
4. **Which ARC user.** Should the docs recommend a dedicated ARC user with
   arm/disarm rights for Home Assistant, separate from the admin account? This
   design recommends it but does not enforce it.
5. **Merge order.** Phases 1–2 can be built and tested against a fake ARC
   before the hardware evidence exists. Merge them early, with the option
   showing "not yet supported", or hold them until Phase 0 is done? See
   05, Phase 2 exit.

## Hard rules for the implementer

- Do not ship any command path until Phase 0 has produced hardware evidence
  (see 02). Building against the `CommandSpec` interface with a fake ARC is fine.
- Never send an arm/disarm RPC from discovery, diagnostics, setup, reload,
  options, or periodic code. Only a user or automation action may send one.
- Never retry an arm/disarm RPC automatically after it has been written to the
  socket.
- Never log or put into diagnostics the HA code, its hash, or the ARC password.
- `protocol/` must stay free of `homeassistant` imports (a test enforces this).
- Preserve every existing entity `unique_id`. New entities get new IDs only.
