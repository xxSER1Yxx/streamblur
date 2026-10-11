"""Страховка: локальная нейросеть поверх таймингов.

Зачем она нужна
---------------
Тайминги с сайта могут отсутствовать, быть неточными или относиться к другой
версии фильма. Страховка смотрит на сам кадр и включает блюр, если увидела то,
что запрещено правилами выбранной платформы.

Три правила, ради которых всё устроено именно так
-------------------------------------------------
1. **Страховка никогда не выключает блюр.** Она может только добавить причину
   блюрить. Если тайминги говорят «блюрить», а нейросеть молчит — блюр остаётся.
2. **Нет модели или она упала — стрим продолжается.** Ошибка детектора пишется
   в лог и в статус, но движение фильма не останавливается.
3. **Проверяемый режим.** Пока человек лично не убедился, что срабатывания
   адекватные, страховку держат в ``test_mode``: всё логируется, но блюр не
   включается. Вкладка «Страховка» показывает, что именно и с какой уверенностью
   было найдено.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .detector_base import DETECTABLE_CATEGORIES, Detector, Detection, FrameVerdict
from .profiles import get_profile, profile_status

log = logging.getLogger("streamblur.safety")


@dataclass
class SafetySettings:
    """Настройки страховки. Держатся отдельно, чтобы её можно было выключить целиком."""

    enabled: bool = False
    platform: str = "twitch"
    blur_threshold: float = 0.0      # 0 = брать порог из профиля платформы
    fps: float = 2.0                 # сколько кадров в секунду смотреть
    input_size: int = 640            # размер входа модели
    confidence: float = 0.4          # ниже этого детектор вообще не рассматриваем
    hold_seconds: float = 1.5        # сколько держим блюр после срабатывания
    model_path: str = ""             # детектор предметов; пусто = искать в папке моделей
    nsfw_model_path: str = ""        # классификатор наготы; пусто = искать в папке моделей
    test_mode: bool = True           # логировать, но НЕ блюрить

    @classmethod
    def from_settings(cls, settings: Any) -> "SafetySettings":
        """Собрать из ``Settings.safety`` (pydantic) без жёсткой связки моделей."""
        block = getattr(settings, "safety", None)
        if block is None:
            return cls()
        values = block.model_dump() if hasattr(block, "model_dump") else dict(block)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in values.items() if k in known})


@dataclass
class SafetyStatus:
    running: bool = False
    enabled: bool = False
    platform: str = "twitch"
    model: str = ""
    available: bool = False
    error: str = ""
    frames_checked: int = 0
    detections_total: int = 0
    blurs_triggered: int = 0
    active: bool = False
    last_category: str = ""
    last_confidence: float = 0.0
    last_label: str = ""
    last_seen_at: float | None = None
    test_mode: bool = True
    covered: list[dict[str, Any]] = field(default_factory=list)
    not_covered: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "running": self.running,
            "enabled": self.enabled,
            "platform": self.platform,
            "model": self.model,
            "available": self.available,
            "error": self.error,
            "frames_checked": self.frames_checked,
            "detections_total": self.detections_total,
            "blurs_triggered": self.blurs_triggered,
            "active": self.active,
            "last_category": self.last_category,
            "last_confidence": round(float(self.last_confidence), 3),
            "last_label": self.last_label,
            "last_seen_ago": (
                round(time.time() - self.last_seen_at, 1) if self.last_seen_at else None
            ),
            "test_mode": self.test_mode,
            "covered": self.covered,
            "not_covered": self.not_covered,
        }
        return data


class SafetyNet:
    """Обёртка: детектор + профиль платформы + удержание блюра."""

    def __init__(
        self,
        settings: SafetySettings,
        detector: Detector | None = None,
        grab: Callable[[], Any] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        detector_factory: Callable[[], Detector] | None = None,
    ) -> None:
        self.settings = settings
        self.detector = detector
        # чем строить детектор: по умолчанию составной под категории профиля,
        # в тестах подсовывают свой — чтобы не зависеть от весов на диске
        self._detector_factory = detector_factory
        self._grab = grab
        self.on_event = on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hold_until = 0.0
        self._last_frame_at = 0.0
        self.status = SafetyStatus(
            enabled=settings.enabled,
            platform=settings.platform,
            test_mode=settings.test_mode,
        )
        self.log: list[dict[str, Any]] = []

    # ------------------------------------------------------------- жизненный цикл
    def _build_detector(self) -> Detector:
        """Собрать детектор под категории выбранного профиля.

        Модель, чья категория в профиле выключена, не грузится вовсе: незачем
        держать в памяти 85 МБ весов, которые всё равно не понадобятся.
        """
        from .composite import build_detector

        profile = get_profile(self.settings.platform)
        wanted = {
            category for category in profile.enabled_categories()
            if category in DETECTABLE_CATEGORIES
        }
        return build_detector(self.settings, wanted)

    def start(self) -> tuple[bool, str]:
        """Запустить страховку. Возвращает ``(получилось, сообщение)``.

        Формат ответа такой же, как у ``Detector.start()`` — иначе вызывающий код
        вынужден помнить, кто что возвращает, и спотыкается.
        """
        with self._lock:
            if not self.settings.enabled:
                self.status.enabled = False
                self.status.running = False
                self.status.error = ""
                return False, "страховка выключена в настройках"
            if self.status.running:
                return True, "уже запущена"
            if self.detector is None:
                self.detector = (
                    self._detector_factory() if self._detector_factory
                    else self._build_detector()
                )
            ok, message = self.detector.start()
            self.status.enabled = True
            self.status.running = ok
            self.status.available = ok
            self.status.error = "" if ok else message
            self.status.test_mode = self.settings.test_mode
            self.status.platform = self.settings.platform
            self.status.model = self._describe_models() if ok else ""
            status = profile_status(self.settings.platform)
            self.status.covered = status["covered"]
            self.status.not_covered = status["not_covered"]
            if ok:
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._loop, name="safety-net", daemon=True
                )
                self._thread.start()
                log.info("Страховка запущена: %s", message)
            else:
                log.warning("Страховка не запустилась: %s", message)
            return ok, message

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self.status.running = False
            self.status.available = False
            self._hold_until = 0.0
            detector = self.detector
        if detector is not None:
            try:
                detector.stop()
            except Exception:  # noqa: BLE001
                pass

    def _describe_models(self) -> str:
        """Какие модели реально поднялись — для статуса и вкладки «Страховка»."""
        describe = getattr(self.detector, "describe", None)
        if describe is None:
            path = getattr(self.detector, "model_path", None)
            return path.name if path else self.detector.id
        info = describe()
        parts = info.get("running") or []
        if not parts:
            return self.detector.id
        names = []
        for _, part in getattr(self.detector, "parts", []):
            if part in [p for p in getattr(self.detector, "_started", [])]:
                path = getattr(part, "model_path", None)
                names.append(path.name if path else part.id)
        return ", ".join(names) if names else ", ".join(parts)

    def _loop(self) -> None:  # pragma: no cover - поток
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.status.error = f"{type(exc).__name__}: {exc}"
                log.exception("safety-net: ошибка проверки кадра")
            interval = 1.0 / max(0.2, float(self.settings.fps))
            self._stop.wait(interval)

    # ------------------------------------------------------------------- логика
    def threshold(self) -> float:
        """Порог блюра: явная настройка важнее порога профиля."""
        explicit = float(self.settings.blur_threshold or 0)
        if explicit > 0:
            return explicit
        return float(get_profile(self.settings.platform).blur_threshold)

    def evaluate(self, detections: list[Detection]) -> FrameVerdict:
        """Чистая функция: срабатывания детектора → вердикт по кадру."""
        profile = get_profile(self.settings.platform)
        threshold = self.threshold()
        best: Detection | None = None
        allowed = [
            d for d in detections
            if d.category in DETECTABLE_CATEGORIES and profile.is_enabled(d.category)
        ]
        for detection in allowed:
            if best is None or detection.confidence > best.confidence:
                best = detection
        if best is None:
            return FrameVerdict(detections=detections)
        return FrameVerdict(
            blur=best.confidence >= threshold,
            category=best.category,
            label=best.label,
            confidence=best.confidence,
            detections=detections,
        )

    def check_once(self, frame: Any | None = None) -> FrameVerdict:
        """Проверить один кадр. Используется и потоком, и ручной проверкой."""
        with self._lock:
            if not self.settings.enabled or self.detector is None:
                return FrameVerdict(skipped="страховка выключена")
            if frame is None:
                if self._grab is None:
                    return FrameVerdict(skipped="нет источника кадров")
                frame = self._grab()
                if frame is None:
                    return FrameVerdict(skipped="кадр не получен")
            self.status.frames_checked += 1

        try:
            detections = self.detector.detect(frame)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.status.error = f"{type(exc).__name__}: {exc}"
            return FrameVerdict(skipped=f"детектор упал: {exc}")

        verdict = self.evaluate(detections)
        now = time.time()
        with self._lock:
            if detections:
                self.status.detections_total += len(detections)
                self.status.last_category = verdict.category
                self.status.last_confidence = verdict.confidence
                self.status.last_label = verdict.label
                self.status.last_seen_at = now
            if verdict.blur:
                self._hold_until = now + max(0.0, float(self.settings.hold_seconds))
                if not self.settings.test_mode:
                    self.status.blurs_triggered += 1
            self.status.active = self.is_active_locked(now)
        self._emit(verdict, detections)
        return verdict

    def is_active_locked(self, now: float | None = None) -> bool:
        """Держим ли мы сейчас блюр (с учётом удержания после срабатывания)."""
        if self.settings.test_mode:
            return False
        current = now if now is not None else time.time()
        return current < self._hold_until

    def is_active(self) -> bool:
        with self._lock:
            return self.is_active_locked()

    def _emit(self, verdict: FrameVerdict, detections: list[Detection]) -> None:
        if not detections:
            return
        entry = {
            "at": time.time(),
            "blur": verdict.blur and not self.settings.test_mode,
            "test_mode": self.settings.test_mode,
            "category": verdict.category,
            "label": verdict.label,
            "confidence": round(verdict.confidence, 3),
            "threshold": round(self.threshold(), 3),
            "items": [d.as_dict() for d in detections][:10],
        }
        with self._lock:
            self.log.append(entry)
            del self.log[:-200]
        if self.on_event:
            try:
                self.on_event(entry)
            except Exception:  # noqa: BLE001
                log.exception("safety-net: on_event упал")

    # -------------------------------------------------------------------- отчёт
    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self.log[-limit:]))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = self.status.as_dict()
            data["threshold"] = round(self.threshold(), 3)
            data["detectable_categories"] = list(DETECTABLE_CATEGORIES)
            data["settings"] = {
                "enabled": self.settings.enabled,
                "platform": self.settings.platform,
                "blur_threshold": self.settings.blur_threshold,
                "fps": self.settings.fps,
                "test_mode": self.settings.test_mode,
                "hold_seconds": self.settings.hold_seconds,
                "confidence": self.settings.confidence,
            }
        # Что прикрыто — пересчитываем КАЖДЫЙ раз от выбранного профиля,
        # а не от последнего запуска: список должен быть виден и когда
        # страховка выключена, и когда модель не скачана.
        status = profile_status(self.settings.platform)
        data["profile"] = status
        data["covered"] = status["covered"]
        data["not_covered"] = status["not_covered"]
        return data
