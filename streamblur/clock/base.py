"""Базовые типы источников времени."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ClockReading:
    position: float | None
    """Время фильма в секундах (None — кадр не распознан)."""
    confidence: float = 0.0
    raw_text: str = ""
    paused: bool | None = None
    """``True``/``False`` — если источник сам знает про паузу (браузерный мост).

    ``None`` — источник не знает; тогда паузу определяет ``ClockTracker``
    по неизменности показаний.
    """


@dataclass
class ClockStatus:
    running: bool
    source: str | None
    position: float
    confidence: float
    message: str = ""
    text: str = ""
    offset: float = 0.0
    manual: bool = False
    stale_seconds: float | None = None
    frozen: bool = False
    """Фильм на паузе: extrapolация выключена, блюр держится как есть."""

    def as_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "source": self.source,
            "position": round(self.position, 3),
            "confidence": round(self.confidence, 3),
            "message": self.message,
            "text": self.text,
            "offset": round(self.offset, 3),
            "manual": self.manual,
            "stale_seconds": (
                round(self.stale_seconds, 2) if self.stale_seconds is not None else None
            ),
            "frozen": self.frozen,
        }


@runtime_checkable
class ClockSource(Protocol):
    id: str
    poll_interval: float

    def start(self) -> tuple[bool, str]: ...

    def read(self) -> ClockReading | None: ...

    def stop(self) -> None: ...


class ManualClock:
    """Источник-заглушка: время задаётся руками (хоткей «синхронизировать сейчас»)."""

    id = "manual"
    poll_interval = 1.0

    def __init__(self) -> None:
        self._position = 0.0

    def start(self) -> tuple[bool, str]:
        return True, "Ручной режим: задавайте время кнопкой «Синхронизировать»"

    def read(self) -> ClockReading | None:
        return None

    def stop(self) -> None:
        return None

    def set_position(self, position: float) -> None:
        self._position = max(0.0, float(position))
