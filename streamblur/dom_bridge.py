"""HTTP-мост для браузерного расширения.

Расширение (``browser-extension/``) раз в 250 мс шлёт ``POST /time`` с состоянием
активного ``<video>``, а StreamBlur забирает его через ``GET /time``:

    POST /time  {"position": 1234.5, "duration": 7200, "paused": false, "title": "..."}
    GET  /time  → последнее полученное состояние

Только stdlib, поэтому мост можно запускать где угодно:

    python -m streamblur.dom_bridge --port 8799
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_STATE: dict[str, Any] = {}
_LOCK = threading.Lock()
_STALE_AFTER = 5.0


def _payload() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    if data:
        data["stale"] = (time.time() - float(data.get("received_at", time.time()))) > _STALE_AFTER
    return data


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "StreamBlurDomBridge/0.1"

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802 — сигнатура stdlib
        if self.path.split("?")[0] not in ("/time", "/"):
            self._send(404, {"error": "not found"})
            return
        data = _payload()
        if not data:
            self._send(200, {"position": None, "message": "расширение ещё ничего не прислало"})
            return
        self._send(200, data)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/time":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad json: {exc}"})
            return
        position = body.get("position")
        try:
            position = float(position) if position is not None else None
        except (TypeError, ValueError):
            position = None
        with _LOCK:
            _STATE.clear()
            _STATE.update(
                {
                    "position": max(0.0, position) if position is not None else None,
                    "duration": body.get("duration"),
                    "paused": bool(body.get("paused")),
                    "title": body.get("title"),
                    "url": body.get("url"),
                    "received_at": time.time(),
                }
            )
        self._send(200, {"ok": True})

    # -------------------------------------------------------------- helpers
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 — preflight от расширения
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        return None


def serve(host: str = "127.0.0.1", port: int = 8799) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dom-bridge")
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Мост между браузерным расширением и StreamBlur")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    server = serve(args.host, args.port)
    print(f"DOM-мост: http://{args.host}:{args.port}/time  (Ctrl+C — выход)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
