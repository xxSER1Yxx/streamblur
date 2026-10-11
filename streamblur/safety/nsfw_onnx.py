"""Классификатор наготы на ONNX (ViT, sfw/nsfw).

Закрывает категорию ``nudity`` — главную причину банов, которую детектор
предметов на COCO не ловит в принципе.

Предобработка берётся из ``preprocessor_config.json`` модели:
размер 384×384, rescale 1/255, нормализация ``(x - 0.5) / 0.5``.
Менять эти числа нельзя: с другими модель молча начнёт врать.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .detector_base import Detector, Detection
from .models import KNOWN_MODELS, find_model, models_dir

log = logging.getLogger("streamblur.safety")

INPUT_SIZE = 384
NORM_MEAN = 0.5
NORM_STD = 0.5


def preprocess(image, size: int = INPUT_SIZE):
    """Кадр (PIL или numpy RGB) → тензор 1×3×size×size float32."""
    import numpy as np
    from PIL import Image

    if not isinstance(image, Image.Image):
        image = Image.fromarray(image)
    resized = image.convert("RGB").resize((size, size), Image.BICUBIC)
    array = np.asarray(resized, dtype=np.float32) / 255.0
    array = (array - NORM_MEAN) / NORM_STD
    return np.transpose(array, (2, 0, 1))[None, ...]


def softmax(values) -> list[float]:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    shifted = np.exp(array - array.max())
    normalized = shifted / shifted.sum()
    return [float(v) for v in normalized]


def find_nsfw_model(explicit: str | Path | None = None) -> Path | None:
    """Найти веса: явный путь → известная модель в папке → любой *nsfw*.onnx."""
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path
        return None
    info = next((m for m in KNOWN_MODELS if m.category == "nudity"), None)
    if info is not None:
        found = find_model(info)
        if found is not None:
            return found
    folder = models_dir()
    if not folder.exists():
        return None
    for candidate in sorted(folder.glob("*.onnx")):
        if "nsfw" in candidate.name.lower() or "nude" in candidate.name.lower():
            return candidate
    return None


class NsfwOnnxClassifier(Detector):
    """Бинарный классификатор sfw/nsfw."""

    id = "nsfw-onnx"
    category = "nudity"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        labels: tuple[str, ...] = ("sfw", "nsfw"),
        input_size: int = INPUT_SIZE,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.labels = labels
        self.input_size = int(input_size)
        self._session = None
        self._error = ""

    def start(self) -> tuple[bool, str]:
        if self._session is not None:
            return True, "уже запущен"
        path = self.model_path if self.model_path and self.model_path.exists() else None
        if path is None:
            path = find_nsfw_model(self.model_path)
        if path is None:
            self._error = (
                f"модель наготы не найдена в {models_dir()}. "
                "Скачайте: python -m streamblur safety-models --download"
            )
            return False, self._error
        try:
            import onnxruntime as ort

            providers = ["CPUExecutionProvider"]
            if "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, "CUDAExecutionProvider")
            self._session = ort.InferenceSession(str(path), providers=providers)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._session = None
            return False, self._error
        self.model_path = path
        return True, f"модель наготы загружена: {path.name}"

    def stop(self) -> None:
        self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    @property
    def error(self) -> str:
        return self._error

    def classify(self, frame) -> tuple[str, float]:
        """Вернуть (метка, вероятность). При ошибке — ('', 0.0)."""
        if self._session is None:
            return "", 0.0
        try:
            tensor = preprocess(frame, self.input_size)
            input_name = self._session.get_inputs()[0].name
            output = self._session.run(None, {input_name: tensor})[0][0]
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("классификатор наготы не отработал кадр: %s", exc)
            return "", 0.0
        probabilities = softmax(output)
        index = int(max(range(len(probabilities)), key=probabilities.__getitem__))
        label = self.labels[index] if index < len(self.labels) else str(index)
        return label, probabilities[index]

    def detect(self, frame) -> list[Detection]:
        """Интерфейс детектора: срабатывание, если модель сказала «не sfw».

        Рамки нет — классификатор смотрит на весь кадр, а не ищет объект.
        """
        label, probability = self.classify(frame)
        if not label or label == "sfw":
            return []
        return [
            Detection(
                category=self.category,
                label=label,
                confidence=probability,
                box=(0.0, 0.0, 1.0, 1.0),
            )
        ]
