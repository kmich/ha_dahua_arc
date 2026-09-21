# Contributing to Dahua ARC

Thanks for helping improve the Dahua ARC integration for Home Assistant.

This project talks directly to alarm hardware using locally observed CGI and DHIP behavior. Contributions are welcome, but protocol claims need to be evidence-based and changes must preserve existing Home Assistant entity identity and safety expectations.

## Before you start

For a normal bug or feature request, open the appropriate GitHub issue first when the change is non-trivial.

For security issues, use GitHub's private **Report a vulnerability** flow instead of a public issue. Never post ARC passwords, session material, unredacted diagnostics, serial numbers, public IP addresses, or captures containing credentials.

## Supported scope

The production integration is intentionally conservative:

- local CGI discovery and DHIP state/event handling,
- wired MultiIO inputs,
- verified primary wireless sensor inputs,
- read-only zone state,
- Home Assistant config flow, reauthentication, diagnostics, repairs, and area placement.

Do not present a protocol observation as supported behavior until it has repeatable evidence and regression coverage.

In particular, experimental PIRCam / LowRateWPAN work must remain isolated behind the existing research feature gate until it is proven reliable enough for normal Home Assistant use.

## Development setup

The integration targets Home Assistant 2026.9+ and Python 3.14.

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q custom_components/dahua_arc tests
ruff check custom_components/dahua_arc tests scripts
ruff format --check custom_components/dahua_arc tests scripts
ruff check --select S --ignore S101,S110,S324 custom_components/dahua_arc scripts

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin \
  tests/test_state_engine.py tests/test_area_assignment.py

python -m pytest tests/ha --cov=custom_components/dahua_arc --cov-report=term-missing
python scripts/check_package.py
```

The repository CI also runs Hassfest and HACS validation.

## Protocol and hardware evidence

Protocol work is especially welcome, but please make it reproducible.

When reporting or contributing a newly observed ARC behavior, include as much of the following as you safely can:

- exact ARC model,
- firmware version,
- whether the input is wired, MultiIO, or wireless,
- sensor/peripheral model where applicable,
- whether the ARC is armed or disarmed,
- expected state transition,
- observed CGI/DHIP method or event,
- whether the behavior survives reconnect/restart,
- sanitized sample payloads when they materially help.

Remove or replace all secrets and identifying data before posting. This includes passwords, tokens, cookies, serial numbers, MAC addresses, public addresses, account identifiers, and any unrelated alarm configuration.

A useful sanitized capture preserves field names, types, indexes, and event ordering while replacing identifying values.

## Compatibility rules

Please preserve these invariants unless a migration is explicitly designed and tested:

- existing entity `unique_id` values,
- config-entry identity and reauthentication behavior,
- user-selected or user-cleared Home Assistant areas,
- unavailable state semantics,
- read-only production behavior,
- the separation between production state and research-only features.

If a compatibility break is genuinely necessary, call it out in the issue and PR before implementation.

## Tests

Bug fixes should include a regression test whenever practical.

Protocol changes should prefer deterministic captured or representative payloads over live-device-only tests. Tests must not require credentials or access to a contributor's ARC.

Changes touching Home Assistant setup, config flows, registry behavior, diagnostics, migrations, or repairs should include coverage under `tests/ha/`.

## Pull requests

Before opening a PR:

1. Run the development checks above.
2. Confirm no credentials or identifying captures are committed.
3. Add or update tests for behavior changes.
4. Update `CHANGELOG.md` under **Unreleased** for user-visible changes.
5. Explain the hardware/firmware evidence for protocol changes.
6. Confirm that existing entity IDs and user area choices are preserved unless the PR explicitly includes a tested migration.

Small documentation-only changes do not need protocol evidence.

## Release discipline

The version in `custom_components/dahua_arc/manifest.json` must match the release tag without the leading `v`.

Release tags use the form `vX.Y.Z`. The release workflow performs preflight validation, builds the integration archive dynamically from the tag version, and creates or updates the corresponding GitHub Release.

Do not manually claim support for hardware or protocol paths that have not been validated.
