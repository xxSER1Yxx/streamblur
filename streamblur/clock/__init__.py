"""Источники «сколько сейчас времени в фильме».

``ClockTracker`` — общий фасад: держит текущее время, сглаживает сдвиг (offset)
между нашими часами и часами плеера, отдаёт ``position`` движку блюра.

Главное свойство — **пауза**. Между опросами источника время досчитывается
(extrapolation), иначе при OCR раз в полсекунды блюр опаздывал бы на сцену. Но как
только стример ставит фильм на паузу, досчитывать нельзя — иначе блюр «слетит»
ровно на самой опасной сцене. Пауза определяется двумя способами:

* источник сам её знает (браузерный мост отдаёт ``paused``);
* иначе — по неизменности показаний: если ``pause_streak`` опросов подряд таймер
  показывает одно и то же, считаем, что фильм стоит, и замораживаем позицию.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from .base import ClockReading, ClockSource, ClockStatus, ManualClock
from .dom_clock import DomClock
from .ocr_clock import OcrClock

log = logging.getLogger("streamblur.clock")


@dataclass
class ClockTracker:
    settings: Settings
    source: ClockSource | None = None
    position: float = 0.0
    offset: float = 0.0
    last_fix_monotonic: float = 0.0
    last_fix_wall: float = 0.0
    last_raw: float = 0.0
    last_text: str = ""
    confidence: float = 0.0
    last_fix_at: float = 0.0
    running: bool = False
    manual_override: bool = False
    frozen: bool = False
    _pause_streak: int = 0
    _last_seen: float | None = None
    # ВАЖНО: именно RLock — start()/sync_to() держат лок и вызывают status(),
    # а он берёт его же. С обычным Lock это мгновенная самоблокировка потока.
    _lock: threading.RLock = field(default_factory=threading.RLock)
    on_log: Callable[[str], None] | None = None
    on_pause: Callable[[bool], None] | None = None

    def __post_init__(self) -> None:
        # _stop/_thread — ОБЯЗАТЕЛЬНО на экземпляр. Как атрибуты класса они были
        # общими для всех ClockTracker, и остановка одних часов глушила другие.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def build_source(self, name: str | None = None) -> ClockSource:
        name = (name or self.settings.clock.source or "manual").lower()
        if name == "ocr":
            return OcrClock(self.settings.clock.ocr)
        if name == "dom":
            return DomClock(self.settings.clock.dom_url, self.settings.clock.dom_poll_seconds)
        return ManualClock()

    def start(self, source_name: str | None = None) -> ClockStatus:
        with self._lock:
            if self.running:
                return self.status()
            self.source = self.build_source(source_name)
            ok, message = self.source.start()
            if not ok:
                self.source = None
                return ClockStatus(
                    running=False, source=None, position=0.0, confidence=0.0,
                    message=message, text="", offset=0.0,
                )
            self._stop.clear()
            self._reset_tracking()
            self.running = True
            self._thread = threading.Thread(target=self._loop, name="clock", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> None:
        with self._lock:
            source = self.source
            self.source = None
            self.running = False
            self.frozen = False
            self._stop.set()
        # source.stop() — ВНЕ лока: он может блокироваться на захвате экрана
        if source is not None:
            try:
                source.stop()
            except Exception:  # noqa: BLE001
                pass

    def _reset_tracking(self) -> None:
        self._pause_streak = 0
        self._last_seen = None
        self.frozen = False

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:  # pragma: no cover - поток
        assert self.source is not None
        poll = max(0.05, float(self.source.poll_interval))
        while not self._stop.is_set():
            reading = self.source.read()
            if reading is not None:
                self.apply_reading(reading)
            self._stop.wait(poll)

    # ------------------------------------------------------------ обработка
    def apply_reading(self, reading: ClockReading) -> bool:
        """Применить одно показание источника. Возвращает True, если что-то изменилось.

        Вынесено из потока, чтобы логику паузы можно было тестировать напрямую.
        """
        with self._lock:
            if reading.position is None:
                return False
            now_mono = time.monotonic()
            tolerance = max(0.0, float(self.settings.clock.pause_tolerance))
            need = max(1, int(self.settings.clock.pause_streak))
            unchanged = (
                self._last_seen is not None
                and abs(reading.position - self._last_seen) <= tolerance
            )

            # 1) пауза: источник её знает явно
            if reading.paused is True:
                if not self.frozen:
                    self._set_frozen(True, "плеер на паузе")
                self._last_seen = reading.position
                self.position = reading.position
                self.last_fix_monotonic = now_mono
                self.last_fix_wall = time.time()
                self.last_text = reading.raw_text
                self.confidence = reading.confidence
                self.last_fix_at = time.time()
                return True

            # 2) пауза: показания не меняются несколько опросов подряд.
            # Счётчик включает текущее показание: pause_streak=3 → замерзаем
            # на третьем одинаковом значении подряд.
            self._pause_streak = self._pause_streak + 1 if unchanged else 1
            self._last_seen = reading.position

            if self._pause_streak >= need and not self.frozen:
                self._set_frozen(True, f"таймер не меняется {self._pause_streak} опросов")

            # 3) игра возобновилась
            if self.frozen and reading.paused is not True and not unchanged:
                self._set_frozen(False, "воспроизведение продолжилось")

            # 4) сдвиг между нашими часами и часами плеера
            smooth = float(self.settings.clock.offset_smooth)
            if self.last_fix_monotonic and not self.frozen:
                predicted = self.position + (now_mono - self.last_fix_monotonic)
                delta = reading.position - predicted
            else:
                delta = 0.0
            self.offset = (
                delta if not self.last_fix_monotonic else self.offset * (1 - smooth) + delta * smooth
            )

            # 5) перемотка: большой скачок принимаем сразу, без сглаживания
            jump = self.last_fix_monotonic and abs(reading.position - self.position) > 30.0
            if jump:
                self.offset = 0.0
                if self.on_log:
                    self.on_log(
                        f"Перемотка: {self.position:.1f} → {reading.position:.1f} с"
                    )

            self.position = reading.position
            self.last_fix_monotonic = now_mono
            self.last_fix_wall = time.time()
            self.last_raw = reading.position
            self.last_text = reading.raw_text
            self.confidence = reading.confidence
            self.last_fix_at = time.time()
            return True

    def _set_frozen(self, value: bool, reason: str) -> None:
        self.frozen = value
        if self.on_log:
            self.on_log(f"{'Пауза' if value else 'Снято с паузы'}: {reason}")
        if self.on_pause:
            try:
                self.on_pause(value)
            except Exception:  # noqa: BLE001
                log.exception("on_pause callback failed")

    # ------------------------------------------------------------ public API
    def _position_unlocked(self) -> float:
        """Оценка времени БЕЗ взятия лока — для вызовов из уже заблокированных мест."""
        if not self.running or self.manual_override or self.frozen:
            return max(0.0, self.position)
        if self.last_fix_monotonic <= 0:
            return max(0.0, self.position)
        elapsed = time.monotonic() - self.last_fix_monotonic
        if elapsed > max(0.5, float(self.settings.clock.stale_after)):
            return max(0.0, self.position)
        return max(0.0, self.position + elapsed)

    def current_position(self) -> float:
        """Оценка текущего времени фильма.

        На паузе и при протухших данных возвращается последняя достоверная
        позиция — время не «убегает» вперёд.
        """
        with self._lock:
            return self._position_unlocked()

    def is_frozen(self) -> bool:
        with self._lock:
            return self.frozen

    def sync_to(self, position: float) -> ClockStatus:
        """Ручная синхронизация: «сейчас в фильме вот столько»."""
        with self._lock:
            self.position = max(0.0, float(position))
            self.last_fix_monotonic = time.monotonic()
            self.last_fix_wall = time.time()
            self.last_raw = self.position
            self.confidence = 1.0
            self.last_fix_at = time.time()
            self.manual_override = True
            self._reset_tracking()
            message = f"Ручная синхронизация → {self.position:.1f} с"
        if self.on_log:
            self.on_log(message)
        return self.status()

    def release_manual(self) -> None:
        with self._lock:
            self.manual_override = False
            self._reset_tracking()

    def status(self) -> ClockStatus:
        with self._lock:
            return ClockStatus(
                running=self.running,
                source=(self.source.id if self.source else None),
                position=self._position_unlocked(),
                confidence=self.confidence,
                message="",
                text=self.last_text,
                offset=self.offset,
                manual=self.manual_override,
                frozen=self.frozen,
                stale_seconds=(
                    max(0.0, time.time() - self.last_fix_at) if self.last_fix_at else None
                ),
            )
