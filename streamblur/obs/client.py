"""Мини-клиент obs-websocket v5 (протокол 5.x) без внешних зависимостей.

Реализовано ровно то, что нужно для автоблюра:

* ``Identify``/``Hello`` с паролем (HMAC-SHA256);
* ``GetSourceFilterList``, ``SetSourceFilterEnabled``, ``CreateSourceFilter``,
  ``SetInputFilterSettings``;
* ``GetSceneList`` / ``GetCurrentProgramScene`` — для выбора сцены и источника;
* ``GetVersion`` — для диагностики.

Работает и с настоящим OBS, и с ``tests/mock_obs_server.py``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import struct
import threading
import time
from typing import Any, Callable

log = logging.getLogger("streamblur.obs")

OPCODE_HELLO = 0
OPCODE_IDENTIFY = 1
OPCODE_IDENTIFIED = 2
OPCODE_REQUEST = 6
OPCODE_REQUEST_RESPONSE = 7
OPCODE_EVENT = 5

RPC_VERSION = 1


class OBSClientError(RuntimeError):
    pass


def make_auth(password: str, salt: str, challenge: str) -> str:
    """Стандартная схема аутентификации obs-websocket v5."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("utf-8")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("utf-8")


class _Frame:
    """Одно WebSocket-сообщение (RFC6455, клиентская маска обязательна)."""

    @staticmethod
    def encode(payload: bytes) -> bytes:
        import os

        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(header) + masked

    @staticmethod
    def read(sock: socket.socket) -> bytes | None:
        head = _recv_exact(sock, 2)
        if head is None:
            return None
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            ext = _recv_exact(sock, 2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = _recv_exact(sock, 8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = b""
        if masked:
            mask = _recv_exact(sock, 4) or b""
        data = _recv_exact(sock, length) if length else b""
        if data is None:
            return None
        if masked and mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            return None
        if opcode in (0x9, 0xA):  # ping/pong
            return b""
        return data


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            chunk = sock.recv(min(remaining, 65536))
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class BufferedSocket:
    """Сокет с буфером недочитанных байтов.

    Нужен потому, что ответ ``101 Switching Protocols`` и первый WebSocket-кадр
    (Hello) часто приходят одним TCP-сегментом: без буфера остаток теряется и
    клиент решает, что соединение разорвано.
    """

    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(initial)

    def recv(self, size: int) -> bytes:
        if self._buf:
            data = bytes(self._buf[:size])
            del self._buf[:size]
            return data
        return self._sock.recv(size)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def close(self) -> None:
        self._sock.close()


class OBSClient:
    """Синхронный клиент. Один сокет на соединение, потокобезопасен через lock."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str = "",
                 timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._counter = 0
        self.connected = False
        self.last_error: str | None = None
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None

    # -------------------------------------------------------------- connect
    def connect(self) -> dict[str, Any]:
        with self._lock:
            self.close()
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            buffered = self._handshake_http(sock)
            self._sock = buffered
            hello = self._expect_json(OPCODE_HELLO)
            auth_payload = hello.get("d", {}).get("authentication")
            identify: dict[str, Any] = {
                "rpcVersion": RPC_VERSION,
                "eventSubscriptions": 33,
            }
            if auth_payload:
                if not self.password:
                    raise OBSClientError("OBS требует пароль, а в настройках он пустой")
                identify["authentication"] = make_auth(
                    self.password, auth_payload.get("salt", ""), auth_payload.get("challenge", "")
                )
            self._send(OPCODE_IDENTIFY, {"d": identify})
            identified = self._expect_json(OPCODE_IDENTIFIED)
            self.connected = True
            self.last_error = None
            return identified.get("d", {})

    def _handshake_http(self, sock: socket.socket) -> BufferedSocket:
        key = base64.b64encode(b"streamblur-handshake").decode()
        request = (
            f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise OBSClientError("OBS закрыл соединение во время рукопожатия")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise OBSClientError(f"OBS отказал в WebSocket-рукопожатии: {status_line}")
        # всё, что приехало после заголовков, — уже кадры WebSocket; не теряем
        return BufferedSocket(sock, rest)

    def close(self) -> None:
        with self._lock:
            self.connected = False
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # --------------------------------------------------------------- protocol
    def _send(self, opcode: int, payload: dict[str, Any]) -> None:
        if self._sock is None:
            raise OBSClientError("Нет соединения с OBS")
        body = {"op": opcode, **payload}
        self._sock.sendall(_Frame.encode(json.dumps(body).encode("utf-8")))

    def _expect_json(self, opcode: int) -> dict[str, Any]:
        if self._sock is None:
            raise OBSClientError("Нет соединения с OBS")
        while True:
            raw = _Frame.read(self._sock)
            if raw is None:
                raise OBSClientError("Соединение с OBS разорвано")
            if not raw:
                continue
            message = json.loads(raw.decode("utf-8"))
            if message.get("op") == opcode:
                return message
            if message.get("op") == OPCODE_EVENT and self.on_event:
                data = message.get("d", {})
                self.on_event(str(data.get("eventType", "")), data.get("eventData", {}))

    def request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.connected:
                raise OBSClientError("Нет соединения с OBS")
            self._counter += 1
            request_id = f"sb-{self._counter}-{int(time.time()*1000)}"
            self._send(
                OPCODE_REQUEST,
                {"d": {"requestType": request_type, "requestId": request_id,
                       "requestData": data or {}}},
            )
            while True:
                message = self._expect_json(OPCODE_REQUEST_RESPONSE)
                body = message.get("d", {})
                if body.get("requestId") != request_id:
                    continue
                status = body.get("requestStatus", {})
                if not status.get("result", False):
                    comment = status.get("comment", "неизвестная ошибка")
                    raise OBSClientError(f"{request_type}: {comment}")
                return body.get("responseData", {})

    # ------------------------------------------------------------ high level
    def version(self) -> dict[str, Any]:
        return self.request("GetVersion")

    def scenes(self) -> list[str]:
        data = self.request("GetSceneList")
        return [s.get("sceneName", "") for s in data.get("scenes", [])]

    def current_scene(self) -> str:
        return self.request("GetCurrentProgramScene").get("currentProgramSceneName", "")

    def inputs_of_scene(self, scene: str) -> list[str]:
        data = self.request("GetSceneItemList", {"sceneName": scene})
        names: list[str] = []
        for item in data.get("sceneItems", []):
            name = item.get("sourceName")
            if name:
                names.append(name)
        return names

    def filters_of(self, source: str) -> list[dict[str, Any]]:
        return self.request("GetSourceFilterList", {"sourceName": source}).get("filters", [])

    def set_filter_enabled(self, source: str, filter_name: str, enabled: bool) -> None:
        self.request(
            "SetSourceFilterEnabled",
            {"sourceName": source, "filterName": filter_name, "filterEnabled": enabled},
        )

    def filter_settings(self, source: str, filter_name: str) -> dict[str, Any]:
        data = self.request(
            "GetSourceFilter", {"sourceName": source, "filterName": filter_name}
        )
        return data.get("filterSettings", {})

    def set_filter_settings(self, source: str, filter_name: str, settings: dict[str, Any]) -> None:
        self.request(
            "SetInputFilterSettings",
            {"sourceName": source, "filterName": filter_name, "filterSettings": settings},
        )

    def create_filter(self, source: str, filter_name: str, filter_kind: str,
                      settings: dict[str, Any]) -> None:
        self.request(
            "CreateSourceFilter",
            {
                "sourceName": source,
                "filterName": filter_name,
                "filterKind": filter_kind,
                "filterSettings": settings,
            },
        )

    def set_scene_item_enabled(self, scene: str, source: str, enabled: bool) -> None:
        item_id = self.request(
            "GetSceneItemId", {"sceneName": scene, "sourceName": source}
        ).get("sceneItemId")
        self.request(
            "SetSceneItemEnabled",
            {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled},
        )

    # ------------------------------------------------------------- диагностика
    def ping(self) -> tuple[bool, str]:
        try:
            info = self.connect()
            version = self.version()
            self.close()
            return True, (
                f"OBS {version.get('obsVersion', '?')} / "
                f"obs-websocket {version.get('obsWebSocketVersion', '?')} "
                f"(rpc {info.get('negotiatedRpcVersion', '?')})"
            )
        except (OBSClientError, OSError, ValueError) as exc:
            self.close()
            return False, f"{type(exc).__name__}: {exc}"
