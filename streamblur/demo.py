"""Демо-режим: вся цепочка без установленного OBS.

Поднимается:

1. **мок-OBS** — реальный obs-websocket v5 протокол (``mock_obs_server.MockOBS``);
2. **виртуальный плеер** — «проигрывает» фильм с ускорением и отдаёт время так же,
   как это делает OCR/браузерный мост;
3. **штатный движок** — те же ``BlurEngine`` и ``BlurController``, что и в бою.

Поэтому демо проверяет не «заглушку», а настоящий код переключения размытия.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .clock.base import ClockReading
from .database import Database
from .engine import BlurEngine
from .obs.blur import BlurController
from .obs.client import OBSClient
from .mock_obs_server import MockOBS

log = logging.getLogger("streamblur.demo")

DEMO_FILM = {
    "title": "Демо-фильм: Ночная смена",
    "year": 2026,
    "country": "Россия",
    "studios": ["StreamBlur Demo Studio"],
    "production_companies": ["Demo Films LLC"],
    "genres": ["триллер", "драма"],
    "runtime_minutes": 12,
    "overview": "Служебный фильм для проверки автоблюра. Опасные сцены размечены вручную.",
}

DEMO_SEGMENTS = [
    {"start": 60.0, "end": 85.0, "reason": "драка, кровь", "kind": "violence"},
    {"start": 180.0, "end": 205.0, "reason": "обнажённая сцена", "kind": "nudity"},
    {"start": 330.0, "end": 350.0, "reason": "жестокое насилие", "kind": "violence"},
    {"start": 480.0, "end": 495.0, "reason": "нецензурная брань крупным планом", "kind": "language"},
]


class ScriptedClock:
    """Источник времени, который «проигрывает» фильм с заданной скоростью."""

    id = "scripted"

    def __init__(self, speed: float = 4.0) -> None:
        self.speed = float(speed)
        self.poll_interval = 0.1
        self._t0 = time.monotonic()
        self._paused = False

    def start(self) -> tuple[bool, str]:
        self._t0 = time.monotonic()
        return True, f"Виртуальный плеер запущен (x{self.speed:g})"

    def read(self) -> ClockReading | None:
        elapsed = (time.monotonic() - self._t0) * self.speed
        return ClockReading(position=elapsed, confidence=1.0, raw_text=f"demo:{elapsed:.1f}")

    def stop(self) -> None:
        return None

    def seek(self, position: float) -> None:
        self._t0 = time.monotonic() - position / max(0.01, self.speed)


@dataclass
class DemoStatus:
    running: bool = False
    message: str = ""
    film_id: int | None = None
    blur_events: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "message": self.message,
            "film_id": self.film_id,
            "blur_events": self.blur_events,
        }


class DemoRunner:
    """Управляет жизненным циклом демо. Один экземпляр на приложение."""

    def __init__(self, db: Database, engine: BlurEngine, on_log: Callable[[str], None] | None = None) -> None:
        self.db = db
        self.engine = engine
        self.on_log = on_log or (lambda _m: None)
        self.mock: MockOBS | None = None
        self.status = DemoStatus()
        self._original_blur: BlurController | None = None
        self._original_clock_source: Any = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------- запуск
    def start(self, speed: float = 4.0, port: int = 0) -> DemoStatus:
        with self._lock:
            if self.status.running:
                self.status.message = "Демо уже запущено"
                return self.status

            self.mock = MockOBS("127.0.0.1", port or 4456)
            try:
                actual_port = self.mock.start()
            except OSError as exc:
                self.mock = None
                self.status.message = f"Не удалось поднять мок-OBS: {exc}"
                self.on_log(self.status.message)
                return self.status

            self.mock.on_blur = lambda enabled: self._count_blur(enabled)

            # фильм в базу
            film = self.db.upsert_film(source="demo", **DEMO_FILM)
            self.db.replace_segments(int(film["id"]), DEMO_SEGMENTS, "demo")
            self.status.film_id = int(film["id"])

            # переподключаем движок на мок-OBS
            self._original_blur = self.engine.blur
            controller = BlurController(
                self._original_blur.settings.model_copy(update={"backend": "streamfx"}),
                client=OBSClient("127.0.0.1", actual_port, ""),
            )
            controller.settings.source = "Game Capture"
            controller.settings.scene = "Main"
            controller.settings.filter_name = "StreamBlur Blur"
            # иначе connect() перезапишет порт из настроек (4455) и уйдёт мимо мока
            controller.settings.host = "127.0.0.1"
            controller.settings.port = actual_port
            controller.settings.password = ""
            self.engine.blur = controller
            self.engine.load_film(int(film["id"]))

            # часы → виртуальный плеер. Важно: именно clock.start(), а не ручная
            # установка полей — иначе поток опроса не запускается и время не читается.
            tracker = self.engine.clock
            tracker.stop()
            tracker.manual_override = False
            tracker.build_source = lambda name=None: ScriptedClock(speed=speed)  # type: ignore[method-assign]
            status = tracker.start("scripted")
            if not status.running:
                self.stop()
                self.status.message = f"Не удалось запустить виртуальный плеер: {status.message}"
                return self.status

            self.status.running = True
            self.status.message = (
                f"Демо идёт: фильм «{DEMO_FILM['title']}», скорость x{speed:g}, "
                f"опасные сцены: 1:00, 3:00, 5:30, 8:00"
            )
            self.on_log(self.status.message)
            threading.Thread(target=self._watch, daemon=True).start()
            return self.status

    # --------------------------------------------------------- остановка
    def stop(self) -> DemoStatus:
        with self._lock:
            if not self.status.running:
                self.status.message = "Демо не запущено"
                return self.status
            self.status.running = False
            self.engine.clock.stop()
            self.engine.blur.set_blur(False, reason="демо остановлено")
            try:
                self.engine.blur.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if self._original_blur is not None:
                self.engine.blur = self._original_blur
            if self.mock is not None:
                self.mock.stop()
                self.mock = None
            self.status.message = "Демо остановлено"
            self.on_log(self.status.message)
            return self.status

    # -------------------------------------------------------------- служебное
    def _count_blur(self, enabled: bool) -> None:
        if enabled:
            self.status.blur_events += 1
        self.on_log(f"{'ВКЛ' if enabled else 'выкл'} размытие в OBS (мок)")

    def _watch(self) -> None:  # pragma: no cover - фоновый поток
        """Пишет в лог, когда движок попадает в опасную сцену."""
        last = False
        while self.status.running:
            snapshot = self.engine.snapshot()
            if snapshot.blur_on != last:
                last = snapshot.blur_on
                self.on_log(
                    f"{snapshot.time} → {'БЛЮР ВКЛ: ' + (snapshot.reason or '') if last else 'блюр снят'}"
                )
            time.sleep(0.2)
