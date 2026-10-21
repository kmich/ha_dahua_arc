"""An in-process fake ARC speaking DHIP over fake sockets.

``FakeArc.patch()`` replaces ``socket.create_connection`` in the vendored
transport so every DHIPTransport the integration opens talks to this fake.
No real socket is created, so it works under pytest-socket as well.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from custom_components.dahua_arc.vendor.dahua import const
from custom_components.dahua_arc.vendor.dahua.transport import login_digest

# Must equal FAKE_COMMAND_SPEC.method (a test checks it). The fake ARC never
# answers any other arm method.
ARM_METHOD = "FakeArc.setArmMode"

SESSION = 4242
REALM = "Login to FAKE"
RANDOM = "123456789"


def _frame(obj: dict[str, Any], *, request_id: int, parts: int = 1) -> bytes:
    body = json.dumps(obj).encode()
    size = -(-len(body) // parts)
    chunks = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    out = b""
    for index, chunk in enumerate(chunks):
        out += struct.pack(
            const.HEADER_FMT,
            const.HEADER_SIZE,
            const.DHIP_MAGIC,
            SESSION,
            request_id,
            len(chunk),
            index,
            len(body),
            0,
        )
        out += chunk
    return out


class FakeSocket:
    def __init__(self, arc: FakeArc) -> None:
        self.arc = arc
        self._rx = bytearray()
        self._tx = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._timeout: float | None = None
        self.attached = False

    # -- socket API used by DHIPTransport --------------------------------
    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def gettimeout(self) -> float | None:
        return self._timeout

    def shutdown(self, how: int) -> None:
        self.close()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.arc._forget(self)

    def recv(self, n: int) -> bytes:
        with self._cond:
            if not self._cond.wait_for(
                lambda: self._rx or self._closed, timeout=self._timeout
            ):
                raise TimeoutError("timed out")
            if not self._rx:
                raise OSError("socket closed")
            data = bytes(self._rx[:n])
            del self._rx[:n]
            return data

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("socket closed")
        self._tx.extend(data)
        while len(self._tx) >= const.HEADER_SIZE:
            header = struct.unpack(const.HEADER_FMT, self._tx[: const.HEADER_SIZE])
            total = const.HEADER_SIZE + header[4]
            if len(self._tx) < total:
                return
            body = bytes(self._tx[const.HEADER_SIZE : total])
            del self._tx[:total]
            self._handle(json.loads(body[: header[6]]))

    # -- fake ARC behaviour ----------------------------------------------
    def push(self, data: bytes) -> None:
        with self._cond:
            self._rx.extend(data)
            self._cond.notify_all()

    def _handle(self, request: dict[str, Any]) -> None:
        method = request["method"]
        request_id = request["id"]
        self.arc.calls.append(method)
        if method == "FileManager.downloadFile":
            self._download(request_id, request["params"]["fileName"])
            return
        response, parts = self.arc.respond(self, method, request.get("params"))
        if response is None:
            return  # a reply that never arrives
        response.setdefault("id", request_id)
        response.setdefault("session", SESSION)
        self.push(_frame(response, request_id=request_id, parts=parts))

    def _download(self, request_id: int, path: str) -> None:
        data = self.arc.files.get(path)
        if data is None:
            self.push(
                _frame(
                    {"id": request_id, "result": False, "error": {"code": 404}},
                    request_id=request_id,
                )
            )
            return
        meta = {"id": request_id, "result": True, "params": {"length": len(data)}}
        self.push(_frame(meta, request_id=request_id))
        # Firmware quirk: a JSON prefix and the binary file in one package.
        payload = b'{"part": "file \\"body\\""}' + b"\x00\x01" + data
        self.push(
            struct.pack(
                const.HEADER_FMT,
                const.HEADER_SIZE,
                const.DHIP_MAGIC,
                SESSION,
                request_id,
                len(payload),
                1,
                0,
                len(payload),
            )
            + payload
        )


class FakeArc:
    def __init__(
        self,
        *,
        password: str = "test-only",
        snapshot: list[dict[str, Any]] | None = None,
        inventory: dict[str, Any] | None = None,
        serial: str = "ARC-TEST-001",
    ) -> None:
        self.password = password
        self.snapshot_states = snapshot or []
        self.config_tables = inventory or {}
        self.serial = serial
        self.calls: list[str] = []
        # Service -> method names answered by "<service>.listMethod".
        self.method_lists: dict[str, list[str]] = {}
        self.files: dict[str, bytes] = {}
        self.sockets: list[FakeSocket] = []
        self.silent = False  # stop answering keepalives (dead peer)
        self._lock = threading.Lock()
        # -- arm/disarm support (the fake ARC only knows ARM_METHOD) --------
        # "ok" | "error" (no permission) | "refused" (reply carries the open
        # zones) | "no_reply" (applied, but the reply never arrives).
        self.arm_reply = "ok"
        self.arm_error_code = 268894210
        self.suppress_arm_events = False
        self.arm_takes_effect = True  # False: accepted but the mode is unchanged
        self.arm_delay = 0.02  # seconds between the reply and the events
        # ZoneAbnormal rows; when set, arming (not disarming) is refused with
        # ArmingFailure events and the modes stay unchanged.
        self.open_zones: list[dict[str, Any]] = []
        self.arm_requests: list[dict[str, Any]] = []
        self.event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._timers: list[threading.Timer] = []

    def _forget(self, sock: FakeSocket) -> None:
        with self._lock:
            if sock in self.sockets:
                self.sockets.remove(sock)

    def connect(self, address, timeout=None) -> FakeSocket:
        sock = FakeSocket(self)
        sock.settimeout(timeout)
        with self._lock:
            self.sockets.append(sock)
        return sock

    @contextmanager
    def patch(self):
        with patch.object(
            socket, "create_connection", side_effect=self.connect
        ) as mocked:
            yield mocked

    def push_event(self, event: dict[str, Any]) -> None:
        for listener in tuple(self.event_listeners):
            listener(event)
        payload = {
            "method": "client.notifyEventStream",
            "params": {"eventList": [event]},
        }
        with self._lock:
            attached = [s for s in self.sockets if s.attached]
        for sock in attached:
            sock.push(_frame(payload, request_id=0))

    # -- arm/disarm ------------------------------------------------------
    def area_modes(self) -> list[str]:
        rows = (self.config_tables.get("AreaArmMode") or {}).get("Areas") or []
        return [row.get("Mode", "D") for row in rows]

    def _area_name(self, index: int) -> str:
        table = self.config_tables.get("AlarmSubSystem") or []
        if index < len(table):
            return str(table[index].get("Name") or f"Area {index + 1}").strip()
        return f"Area {index + 1}"

    def _arm_event(
        self, code: str, index: int, mode: str, *, abnormal: dict | None = None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "EventOptions": {"EventType": "ArmOrDisarm"},
            "IsGlobal": index < 0,
            "Mode": mode,
            "Name": "FAKE",
            "Profile": "Auto",
            "TriggerMode": "Remote",
        }
        if index >= 0:
            data["AreaInfo"] = [{"Index": index, "Name": self._area_name(index)}]
        if abnormal:
            data["Abnormal"] = abnormal
        return {"Action": "Pulse", "Code": code, "Data": data, "Index": index}

    def _abnormal(self, areas: list[int]) -> dict[str, Any]:
        return {
            "detail": [
                {
                    "Area": index + 1,
                    "AreaName": self._area_name(index),
                    "ZoneAbnormal": [dict(zone) for zone in self.open_zones],
                }
                for index in areas
            ]
        }

    def _emit_arm_events(self, mode: str, areas: list[int], *, failed: bool) -> None:
        total = len(self.area_modes())
        suffix = "ArmingFailure" if failed else "AreaArmModeChange"
        abnormal = self._abnormal(areas) if failed else None
        if len(areas) == total and total > 1:
            self.push_event(
                self._arm_event(f"Global{suffix}", -1, mode, abnormal=abnormal)
            )
        for index in areas:
            self.push_event(
                self._arm_event(
                    suffix,
                    index,
                    mode,
                    abnormal=self._abnormal([index]) if failed else None,
                )
            )

    def _arm(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        self.arm_requests.append(params)
        mode, areas = params["Mode"], list(params["Areas"])
        if self.arm_reply == "error":
            return {
                "result": False,
                "error": {"code": self.arm_error_code, "message": "No permission"},
            }, 1
        refuse = bool(self.open_zones) and mode != "D"
        if refuse and self.arm_reply == "refused":
            return {
                "result": False,
                "error": {
                    "code": 1001,
                    "message": "Zones open",
                    "data": {"Abnormal": self._abnormal(areas)},
                },
            }, 1
        if self.arm_takes_effect and not refuse:
            rows = self.config_tables["AreaArmMode"]["Areas"]
            for index in areas:
                rows[index]["Mode"] = mode
        if not self.suppress_arm_events and (refuse or self.arm_takes_effect):
            timer = threading.Timer(
                self.arm_delay,
                self._emit_arm_events,
                args=(mode, areas),
                kwargs={"failed": refuse},
            )
            timer.daemon = True
            self._timers.append(timer)
            timer.start()
        if self.arm_reply == "no_reply":
            return None, 0
        return {"result": True}, 1

    def close_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def respond(
        self, sock: FakeSocket, method: str, params: Any
    ) -> tuple[dict[str, Any] | None, int]:
        if method == ARM_METHOD:
            return self._arm(params)
        if method == const.LOGOUT:
            return {"result": True}, 1
        if method == const.LOGIN:
            if not params.get("password"):
                return {
                    "result": False,
                    "error": {"code": 268632079, "message": "Login challenge"},
                    "params": {
                        "realm": REALM,
                        "random": RANDOM,
                        "encryption": "Default",
                    },
                }, 1
            expected = login_digest(params["userName"], self.password, REALM, RANDOM)
            if params["password"] != expected:
                return {
                    "result": False,
                    "error": {"code": 268632080, "message": "Wrong password"},
                }, 1
            return {"result": True, "params": {"keepAliveInterval": 60}}, 1
        if method == const.KEEPALIVE:
            if self.silent:
                return {"result": True, "id": -1}, 1
            return {"result": True, "params": {"timeout": 60}}, 1
        if method == const.EVENT_ATTACH:
            sock.attached = True
            return {"result": True}, 1
        if method == "AlarmRegion.getChannelsState":
            if (params or {}).get("Condition", {}).get("Type") == "AlarmIn":
                # Large tables arrive fragmented.
                return {"result": True, "params": {"States": self.snapshot_states}}, 3
            return {"result": True, "params": {"States": []}}, 1
        if method == "magicBox.getSerialNo":
            return {"result": True, "params": {"sn": self.serial}}, 1
        if method == "magicBox.getDeviceType":
            return {"result": True, "params": {"type": "ARC3800H"}}, 1
        if method.endswith(".listMethod"):
            methods = self.method_lists.get(method.removesuffix(".listMethod"))
            if methods is not None:
                return {"result": True, "params": {"method": methods}}, 1
        if method == "configManager.getConfig":
            table = self.config_tables.get(params.get("name"))
            if table is None:
                return {"result": False, "error": {"code": 268959743}}, 1
            return {"result": True, "params": {"table": table}}, 2
        return {"result": False, "error": {"code": 405, "message": "Not allowed"}}, 1
