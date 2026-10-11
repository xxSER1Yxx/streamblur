"""Базовые типы детекторов и чистые функции предобработки.

Всё, что здесь написано, — чистые функции без внешних зависимостей, поэтому их
можно проверить тестами без скачивания моделей и без GPU.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 80 классов COCO, которые имеют отношение к правилам платформ.
# Детектор распознаёт ПРЕДМЕТ, а не намерение: нож на кухне и нож в драке для
# него одно и то же. Поэтому список короткий и консервативный — лишние классы
# дали бы столько ложных блюров, что стрим стало бы невозможно смотреть.
COCO_CATEGORY_CLASSES: dict[str, dict[int, str]] = {
    # COCO class 24 = knife. Класс «пистолет» в COCO отсутствует.
    # «handbag» (26) намеренно НЕ берём: не оружие, а ложных срабатываний
    # даёт больше всех остальных классов вместе взятых.
    "weapon": {24: "knife"},
    "person": {0: "person"},
    # В COCO нет классов «кровь» и «нагота» — для них нужны отдельные модели
    # (сегментатор и NSFW-классификатор). Профиль может их включить, но
    # срабатываний не будет, и UI честно об этом скажет.
    "blood": {},
    "nudity": {},
    "self_harm": {},
    "drugs": {},
}

# Категории, для которых в программе ЕСТЬ рабочая модель:
#   weapon — детектор предметов на COCO (class 24 = knife);
#   nudity — классификатор sfw/nsfw (см. nsfw_onnx.py).
# Всё остальное профиль может включать, но срабатываний не будет — и UI честно
# об этом говорит, чтобы стример не думал, будто прикрыт полностью.
DETECTABLE_CATEGORIES = ("weapon", "nudity")


@dataclass
class Detection:
    """Одно срабатывание детектора."""

    category: str
    label: str
    confidence: float
    box: tuple[float, float, float, float] | None = None  # x1, y1, x2, y2 в долях кадра

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "confidence": round(float(self.confidence), 3),
            "box": [round(v, 4) for v in self.box] if self.box else None,
        }


@dataclass
class FrameVerdict:
    """Вердикт по одному кадру."""

    blur: bool = False
    category: str = ""
    label: str = ""
    confidence: float = 0.0
    detections: list[Detection] = field(default_factory=list)
    skipped: str = ""   # почему кадр не проверяли (нет модели, пауза и т.п.)

    def as_dict(self) -> dict[str, Any]:
        return {
            "blur": self.blur,
            "category": self.category,
            "label": self.label,
            "confidence": round(float(self.confidence), 3),
            "detections": [d.as_dict() for d in self.detections],
            "skipped": self.skipped,
        }


class Detector:
    """Интерфейс детектора.

    Реализация обязана быть «ленивой»: модель грузится в ``start()``, а её
    отсутствие не должно ронять программу — только выключать страховку.
    """

    id: str = "detector"

    def start(self) -> tuple[bool, str]:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def detect(self, frame) -> list[Detection]:
        """``frame`` — массив HxWx3 (RGB или BGR, как договорились с вызывающим)."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return False


def letterbox(image, size: int) -> tuple[Any, float, int, int]:
    """Вписать кадр в квадрат size×size с сохранением пропорций.

    Возвращает ``(картинка, масштаб, отступ_x, отступ_y)``. Нужно, чтобы координаты
    найденного можно было вернуть в систему координат исходного кадра.

    Реализация через PIL, чтобы не тащить OpenCV в горячий путь.
    """
    from PIL import Image

    if not isinstance(image, Image.Image):
        image = Image.fromarray(image)
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return image, 1.0, 0, 0
    scale = min(size / src_w, size / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = image.resize((new_w, new_h), Image.BILINEAR)
    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(resized, (pad_x, pad_y))
    return canvas, scale, pad_x, pad_y


def to_chw_float(image, size: int) -> Any:
    """PIL-картинка → массив 1×3×size×size float32 в диапазоне 0..1."""
    import numpy as np

    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    return np.transpose(array, (2, 0, 1))[None, ...]


def boxes_to_original(boxes, scale: float, pad_x: int, pad_y: int,
                      src_w: int, src_h: int) -> list[tuple[float, float, float, float]]:
    """Координаты из letterbox-квадрата → доли исходного кадра (0..1)."""
    result = []
    for x1, y1, x2, y2 in boxes:
        ox1 = (x1 - pad_x) / scale if scale else x1
        oy1 = (y1 - pad_y) / scale if scale else y1
        ox2 = (x2 - pad_x) / scale if scale else x2
        oy2 = (y2 - pad_y) / scale if scale else y2
        result.append((
            max(0.0, min(1.0, ox1 / src_w)) if src_w else 0.0,
            max(0.0, min(1.0, oy1 / src_h)) if src_h else 0.0,
            max(0.0, min(1.0, ox2 / src_w)) if src_w else 0.0,
            max(0.0, min(1.0, oy2 / src_h)) if src_h else 0.0,
        ))
    return result


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Пересечение поверх объединения для двух рамок (x1, y1, x2, y2)."""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def non_max_suppression(items: list[tuple[float, tuple[float, ...], str, str]],
                        iou_threshold: float = 0.45) -> list[tuple[float, tuple[float, ...], str, str]]:
    """Убрать дубли: одну и ту же сцену детектор часто находит 5–10 раз.

    ``items`` — список ``(уверенность, рамка, категория, метка)``.
    """
    ordered = sorted(items, key=lambda item: item[0], reverse=True)
    kept: list[tuple[float, tuple[float, ...], str, str]] = []
    for candidate in ordered:
        duplicate = False
        for chosen in kept:
            if iou(candidate[1], chosen[1]) >= iou_threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept
