"""Home Assistant-side arm/disarm code: salted hash, check and attempt limiter.

The code is a gate in Home Assistant only. It is never sent to the ARC and
never logged; only its PBKDF2 hash is stored in the config entry options.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable

SCHEME = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16
CODE_MIN_LENGTH = 4
CODE_MAX_LENGTH = 8

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 60.0
LOCKOUT_SECONDS = 60.0


def code_is_valid_format(code: str) -> bool:
    """4 to 8 ASCII digits."""
    return (
        CODE_MIN_LENGTH <= len(code) <= CODE_MAX_LENGTH
        and code.isascii()
        and code.isdigit()
    )


def _derive(code: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, iterations)


def hash_code(code: str) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(code, salt, ITERATIONS)
    return "$".join(
        (
            SCHEME,
            str(ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_code(code: str, stored: str) -> bool:
    """Constant-time check of ``code`` against a stored hash."""
    try:
        scheme, iterations, salt_b64, hash_b64 = stored.split("$")
        if scheme != SCHEME:
            return False
        count = int(iterations)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except ValueError:
        return False
    if not 1 <= count <= 10_000_000:
        return False
    return hmac.compare_digest(_derive(code, salt, count), expected)


class AttemptLimiter:
    """Lock out after repeated wrong codes: slows guessing from a dashboard."""

    def __init__(
        self,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        window: float = WINDOW_SECONDS,
        lockout: float = LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window
        self._lockout = lockout
        self._clock = clock
        self._failures: deque[float] = deque()
        self._locked_until = 0.0

    def seconds_locked(self) -> int:
        """Whole seconds left on the lockout, or 0 when not locked."""
        remaining = self._locked_until - self._clock()
        return max(0, int(remaining) + 1) if remaining > 0 else 0

    def record_failure(self) -> None:
        now = self._clock()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            self._failures.clear()

    def record_success(self) -> None:
        self._failures.clear()
