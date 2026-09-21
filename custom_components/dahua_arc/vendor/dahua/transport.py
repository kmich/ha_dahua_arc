from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading

from . import const
from .exceptions import DHIPError, LoginError


def md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def login_digest(username: str, password: str, realm: str, random: str) -> str:
    pwd_hash = md5_upper(f"{username}:{realm}:{password}")
    return md5_upper(f"{username}:{random}:{pwd_hash}")


class DHIPTransport:
    def __init__(
        self,
        host: str,
        port: int = const.DEFAULT_PORT,
        timeout: float = const.DEFAULT_TIMEOUT,
    ):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self.session = 0
        self.encryption: str | None = None
        self._id = 0
        self._lock = threading.RLock()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise DHIPError("not connected")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DHIPError("socket closed by peer")
            buf.extend(chunk)
        return bytes(buf)

    def _send_frame(self, payload: dict, data: bytes = b"") -> None:
        if self.sock is None:
            raise DHIPError("not connected")
        body = json.dumps(payload, separators=(",", ":")).encode()
        header = struct.pack(
            const.HEADER_FMT,
            const.HEADER_SIZE,
            const.DHIP_MAGIC,
            self.session,
            payload.get("id", 0),
            len(body) + len(data),
            0,
            len(body),
            len(data),
        )
        self.sock.sendall(header + body + data)

    def recv_frame(self):
        hdr = self._recv_exact(const.HEADER_SIZE)
        size, magic, session, req_id, pkg_len, pkg_idx, msg_len, data_len = (
            struct.unpack(const.HEADER_FMT, hdr)
        )
        if size != const.HEADER_SIZE or magic != const.DHIP_MAGIC:
            raise DHIPError("invalid DHIP header")
        body = self._recv_exact(pkg_len) if pkg_len else b""
        msg, data = body[:msg_len], body[msg_len : msg_len + data_len]
        try:
            obj = json.loads(msg.decode("utf-8")) if msg else {}
        except json.JSONDecodeError as exc:
            raise DHIPError(f"invalid JSON: {exc}") from exc
        return (
            obj,
            data,
            {
                "session": session,
                "request_id": req_id,
                "package_index": pkg_idx,
                "data_length": data_len,
            },
        )

    def _recv_frame(self):
        obj, data, _ = self.recv_frame()
        return obj, data

    def request(
        self, method: str, params=None, *, data: bytes = b"", extra: dict | None = None
    ):
        with self._lock:
            self._id += 1
            payload = {
                "method": method,
                "id": self._id,
                "params": {} if params is None else params,
            }
            if self.session:
                payload["session"] = self.session
            if extra:
                payload.update(extra)
            self._send_frame(payload, data)
            return self._recv_frame()

    def login(self, username: str, password: str, client_type: str = "Web3.0") -> dict:
        resp, _ = self.request(
            const.LOGIN,
            {
                "userName": username,
                "password": "",
                "clientType": client_type,
                "loginType": "Direct",
            },
        )
        self.session = resp.get("session", self.session) or 0
        params = resp.get("params") or {}
        realm, random = params.get("realm"), params.get("random")
        self.encryption = params.get("encryption")
        if self.encryption and self.encryption not in ("Default", "OldDigest", ""):
            raise DHIPError(f"unsupported login encryption {self.encryption!r}")
        if resp.get("result") and realm is None:
            return resp
        if not realm or not random:
            raise LoginError(
                f"login challenge missing realm/random: {resp}",
                code=(resp.get("error") or {}).get("code"),
            )
        digest = login_digest(username, password, realm, random)
        resp2, _ = self.request(
            const.LOGIN,
            {
                "userName": username,
                "password": digest,
                "clientType": client_type,
                "loginType": "Direct",
                "authorityType": "Default",
                "passwordType": "Default",
                "realm": realm,
                "random": random,
            },
        )
        if not resp2.get("result"):
            err = resp2.get("error") or {}
            raise LoginError(
                err.get("message") or "login failed",
                code=err.get("code"),
                method=const.LOGIN,
            )
        self.session = resp2.get("session", self.session) or self.session
        return resp2
