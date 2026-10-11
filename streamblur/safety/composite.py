"""Составной детектор: несколько моделей за один проход кадра.

Зачем: наготу ловит классификатор, оружие — детектор предметов. Гнать обе модели
на каждом кадре wasteful, поэтому составной детектор включает только те, чьи
категории реально включены в профиле платформы.
"""
from __future__ import annotations

import logging
from typing import Any

from .detector_base import Detector, Detection

log = logging.getLogger("streamblur.safety")


class CompositeDetector(Detector):
    """Прогоняет кадр через несколько детекторов и склеивает результаты."""

    id = "composite"

    def __init__(self, detectors: list[tuple[str, Detector]] | None = None) -> None:
        # (категория, детектор) — категория нужна, чтобы включать по профилю
        self.parts: list[tuple[str, Detector]] = list(detectors or [])
        self.errors: dict[str, str] = {}
        self._started: list[Detector] = []

    def add(self, category: str, detector: Detector) -> None:
        self.parts.append((category, detector))

    def start(self) -> tuple[bool, str]:
        """Запустить все части. Хоть одна поднялась — работаем."""
        messages: list[str] = []
        for category, detector in self.parts:
            try:
                ok, message = detector.start()
            except Exception as exc:  # noqa: BLE001 — одна модель не тянет за собой остальные
                ok, message = False, f"{type(exc).__name__}: {exc}"
            if ok:
                self._started.append(detector)
                messages.append(f"{category}: {message}")
            else:
                self.errors[category] = message
                messages.append(f"{category}: НЕ запущена ({message})")
        return bool(self._started), "; ".join(messages)

    def stop(self) -> None:
        for detector in self._started:
            try:
                detector.stop()
            except Exception:  # noqa: BLE001
                pass
        self._started = []

    @property
    def available(self) -> bool:
        return bool(self._started)

    def detect(self, frame) -> list[Detection]:
        found: list[Detection] = []
        for detector in self._started:
            try:
                found.extend(detector.detect(frame))
            except Exception as exc:  # noqa: BLE001
                log.warning("детектор %s упал на кадре: %s", detector.id, exc)
        return found

    def describe(self) -> dict[str, Any]:
        return {
            "running": [d.id for d in self._started],
            "errors": dict(self.errors),
        }


def build_detector(settings, categories: set[str]):
    """Собрать детектор под набор категорий профиля.

    Не создаёт модель, которая всё равно не будет использоваться: незачем
    грузить 85 МБ весов, если в профиле выключена нагота.
    """
    from .nsfw_onnx import NsfwOnnxClassifier
    from .yolo_onnx import YoloOnnxDetector

    composite = CompositeDetector()
    if "nudity" in categories:
        composite.add(
            "nudity",
            NsfwOnnxClassifier(getattr(settings, "nsfw_model_path", "") or None),
        )
    if categories - {"nudity"}:
        composite.add(
            "weapon",
            YoloOnnxDetector(
                getattr(settings, "model_path", "") or None,
                input_size=getattr(settings, "input_size", 640),
                confidence_threshold=getattr(settings, "confidence", 0.4),
                categories=tuple(sorted(categories - {"nudity"})),
            ),
        )
    return composite
