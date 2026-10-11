"""Чтение таймера фильма с экрана через OCR.

Универсальный способ: работает с **любым** плеером и сайтом — VLC, браузер,
приложение ТВ, второй монитор. Пользователь выделяет прямоугольник, где плеер
рисует время, мы снимаем кадр, увеличиваем, гоняем через OCR и парсим ``0:12:34``.

Движок OCR — RapidOCR (onnxruntime, без системных зависимостей). Если он не
установлен, модуль всё равно импортируется, а ``start()`` честно сообщает причину —
в UI останется ручная синхронизация.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from ..config import OcrSettings
from ..timeutils import parse_ocr_time
from .base import ClockReading

log = logging.getLogger("streamblur.clock.ocr")

try:  # захват экрана
    import mss  # type: ignore

    _MSS_OK = True
except Exception:  # pragma: no cover - зависит от платформы
    mss = None  # type: ignore
    _MSS_OK = False

try:  # OCR
    from rapidocr_onnxruntime import RapidOCR  # type: ignore

    _OCR_OK = True
except Exception:  # pragma: no cover - опциональная зависимость
    RapidOCR = None  # type: ignore
    _OCR_OK = False

try:
    import numpy as np
    from PIL import Image, ImageFilter, ImageOps

    _IMG_OK = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    Image = None  # type: ignore
    _IMG_OK = False


@dataclass
class GrabbedFrame:
    width: int
    height: int
    data: Any  # numpy array RGB


class ScreenGrabber:
    """Снимает регион экрана. Отдельный класс — чтобы тесты подсовывали свои кадры."""

    def __init__(self, settings: OcrSettings) -> None:
        self.settings = settings
        self._sct = None

    def start(self) -> tuple[bool, str]:
        if not _MSS_OK:
            return False, "Не установлен пакет mss (pip install mss)"
        try:
            self._sct = mss.mss()
            monitors = self._sct.monitors
            if not monitors:
                return False, "Не найдено ни одного монитора"
            return True, f"Экран доступен: {monitors[0]['width']}x{monitors[0]['height']}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Не удалось открыть экран: {exc}"

    def region(self) -> dict[str, int]:
        s = self.settings
        if s.width > 0 and s.height > 0:
            return {"left": s.left, "top": s.top, "width": s.width, "height": s.height}
        assert self._sct is not None
        mon = self._sct.monitors[0]
        return {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}

    def grab(self) -> GrabbedFrame | None:
        if self._sct is None:
            return None
        try:
            shot = self._sct.grab(self.region())
        except Exception as exc:  # noqa: BLE001
            log.debug("grab failed: %s", exc)
            return None
        if not _IMG_OK:
            return None
        arr = np.asarray(shot)[:, :, :3]  # BGRA → BGR
        return GrabbedFrame(width=shot.width, height=shot.height, data=arr[:, :, ::-1])

    def stop(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None


def preprocess(frame: GrabbedFrame, upscale: int = 3) -> "Image.Image":
    """Кадр → изображение, пригодное для OCR (крупный контрастный текст)."""
    img = Image.fromarray(frame.data)
    img = ImageOps.grayscale(img)
    if upscale and upscale > 1:
        img = img.resize(
            (max(1, img.width * upscale), max(1, img.height * upscale)), Image.LANCZOS
        )
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(3))
    img = img.point(lambda p: 255 if p > 140 else 0)  # плееры рисуют белые цифры
    return img


def ocr_text(image: "Image.Image", engine: Any, threshold: float = 0.6) -> tuple[str, float]:
    """Прогнать изображение через OCR-движок, вернуть текст и уверенность."""
    if engine is None:
        return "", 0.0
    try:
        result = engine(np.array(image))
    except Exception as exc:  # noqa: BLE001
        log.debug("ocr failed: %s", exc)
        return "", 0.0
    texts: list[str] = []
    score = 0.0
    # RapidOCR возвращает (boxes, txts, scores) или (results, elapse) в разных версиях
    items: list[Any] = []
    if isinstance(result, tuple) and result:
        first = result[0]
        if isinstance(first, list) and first and isinstance(first[0], (list, tuple)) and len(first[0]) == 3:
            items = first
        elif isinstance(first, list):
            items = first
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            texts.append(str(item[1]))
            try:
                score = max(score, float(item[2]))
            except (IndexError, ValueError, TypeError):
                score = max(score, 0.7)
        elif isinstance(item, dict):
            texts.append(str(item.get("text", "")))
            score = max(score, float(item.get("score", 0.7) or 0.7))
        else:
            texts.append(str(item))
    if score < threshold and texts:
        return " ".join(texts), score
    return " ".join(texts), score


class OcrClock:
    id = "ocr"

    def __init__(self, settings: OcrSettings, grabber: ScreenGrabber | None = None,
                 engine: Any | None = None) -> None:
        self.settings = settings
        self.poll_interval = max(0.1, float(settings.poll_seconds))
        self._grabber = grabber or ScreenGrabber(settings)
        self._engine = engine
        self._engine_lock = threading.Lock()
        self._engine_error: str | None = None

    # ------------------------------------------------------------- lifecycle
    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if not _OCR_OK:
            self._engine_error = (
                "OCR-движок недоступен: pip install rapidocr-onnxruntime"
            )
            return None
        with self._engine_lock:
            if self._engine is None:
                try:
                    self._engine = RapidOCR()
                except Exception as exc:  # noqa: BLE001
                    self._engine_error = f"Не удалось инициализировать OCR: {exc}"
                    self._engine = None
        return self._engine

    def start(self) -> tuple[bool, str]:
        ok, message = self._grabber.start()
        if not ok:
            return False, message
        if not _IMG_OK:
            return False, "Не установлены Pillow/numpy (pip install -r requirements.txt)"
        engine = self._ensure_engine()
        if engine is None:
            return False, self._engine_error or "OCR недоступен"
        return True, f"OCR готов, регион {self._grabber.region()}"

    # ------------------------------------------------------------------ read
    def read(self) -> ClockReading | None:
        frame = self._grabber.grab()
        if frame is None:
            return None
        engine = self._ensure_engine()
        if engine is None:
            return None
        try:
            image = preprocess(frame, int(self.settings.upscale or 1))
        except Exception as exc:  # noqa: BLE001
            log.debug("preprocess failed: %s", exc)
            return None
        text, score = ocr_text(image, engine, float(self.settings.threshold))
        if not text.strip():
            return None
        position, confidence = parse_ocr_time(text)
        if position is None:
            return None
        return ClockReading(position=position, confidence=confidence, raw_text=text.strip())

    def stop(self) -> None:
        self._grabber.stop()

    # ------------------------------------------------------------- отладка
    def probe(self) -> dict[str, Any]:
        """«Тест OCR» из UI: снять кадр и показать, что распознали."""
        frame = self._grabber.grab()
        if frame is None:
            return {"ok": False, "error": "Не удалось снять кадр с экрана"}
        engine = self._ensure_engine()
        if engine is None:
            return {"ok": False, "error": self._engine_error or "OCR недоступен"}
        image = preprocess(frame, int(self.settings.upscale or 1))
        text, score = ocr_text(image, engine, float(self.settings.threshold))
        position, confidence = parse_ocr_time(text)
        return {
            "ok": True,
            "text": text,
            "position": position,
            "confidence": confidence,
            "ocr_score": round(score, 3),
            "region": self._grabber.region(),
        }

    def probe_image(self) -> "Image.Image | None":
        """Кадр после предобработки — показать пользователю, что видит OCR."""
        frame = self._grabber.grab()
        if frame is None:
            return None
        return preprocess(frame, int(self.settings.upscale or 1))
