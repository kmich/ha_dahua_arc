from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import struct
import threading
from typing import Any

from . import const
from .exceptions import DHIPError, LoginError

# Sentinel so callers can send an explicit JSON ``null`` params value, which
# some Dahua instance services (``<service>.factory.instance``) expect.
_DEFAULT_PARAMS: Any = object()


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

    @property
    def lock(self) -> threading.RLock:
        """Lock serialising request/response exchanges on this socket."""
        return self._lock

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)

    def set_timeout(self, timeout: float | None) -> None:
        """Change the receive timeout of an already connected socket."""
        if self.sock is not None:
            self.sock.settimeout(timeout)

    def shutdown(self) -> None:
        """Unblock any thread waiting in ``recv`` without releasing the socket."""
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise DHIPError("not connected")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DHIPError("socket closed by peer")
            buf.extend(chunk)
        return bytes(buf)

    # Backwards-compatible private name.
    _recv_exact = recv_exact

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

    def recv_header(self) -> tuple[int, int, int, int, int, int]:
        """Read one DHIP header.

        Returns ``(session, request_id, package_len, package_index,
        message_len, data_len)``.
        """
        hdr = self.recv_exact(const.HEADER_SIZE)
        size, magic, session, req_id, pkg_len, pkg_idx, msg_len, data_len = (
            struct.unpack(const.HEADER_FMT, hdr)
        )
        if size != const.HEADER_SIZE or magic != const.DHIP_MAGIC:
            raise DHIPError("invalid DHIP header")
        return session, req_id, pkg_len, pkg_idx, msg_len, data_len

    def recv_frame(self):
        session, req_id, pkg_len, pkg_idx, msg_len, data_len = self.recv_header()
        body = self.recv_exact(pkg_len) if pkg_len else b""
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

    def recv_fragmented_json(
        self, request_id: int, max_fragments: int = 64
    ) -> tuple[dict[str, Any], int, int]:
        """Reassemble one JSON response split over several DHIP packages.

        Returns ``(response, fragment_count, message_bytes)``. Large Dahua
        tables (for example a 256-row ``getChannelsState``) arrive as
        consecutive packages that share one ``message_len``.
        """
        chunks: list[bytes] = []
        expected_len: int | None = None
        for fragment_number in range(max_fragments):
            _session, response_id, pkg_len, pkg_idx, msg_len, data_len = (
                self.recv_header()
            )
            if response_id != request_id:
                raise DHIPError(
                    f"request id mismatch: expected {request_id}, got {response_id}"
                )
            if pkg_idx != fragment_number:
                raise DHIPError(
                    f"fragment order mismatch: expected {fragment_number}, got {pkg_idx}"
                )
            if data_len:
                raise DHIPError("unexpected binary data in JSON response")
            if expected_len is None:
                expected_len = msg_len
            elif expected_len != msg_len:
                raise DHIPError("message length changed between fragments")
            chunks.append(self.recv_exact(pkg_len))
            raw = b"".join(chunks)
            if len(raw) >= expected_len:
                raw = raw[:expected_len]
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise DHIPError(f"invalid JSON: {exc}") from exc
                return obj, fragment_number + 1, len(raw)
        raise DHIPError(f"response exceeded {max_fragments} fragments")

    def send_request(
        self,
        method: str,
        params: Any = _DEFAULT_PARAMS,
        *,
        object_id: int | None = None,
        extra: dict | None = None,
    ) -> int:
        """Send one RPC request without waiting for its reply.

        Returns the request id. ``params=None`` is sent as JSON ``null``;
        omitting it sends ``{}``.
        """
        with self._lock:
            self._id += 1
            payload: dict[str, Any] = {
                "method": method,
                "id": self._id,
                "params": {} if params is _DEFAULT_PARAMS else params,
            }
            if self.session:
                payload["session"] = self.session
            if object_id is not None:
                payload["object"] = int(object_id)
            if extra:
                payload.update(extra)
            self._send_frame(payload)
            return self._id

    def call(
        self,
        method: str,
        params: Any = _DEFAULT_PARAMS,
        *,
        object_id: int | None = None,
        fragmented: bool = False,
        max_fragments: int = 64,
    ) -> dict[str, Any]:
        """Send one RPC and return its JSON reply, verifying the reply id."""
        with self._lock:
            request_id = self.send_request(method, params, object_id=object_id)
            if fragmented:
                obj, _, _ = self.recv_fragmented_json(request_id, max_fragments)
                return obj
            obj, _data, _meta = self.recv_frame()
            if obj.get("id") != request_id:
                raise DHIPError(
                    f"{method} response id mismatch: "
                    f"expected {request_id}, got {obj.get('id')}"
                )
            return obj

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
