"""Детектор на ONNX-модели семейства YOLO.

Зависимости — только ``onnxruntime`` и ``numpy``, которые уже нужны программе
для OCR таймера. Никакого PyTorch и пакета ``ultralytics``: они добавили бы
в EXE ещё ~2 ГБ, что для «лёгкой программы» неприемлемо.

⚠️ Честно про проверку
---------------------
Веса модели в репозиторий не входят (их нельзя распространять вместе с кодом без
отдельного соблюдения лицензии модели). Поэтому:

* код предобработки и постобработки (letterbox, NMS, пересчёт координат) покрыт
  тестами на синтетических данных и работает без весов;
* сам прогон реальной модели **я проверить не могу** — для этого нужен файл
  весов. Команда ``python -m streamblur safety-check`` и вкладка «Страховка»
  в панели существуют ровно для того, чтобы это проверил человек на живом видео.

Скачать веса: ``python -m streamblur safety-models``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .detector_base import (
    COCO_CATEGORY_CLASSES,
    DETECTABLE_CATEGORIES,
    Detector,
    Detection,
    boxes_to_original,
    letterbox,
    non_max_suppression,
    to_chw_float,
)

log = logging.getLogger("streamblur.safety")

# Куда кладём веса и где их ищем, если путь не задан явно.
MODELS_DIRNAME = "models"


def default_models_dir() -> Path:
    """Папка с весами: рядом с программой в сборке, иначе в каталоге данных."""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / MODELS_DIRNAME
    from ..config import Settings

    return Path(Settings().data_dir) / MODELS_DIRNAME


def parse_yolo_output(
    output,
    *,
    confidence_threshold: float,
    allowed_categories: set[str],
    class_map: dict[int, str] | None = None,
    iou_threshold: float = 0.45,
) -> list[tuple[float, tuple[float, ...], str, str]]:
    """Разобрать выход YOLOv8/YOLO11 в список срабатываний.

    Чистая функция: на вход — массив из onnxruntime, на выход —
    ``[(уверенность, рамка, категория, метка)]``. Тестируется без весов.

    Поддержаны две раскладки выхода:
    ``(1, 84, 8400)`` — YOLOv8/YOLO11, классы по строкам;
    ``(1, 8400, 84)`` — transpose-вариант, классы по столбцам.
    """
    import numpy as np

    array = np.asarray(output, dtype=np.float32)
    if array.ndim == 3:
        array = array[0]
    if array.ndim != 2:
        return []
    # приводим к виду (N, 4 + classes): координаты в первых четырёх столбцах
    if array.shape[0] < array.shape[1]:
        array = array.T
    if array.shape[1] < 5:
        return []

    boxes = array[:, :4]
    scores = array[:, 4:]

    # cx, cy, w, h → x1, y1, x2, y2
    x1 = boxes[:, 0] - boxes[:, 2] / 2.0
    y1 = boxes[:, 1] - boxes[:, 3] / 2.0
    x2 = boxes[:, 0] + boxes[:, 2] / 2.0
    y2 = boxes[:, 1] + boxes[:, 3] / 2.0

    class_ids = scores.argmax(axis=1)
    confidences = scores.max(axis=1)

    # класс → категория профиля; если класс нам не интересен, не тратим время
    lookup: dict[int, tuple[str, str]] = {}
    for category, classes in (class_map or COCO_CATEGORY_CLASSES).items():
        for class_id, label in classes.items():
            lookup[class_id] = (category, label)

    raw: list[tuple[float, tuple[float, ...], str, str]] = []
    for index in range(confidences.shape[0]):
        confidence = float(confidences[index])
        if confidence < confidence_threshold:
            continue
        class_id = int(class_ids[index])
        mapped = lookup.get(class_id)
        if mapped is None:
            continue
        category, label = mapped
        if allowed_categories and category not in allowed_categories:
            continue
        raw.append((
            confidence,
            (float(x1[index]), float(y1[index]), float(x2[index]), float(y2[index])),
            category,
            label,
        ))
    return non_max_suppression(raw, iou_threshold=iou_threshold)


class YoloOnnxDetector(Detector):
    """Детектор оружия/предметов на ONNX-весах YOLO."""

    id = "yolo-onnx"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        input_size: int = 640,
        confidence_threshold: float = 0.4,
        categories: tuple[str, ...] = DETECTABLE_CATEGORIES,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.input_size = int(input_size)
        self.confidence_threshold = float(confidence_threshold)
        self.categories = set(categories)
        self._session = None
        self._error = ""

    # ------------------------------------------------------------ жизненный цикл
    def _find_model(self) -> Path | None:
        if self.model_path and self.model_path.exists():
            return self.model_path
        folder = self.model_path.parent if self.model_path else default_models_dir()
        if not folder.exists():
            return None
        # ВАЖНО: не брать «первый попавшийся .onnx». В папке лежит ещё модель
        # наготы, и если загрузить её как YOLO, onnxruntime будет падать на
        # каждом кадре, а статус соврёт, что детектор оружия запущен.
        from .models import KNOWN_MODELS

        foreign = {m.filename for m in KNOWN_MODELS if m.detector != "yolo"}
        candidates = [
            p for p in sorted(folder.glob("*.onnx")) if p.name not in foreign
        ]
        return candidates[0] if candidates else None

    def start(self) -> tuple[bool, str]:
        if self._session is not None:
            return True, "уже запущен"
        model = self._find_model()
        if model is None:
            self._error = (
                f"модель не найдена в {default_models_dir()}. "
                "Скачайте веса: python -m streamblur safety-models"
            )
            return False, self._error
        try:
            import onnxruntime as ort

            providers = ["CPUExecutionProvider"]
            # CUDA — только если рантайм собран с ней; иначе onnxruntime ругается в лог
            if "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, "CUDAExecutionProvider")
            self._session = ort.InferenceSession(str(model), providers=providers)
        except Exception as exc:  # noqa: BLE001 — страховка не должна ронять стрим
            self._error = f"{type(exc).__name__}: {exc}"
            self._session = None
            return False, self._error
        self.model_path = model
        return True, f"модель загружена: {model.name}"

    def stop(self) -> None:
        self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    @property
    def error(self) -> str:
        return self._error

    # -------------------------------------------------------------------- вывод
    def detect(self, frame) -> list[Detection]:
        """``frame`` — numpy-массив HxWx3 в RGB."""
        if self._session is None:
            return []
        try:
            canvas, scale, pad_x, pad_y = letterbox(frame, self.input_size)
            tensor = to_chw_float(canvas, self.input_size)
            input_name = self._session.get_inputs()[0].name
            outputs = self._session.run(None, {input_name: tensor})
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("детектор не отработал кадр: %s", exc)
            return []

        height, width = frame.shape[0], frame.shape[1]
        items = parse_yolo_output(
            outputs[0],
            confidence_threshold=self.confidence_threshold,
            allowed_categories=self.categories,
        )
        if not items:
            return []
        boxes = boxes_to_original(
            [item[1] for item in items], scale, pad_x, pad_y, width, height
        )
        return [
            Detection(category=item[2], label=item[3], confidence=item[0], box=boxes[i])
            for i, item in enumerate(items)
        ]


def describe_environment() -> dict[str, Any]:
    """Что доступно в этой системе — для вкладки «Страховка» и ``doctor``."""
    info: dict[str, Any] = {
        "models_dir": str(default_models_dir()),
        "model_files": [],
        "onnxruntime": None,
        "providers": [],
    }
    folder = default_models_dir()
    if folder.exists():
        info["model_files"] = [
            {"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1)}
            for p in sorted(folder.glob("*.onnx"))
        ]
    try:
        import onnxruntime as ort

        info["onnxruntime"] = ort.__version__
        info["providers"] = ort.get_available_providers()
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
