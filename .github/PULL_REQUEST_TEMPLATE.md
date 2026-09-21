## Summary

Describe what this PR changes and why.

## Hardware / protocol evidence

If this changes protocol behavior, include the ARC model, firmware, input/peripheral type, and the repeatable evidence supporting the change.

For documentation-only or repository-maintenance changes, write **Not applicable**.

## Compatibility

- [ ] Existing entity `unique_id` values are preserved, or a tested migration is included.
- [ ] Existing config-entry identity / reauthentication behavior is preserved, or a tested migration is included.
- [ ] User-selected or user-cleared Home Assistant areas are preserved.
- [ ] Production behavior remains read-only unless the change has been explicitly discussed.
- [ ] Research-only behavior remains isolated from production state unless it has been proven and reviewed for promotion.

## Validation

- [ ] `python -m compileall -q custom_components/dahua_arc tests`
- [ ] `ruff check custom_components/dahua_arc tests scripts`
- [ ] `ruff format --check custom_components/dahua_arc tests scripts`
- [ ] Protocol / area tests pass.
- [ ] Home Assistant runtime / config-flow tests pass.
- [ ] `python scripts/check_package.py`
- [ ] I added or updated regression tests for behavior changes.
- [ ] I updated `CHANGELOG.md` under **Unreleased** for user-visible changes.

## Privacy

- [ ] No credentials, tokens, session data, unredacted serial numbers, public IP addresses, or unrelated alarm configuration are included in this PR.
