from __future__ import annotations

DHIP_MAGIC = 0x50494844
HEADER_SIZE = 32
HEADER_FMT = "<IIIIIIII"
DEFAULT_PORT = 5000
DEFAULT_TIMEOUT = 10.0
DEFAULT_KEEPALIVE = 60
LOGIN = "global.login"
LOGOUT = "global.logout"
KEEPALIVE = "global.keepAlive"
EVENT_ATTACH = "eventManager.attach"
EVENT_DETACH = "eventManager.detach"
DAHUA_ERRORS = {
    268632079: "Login challenge",
    268632080: "Unknown user or wrong password",
    268632081: "User has been locked",
    268632082: "User is blocked",
    268632083: "User account is in use elsewhere",
    268894210: "Insufficient permissions for this method",
    403: "Forbidden",
    405: "Method not allowed",
}


def error_message(code: int | None, fallback: str = "RPC error") -> str:
    return DAHUA_ERRORS.get(code, fallback) if code is not None else fallback
