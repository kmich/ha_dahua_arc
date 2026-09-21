from __future__ import annotations


class DHIPError(Exception):
    pass


class DahuaError(DHIPError):
    def __init__(
        self, message: str, code: int | None = None, method: str | None = None
    ):
        self.code = code
        self.method = method
        prefix = f"{method}: " if method else ""
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"{prefix}{message}{suffix}")


class LoginError(DahuaError):
    pass
