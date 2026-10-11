"""Мок obs-websocket v5 для демо и тестов.

Поднимается отдельным процессом/потоком и честно реализует протокол: Hello →
Identify (с паролем, если задан) → Request/RequestResponse. Поддерживает ровно те
запросы, которые использует StreamBlur, и пишет в лог каждое переключение фильтра —
так в демо видно, что автоблюр действительно сработал.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import secrets
import socket
import struct
import threading
import time
from typing import Any, Callable

log = logging.getLogger("streamblur.mockobs")

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_EVENT, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 5, 6, 7


def _mask_payload(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frame(sock: socket.socket) -> bytes | None:
    head = _recv_exact(sock, 2)
    if head is None:
        return None
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        ext = _recv_exact(sock, 2)
        length = struct.unpack("!H", ext)[0] if ext else 0
    elif length == 127:
        ext = _recv_exact(sock, 8)
        length = struct.unpack("!Q", ext)[0] if ext else 0
    mask = _recv_exact(sock, 4) if masked else b""
    data = _recv_exact(sock, length) if length else b""
    if data is None:
        return None
    if masked and mask:
        data = _mask_payload(data, mask)
    if opcode == 0x8:
        return None
    return data


def write_frame(sock: socket.socket, payload: bytes) -> None:
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    sock.sendall(bytes(header) + payload)


class MockOBS:
    """Состояние «как бы OBS»: сцены, источники, фильтры."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4456, password: str = "") -> None:
        self.host = host
        self.port = port
        self.password = password
        self.salt = secrets.token_urlsafe(16)
        self.challenge = secrets.token_urlsafe(16)
        self.scenes: dict[str, list[str]] = {"Main": ["Game Capture", "Webcam", "BRB Screen"]}
        self.current_scene = "Main"
        self.filters: dict[str, dict[str, dict[str, Any]]] = {}
        self.scene_items_enabled: dict[tuple[str, str], bool] = {}
        self.blur_log: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_blur: Callable[[bool], None] | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self.port = server.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="mock-obs", daemon=True)
        self._thread.start()
        log.info("Мок-OBS слушает %s:%s", self.host, self.port)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------ serve
    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        try:
            self._handshake(conn)
            self._send(conn, OP_HELLO, {
                "obsWebSocketVersion": "5.5.0-mock",
                "rpcVersion": 1,
                **({"authentication": {"challenge": self.challenge, "salt": self.salt}}
                   if self.password else {}),
            })
            while not self._stop.is_set():
                raw = read_frame(conn)
                if raw is None:
                    break
                message = json.loads(raw.decode("utf-8"))
                op = message.get("op")
                data = message.get("d", {})
                if op == OP_IDENTIFY:
                    if not self._check_auth(data.get("authentication")):
                        break
                    self._send(conn, OP_IDENTIFIED, {
                        "negotiatedRpcVersion": 1,
                        "obsWebSocketVersion": "5.5.0-mock",
                    })
                elif op == OP_REQUEST:
                    self._handle_request(conn, data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.debug("mock-obs connection closed: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn: socket.socket) -> None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(1024)
            if not chunk:
                raise OSError("client closed during handshake")
            buffer += chunk
        key = ""
        for line in buffer.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )

    def _check_auth(self, token: str | None) -> bool:
        if not self.password:
            return True
        if not token:
            return False
        secret = base64.b64encode(
            hashlib.sha256((self.password + self.salt).encode()).digest()
        ).decode()
        expected = base64.b64encode(
            hashlib.sha256((secret + self.challenge).encode()).digest()
        ).decode()
        return secrets.compare_digest(token, expected)

    # --------------------------------------------------------------- requests
    def _send(self, conn: socket.socket, op: int, data: dict[str, Any]) -> None:
        write_frame(conn, json.dumps({"op": op, "d": data}).encode("utf-8"))

    def _handle_request(self, conn: socket.socket, data: dict[str, Any]) -> None:
        kind = data.get("requestType", "")
        payload = data.get("requestData", {}) or {}
        self.requests.append(kind)
        try:
            response = self._dispatch(kind, payload)
            status: dict[str, Any] = {"result": True, "code": 100}
        except KeyError as exc:
            response, status = {}, {"result": False, "code": 600, "comment": f"не найдено: {exc}"}
        except ValueError as exc:
            response, status = {}, {"result": False, "code": 600, "comment": str(exc)}
        self._send(conn, OP_RESPONSE, {
            "requestType": kind,
            "requestId": data.get("requestId"),
            "requestStatus": status,
            "responseData": response,
        })

    def _dispatch(self, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        if kind == "GetVersion":
            return {
                "obsVersion": "30.0.0-mock",
                "obsWebSocketVersion": "5.5.0-mock",
                "rpcVersion": 1,
                "platform": "mock",
            }
        if kind == "GetSceneList":
            return {"scenes": [{"sceneName": n} for n in self.scenes]}
        if kind == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.current_scene}
        if kind == "GetSceneItemList":
            scene = p.get("sceneName", self.current_scene)
            if scene not in self.scenes:
                raise KeyError(scene)
            return {
                "sceneItems": [
                    {"sceneItemId": i, "sourceName": n, "sourceType": "OBS_SOURCE_TYPE_INPUT"}
                    for i, n in enumerate(self.scenes[scene])
                ]
            }
        if kind == "GetSourceFilterList":
            source = p.get("sourceName", "")
            return {"filters": list(self.filters.get(source, {}).values())}
        if kind == "GetSourceFilter":
            source, name = p.get("sourceName", ""), p.get("filterName", "")
            filt = self.filters.get(source, {}).get(name)
            if filt is None:
                raise KeyError(name)
            return dict(filt)
        if kind == "CreateSourceFilter":
            source = p.get("sourceName", "")
            name = p.get("filterName", "")
            self.filters.setdefault(source, {})[name] = {
                "filterName": name,
                "filterKind": p.get("filterKind", ""),
                "filterEnabled": False,
                "filterSettings": p.get("filterSettings", {}),
            }
            return {}
        if kind == "SetInputFilterSettings":
            source = p.get("sourceName", "")
            name = p.get("filterName", "")
            filt = self.filters.get(source, {}).get(name)
            if filt is None:
                raise KeyError(name)
            filt["filterSettings"] = {**filt.get("filterSettings", {}), **p.get("filterSettings", {})}
            return {}
        if kind == "SetSourceFilterEnabled":
            source = p.get("sourceName", "")
            name = p.get("filterName", "")
            enabled = bool(p.get("filterEnabled"))
            filt = self.filters.get(source, {}).get(name)
            if filt is None:
                raise KeyError(name)
            filt["filterEnabled"] = enabled
            self.blur_log.append({"ts": time.time(), "source": source, "filter": name,
                                  "enabled": enabled})
            log.info("[mock-obs] фильтр %s на «%s» → %s", name, source,
                     "ВКЛ" if enabled else "выкл")
            if self.on_blur:
                try:
                    self.on_blur(enabled)
                except Exception:  # noqa: BLE001
                    pass
            return {}
        if kind == "GetSceneItemId":
            scene, source = p.get("sceneName", ""), p.get("sourceName", "")
            items = self.scenes.get(scene, [])
            if source not in items:
                raise KeyError(source)
            return {"sceneItemId": items.index(source)}
        if kind == "SetSceneItemEnabled":
            scene = p.get("sceneName", "")
            item_id = int(p.get("sceneItemId", -1))
            sources = self.scenes.get(scene, [])
            if not 0 <= item_id < len(sources):
                raise KeyError(item_id)
            source = sources[item_id]
            enabled = bool(p.get("sceneItemEnabled"))
            self.scene_items_enabled[(scene, source)] = enabled
            self.blur_log.append({"ts": time.time(), "source": source, "filter": "(toggle)",
                                  "enabled": enabled})
            log.info("[mock-obs] источник «%s» → %s", source, "ВКЛ" if enabled else "выкл")
            if self.on_blur:
                self.on_blur(enabled)
            return {}
        raise ValueError(f"Мок-OBS не знает запрос {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Мок obs-websocket для StreamBlur")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4456)
    parser.add_argument("--password", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = MockOBS(args.host, args.port, args.password)
    server.start()
    print(f"Мок-OBS запущен на ws://{args.host}:{server.port} (Ctrl+C — выход)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
