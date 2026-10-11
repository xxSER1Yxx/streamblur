"""Страховка: локальная нейросеть поверх таймингов.

Включается в настройках, по умолчанию **выключена** и в режиме проверки
(``test_mode``) — то есть пишет, что увидела, но блюр не трогает, пока человек
лично не убедится, что срабатывания адекватные.

Состав:
    ``profiles``        что запрещено на Twitch / YouTube / Kick (данные, не код)
    ``detector_base``   интерфейс детектора + чистые функции (letterbox, NMS)
    ``yolo_onnx``       детектор на ONNX-весах YOLO, без PyTorch
    ``safety_net``      оркестратор: детектор + профиль + удержание блюра
"""
from __future__ import annotations

from .detector_base import (
    DETECTABLE_CATEGORIES,
    Detector,
    Detection,
    FrameVerdict,
    boxes_to_original,
    iou,
    letterbox,
    non_max_suppression,
    to_chw_float,
)
from .profiles import (
    DEFAULT_PROFILE,
    Profile,
    category_labels,
    get_profile,
    list_profiles,
    profile_ids,
    profile_status,
    reload_profiles,
)
from .safety_net import SafetyNet, SafetySettings, SafetyStatus
from .yolo_onnx import YoloOnnxDetector, default_models_dir, describe_environment

__all__ = [
    "DETECTABLE_CATEGORIES",
    "DEFAULT_PROFILE",
    "Detector",
    "Detection",
    "FrameVerdict",
    "Profile",
    "SafetyNet",
    "SafetySettings",
    "SafetyStatus",
    "YoloOnnxDetector",
    "boxes_to_original",
    "category_labels",
    "default_models_dir",
    "describe_environment",
    "get_profile",
    "iou",
    "letterbox",
    "list_profiles",
    "non_max_suppression",
    "profile_ids",
    "profile_status",
    "reload_profiles",
    "to_chw_float",
]
