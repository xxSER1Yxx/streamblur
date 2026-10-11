"""Ядро: сопоставление текущего времени фильма с таймингами и команда OBS.

Здесь живёт вся логика «безопасности стрима»:

* **pre-roll / post-roll** — блюр включается чуть раньше и снимается чуть позже
  интервала (запас на лаг захвата и на неточность OCR);
* **гистерезис** — отдельные пороги включения и выключения, чтобы на границе сцены
  размытие не мигало;
* **PANIC** — принудительный блюр «всё прикрыть», снимается только вручную;
* **fail-safe** — если связь с OBS потеряна во время опасной сцены, состояние
  помечается как ``ERROR``, и UI орёт красным.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import ClockTracker
from .config import Settings
from .database import Database
from .obs.blur import BlurController, BlurState, BlurStatus
from .safety import SafetyNet, SafetySettings
from .timeutils import format_timestamp

log = logging.getLogger("streamblur.engine")


@dataclass
class EngineEvent:
    ts: float
    kind: str          # blur_on | blur_off | panic_on | panic_off | error | info
    message: str
    position: float = 0.0
    segment: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "time": format_timestamp(self.position),
            "kind": self.kind,
            "message": self.message,
            "position": round(self.position, 2),
            "segment": self.segment,
        }


class Timeline:
    """Отсортированный список интервалов + быстрый поиск по времени."""

    def __init__(self, segments: list[dict[str, Any]] | None = None) -> None:
        self.segments: list[dict[str, Any]] = sorted(
            (s for s in (segments or []) if s["end"] > s["start"]),
            key=lambda s: s["start"],
        )

    def __len__(self) -> int:
        return len(self.segments)

    def reload(self, segments: list[dict[str, Any]]) -> None:
        self.segments = sorted(
            (s for s in segments if s["end"] > s["start"]), key=lambda s: s["start"]
        )

    def find(self, position: float) -> dict[str, Any] | None:
        """Интервал, внутри которого находится ``position`` (или None)."""
        lo, hi = 0, len(self.segments) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            seg = self.segments[mid]
            if position < seg["start"]:
                hi = mid - 1
            elif position >= seg["end"]:
                lo = mid + 1
            else:
                return seg
        return None

    def contains(self, position: float) -> bool:
        return self.find(position) is not None

    def next_segment(self, position: float) -> dict[str, Any] | None:
        for seg in self.segments:
            if seg["start"] > position:
                return seg
        return None

    def total_seconds(self) -> float:
        return sum(float(s["end"]) - float(s["start"]) for s in self.segments)


@dataclass
class EngineSnapshot:
    position: float = 0.0
    blur_on: bool = False
    panic: bool = False
    segment: dict[str, Any] | None = None
    reason: str = ""
    next_segment: dict[str, Any] | None = None
    segments_total: int = 0
    clock_running: bool = False
    clock_confidence: float = 0.0
    clock_text: str = ""
    obs_connected: bool = False
    obs_state: str = "off"
    obs_error: str | None = None
    film_id: int | None = None
    film_title: str = ""
    uptime: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    safety: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": round(self.position, 2),
            "time": format_timestamp(self.position),
            "blur_on": self.blur_on,
            "panic": self.panic,
            "segment": self.segment,
            "reason": self.reason,
            "next_segment": self.next_segment,
            "segments_total": self.segments_total,
            "clock_running": self.clock_running,
            "clock_confidence": round(self.clock_confidence, 3),
            "clock_text": self.clock_text,
            "obs_connected": self.obs_connected,
            "obs_state": self.obs_state,
            "obs_error": self.obs_error,
            "film_id": self.film_id,
            "film_title": self.film_title,
            "uptime": round(self.uptime, 1),
            "events": self.events,
            "safety": self.safety,
        }


class BlurEngine:
    def __init__(
        self,
        db: Database,
        clock: ClockTracker,
        blur: BlurController,
        settings_provider: Callable[[], Settings],
        max_events: int = 200,
        safety: SafetyNet | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.blur = blur
        self.settings_provider = settings_provider
        # None = страховка не используется вовсе (тесты, лёгкий режим)
        self.safety: SafetyNet | None = safety
        self.timeline = Timeline()
        self.film_id: int | None = None
        self.film_title: str = ""
        self.panic = False
        self.events: list[EngineEvent] = []
        self.max_events = max_events
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._started_at = time.time()
        self._last_blur = False
        self.on_event: Callable[[EngineEvent], None] | None = None
        # на экземпляр, а не в тело класса: иначе все движки делят один поток
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- тайминги
    def load_film(self, film_id: int) -> EngineSnapshot:
        film = self.db.get_film(film_id)
        if not film:
            raise KeyError(f"Фильм #{film_id} не найден в базе")
        with self._lock:
            self.film_id = film_id
            self.film_title = film["title"]
            self.timeline.reload(self.db.list_segments(film_id))
        self.log(
            "info",
            f"Загружен фильм «{film['title']}»: {len(self.timeline)} интервалов, "
            f"суммарно {format_timestamp(self.timeline.total_seconds())}",
        )
        return self.snapshot()

    def clear_film(self) -> None:
        with self._lock:
            self.film_id = None
            self.film_title = ""
            self.timeline.reload([])
        self.set_blur(False, reason="фильм снят с защиты")

    def reload_segments(self) -> int:
        if self.film_id is None:
            return 0
        with self._lock:
            self.timeline.reload(self.db.list_segments(self.film_id))
            return len(self.timeline)

    # ------------------------------------------------------------------ цикл
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="blur-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        if self.safety is not None:
            self.safety.stop()
        # set_blur снимает снапшот (и берёт лок часов) — поэтому строго вне любых локов
        self.set_blur(False, reason="остановка движка")

    def _loop(self) -> None:  # pragma: no cover - поток
        # ссылка на СВОЙ поток: _thread может обнулиться при остановке,
        # а цикл обязан дожить до конца корректно
        thread = threading.current_thread()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("engine tick failed: %s", exc)
                self.log("error", f"Ошибка движка: {exc}")
            if not thread.is_alive():
                return
            self._stop.wait(max(0.01, self.settings_provider().engine.poll_seconds))

    # -------------------------------------------------------------- логика
    def decide(self, position: float) -> tuple[bool, str, dict[str, Any] | None]:
        """Чистая функция решения: вернуть (блюрить?, причина, интервал)."""
        settings = self.settings_provider()
        pre = float(settings.clock.pre_roll)
        post = float(settings.clock.post_roll)
        if self.panic:
            return True, "PANIC", None
        timeline = self.timeline
        seg = timeline.find(position)
        if seg is not None:
            return True, f'сцена: {seg.get("reason") or seg.get("kind")}', seg
        # гистерезис: чуть до / чуть после интервала
        for candidate in timeline.segments:
            if candidate["start"] - pre <= position < candidate["start"]:
                return True, "pre-roll (запас до сцены)", candidate
            if candidate["end"] < position <= candidate["end"] + post:
                return True, "post-roll (доснять хвост сцены)", candidate
        return False, "", None

    def tick(self) -> EngineSnapshot:
        settings = self.settings_provider()
        position = self.clock.current_position()
        should_blur, reason, seg = self.decide(position)

        # Страховка смотрит на кадр. Она может только ДОБАВИТЬ причину блюрить:
        # если тайминги уже включили блюр, нейросеть не имеет права его снять.
        safety = self.safety
        if safety is not None and safety.settings.enabled:
            if not safety.status.running and not safety.status.error:
                safety.start()
            if safety.is_active():
                if not should_blur:
                    reason = (
                        f"страховка: {safety.status.last_label or safety.status.last_category}"
                    )
                should_blur = True

        status = self.blur.status
        if should_blur and not status.connected and settings.engine.fail_safe == "blur":
            if not self._last_blur:
                self.log(
                    "error",
                    "Опасная сцена, а OBS не подключён — стрим не защищён! "
                    "Нажмите PANIC / переключите сцену-заглушку.",
                    position,
                    seg,
                )
        if should_blur != self._last_blur or (should_blur and status.state != BlurState.ON):
            self.set_blur(should_blur, reason=reason, segment=seg, position=position)
        return self.snapshot(position=position)

    # ----------------------------------------------------------------- блюр
    def set_blur(self, enabled: bool, reason: str = "", segment: dict[str, Any] | None = None,
                 position: float | None = None) -> BlurState:
        status = self.blur.set_blur(enabled, reason=reason)
        # blur.set_blur возвращает BlurStatus, а не BlurState — сравниваем .state
        state = status.state if isinstance(status, BlurStatus) else status
        self._last_blur = enabled and state == BlurState.ON
        if position is None:
            position = self.clock.current_position()
        if enabled and state == BlurState.ON:
            self.log("blur_on", f"БЛЮР ВКЛ: {reason}", position, segment)
        elif not enabled and state == BlurState.OFF:
            self.log("blur_off", "блюр снят", position, segment)
        elif state == BlurState.ERROR:
            self.log("error", f"Не удалось переключить блюр: {status.last_error}", position, segment)
        return state

    def set_panic(self, enabled: bool) -> EngineSnapshot:
        with self._lock:
            self.panic = bool(enabled)
        if enabled:
            self.set_blur(True, reason="PANIC (ручной)")
            self.log("panic_on", "PANIC: всё закрыто размытием")
        else:
            position = self.clock.current_position()
            should, reason, seg = self.decide(position)
            self.set_blur(should, reason=reason or "PANIC снят", segment=seg, position=position)
            self.log("panic_off", "PANIC снят")
        return self.snapshot()

    def blur_now(self, seconds: float = 0.0) -> None:
        """Одноразовый блюр на N секунд (кнопка «Закрыть на 10 с»)."""
        self.set_blur(True, reason=f"ручной блюр {seconds:.0f} с")
        if seconds > 0:
            threading.Timer(seconds, lambda: self.tick()).start()

    # ---------------------------------------------------------------- журнал
    def log(self, kind: str, message: str, position: float = 0.0,
            segment: dict[str, Any] | None = None) -> EngineEvent:
        event = EngineEvent(
            ts=time.time(), kind=kind, message=message, position=position, segment=segment
        )
        with self._lock:
            self.events.append(event)
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events :]
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("on_event callback failed")
        return event

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [e.as_dict() for e in self.events[-limit:][::-1]]

    # -------------------------------------------------------------- снапшот
    def snapshot(self, position: float | None = None) -> EngineSnapshot:
        if position is None:
            position = self.clock.current_position()
        clock_status = self.clock.status()
        blur_status = self.blur.status
        return EngineSnapshot(
            position=position,
            blur_on=blur_status.state == BlurState.ON,
            safety=self.safety.snapshot() if self.safety is not None else None,
            panic=self.panic,
            segment=self.timeline.find(position),
            reason=(
                "PANIC"
                if self.panic
                else (
                    f'{self.timeline.find(position).get("reason")}'
                    if self.timeline.find(position)
                    else ""
                )
            ),
            next_segment=self.timeline.next_segment(position),
            segments_total=len(self.timeline),
            clock_running=clock_status.running,
            clock_confidence=clock_status.confidence,
            clock_text=clock_status.text,
            obs_connected=blur_status.connected,
            obs_state=blur_status.state.value,
            obs_error=blur_status.last_error,
            film_id=self.film_id,
            film_title=self.film_title,
            uptime=time.time() - self._started_at,
            events=self.recent_events(12),
        )
