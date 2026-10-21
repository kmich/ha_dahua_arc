"""The Home Assistant arm/disarm code: hashing, checking and lockout."""

from __future__ import annotations

import pytest
from custom_components.dahua_arc import arm_code


def test_hash_is_salted_and_verifies() -> None:
    first, second = arm_code.hash_code("1234"), arm_code.hash_code("1234")
    assert first != second
    assert first.startswith("pbkdf2_sha256$")
    assert "1234" not in first
    assert arm_code.verify_code("1234", first)
    assert arm_code.verify_code("1234", second)
    assert not arm_code.verify_code("1235", first)
    assert not arm_code.verify_code("", first)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        "md5$1$AAAA$AAAA",
        "pbkdf2_sha256$x$AAAA$AAAA",
        "pbkdf2_sha256$0$AAAA$AAAA",
        "pbkdf2_sha256$99999999999$AAAA$AAAA",
        "pbkdf2_sha256$1000$***$***",
        "pbkdf2_sha256$1000$AAAA",
    ],
)
def test_malformed_hash_never_verifies(stored: str) -> None:
    assert not arm_code.verify_code("1234", stored)


@pytest.mark.parametrize(
    ("code", "valid"),
    [
        ("1234", True),
        ("12345678", True),
        ("123", False),
        ("123456789", False),
        ("12a4", False),
        ("", False),
        ("12 4", False),
        ("١٢٣٤", False),  # non-ASCII digits
    ],
)
def test_code_format(code: str, valid: bool) -> None:
    assert arm_code.code_is_valid_format(code) is valid


def test_five_wrong_codes_lock_for_a_minute() -> None:
    now = [100.0]
    limiter = arm_code.AttemptLimiter(clock=lambda: now[0])
    for _ in range(4):
        limiter.record_failure()
        assert limiter.seconds_locked() == 0
    limiter.record_failure()
    assert 59 <= limiter.seconds_locked() <= 61
    now[0] += 30
    assert 29 <= limiter.seconds_locked() <= 31
    now[0] += 31
    assert limiter.seconds_locked() == 0


def test_old_failures_age_out_and_success_resets() -> None:
    now = [0.0]
    limiter = arm_code.AttemptLimiter(clock=lambda: now[0])
    for _ in range(4):
        limiter.record_failure()
    now[0] += 61
    limiter.record_failure()
    assert limiter.seconds_locked() == 0
    for _ in range(3):
        limiter.record_failure()
    limiter.record_success()
    for _ in range(4):
        limiter.record_failure()
    assert limiter.seconds_locked() == 0
