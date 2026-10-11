"""Точное время из браузера: расширение/мост отдаёт ``video.currentTime``.

Для сайтов, где доступен DOM, OCR не нужен — время приходит точное. Мост слушает
``http://127.0.0.1:8799/time`` (см. ``browser-extension/``) и отвечает JSON::

    {"position": 1234.5, "duration": 7200.0, "paused": false}
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from .base import ClockReading

log = logging.getLogger("streamblur.clock.dom")


class DomClock:
    id = "dom"

    def __init__(self, url: str, poll_seconds: float = 0.25, timeout: float = 1.5) -> None:
        self.url = url
        self.poll_interval = max(0.05, float(poll_seconds))
        self.timeout = timeout
        self.last_payload: dict[str, Any] = {}

    def start(self) -> tuple[bool, str]:
        try:
            payload = self._request()
        except (requests.RequestException, ValueError) as exc:
            return False, f"Мост браузера недоступен ({self.url}): {exc}"
        if "position" not in payload:
            return False, "Мост ответил без поля position"
        return True, f"Мост браузера подключён: {self.url}"

    def _request(self) -> dict[str, Any]:
        response = requests.get(self.url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def read(self) -> ClockReading | None:
        try:
            payload = self._request()
        except (requests.RequestException, ValueError) as exc:
            log.debug("dom clock read failed: %s", exc)
            return None
        self.last_payload = payload
        try:
            position = float(payload.get("position"))
        except (TypeError, ValueError):
            return None
        paused = bool(payload.get("paused"))
        return ClockReading(
            position=max(0.0, position),
            confidence=0.0 if paused else 1.0,
            raw_text=json.dumps(payload, ensure_ascii=False)[:120],
            paused=paused,
        )

    def stop(self) -> None:
        return None
