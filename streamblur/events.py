"""Кольцевой буфер событий для ленты в UI (WebSocket)."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamEvent:
    ts: float
    kind: str
    data: dict[str, Any]


class EventBus:
    def __init__(self, maxlen: int = 500) -> None:
        self._items: deque[StreamEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._sequence = 0

    def publish(self, kind: str, **data: Any) -> StreamEvent:
        """Публикует событие.

        ``kind`` и ``seq`` зарезервированы: если они пришли ещё и в ``data``,
        явные аргументы побеждают (а не роняют вызывающий код).
        """
        data.pop("kind", None)
        data.pop("seq", None)
        with self._lock:
            self._sequence += 1
            event = StreamEvent(ts=time.time(), kind=kind, data={"seq": self._sequence, **data})
            self._items.append(event)
        return event

    def since(self, sequence: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"ts": e.ts, "kind": e.kind, **e.data}
                for e in self._items
                if int(e.data.get("seq", 0)) > sequence
            ]

    def last(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"ts": e.ts, "kind": e.kind, **e.data} for e in list(self._items)[-limit:]
            ][::-1]
