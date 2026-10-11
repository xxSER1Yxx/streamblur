"""Профили платформ: что запрещено и чем это детектируется.

Профиль — это данные (``profiles.json``), а не код: правила Twitch и YouTube
меняются, и править список категорий должен уметь любой, без пересборки EXE.

Главное, что надо понимать про эту подсистему
---------------------------------------------
Нейросеть **не читает правила платформы**. Она распознаёт предметы и сцены.
Здесь правила вручную переведены в набор категорий и пороги. Поэтому:

* категория может быть включена в профиле, но не детектироваться текущей
  моделью (например, «кровь» — в COCO такого класса нет). Это не скрывается:
  ``profile_status()`` прямо говорит, что прикрыто, а что нет;
* страховка работает **поверх** таймингов, а не вместо них.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .detector_base import DETECTABLE_CATEGORIES

log = logging.getLogger("streamblur.safety")

PROFILES_PATH = Path(__file__).resolve().parent / "profiles.json"

DEFAULT_PROFILE = "twitch"


@dataclass
class Profile:
    """Один профиль платформы."""

    id: str
    label: str
    blur_threshold: float
    categories: dict[str, dict[str, Any]]
    last_reviewed: str = ""
    source_url: str = ""

    def enabled_categories(self) -> list[str]:
        return [name for name, cfg in self.categories.items() if cfg.get("enabled")]

    def is_enabled(self, category: str) -> bool:
        return bool(self.categories.get(category, {}).get("enabled"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "blur_threshold": self.blur_threshold,
            "categories": self.categories,
            "last_reviewed": self.last_reviewed,
            "source_url": self.source_url,
        }


_CACHE: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Не удалось прочитать профили платформ %s: %s", PROFILES_PATH, exc)
            _CACHE = {"profiles": {}, "categories": {}}
    return _CACHE


def reload_profiles() -> None:
    """Перечитать файл профилей (например, после правки пользователем)."""
    global _CACHE
    _CACHE = None


def list_profiles() -> list[Profile]:
    data = _load()
    result: list[Profile] = []
    for profile_id, raw in data.get("profiles", {}).items():
        result.append(
            Profile(
                id=profile_id,
                label=raw.get("label", profile_id),
                blur_threshold=float(raw.get("blur_threshold", 0.55)),
                categories=raw.get("categories", {}),
                last_reviewed=raw.get("last_reviewed", ""),
                source_url=raw.get("source_url", ""),
            )
        )
    return result


def profile_ids() -> list[str]:
    return list(_load().get("profiles", {}))


def get_profile(profile_id: str | None) -> Profile:
    """Профиль по имени; неизвестное имя молча не проходит — берём дефолт."""
    data = _load()
    profiles = data.get("profiles", {})
    key = profile_id if profile_id in profiles else DEFAULT_PROFILE
    raw = profiles.get(key) or {}
    return Profile(
        id=key,
        label=raw.get("label", key),
        blur_threshold=float(raw.get("blur_threshold", 0.55)),
        categories=raw.get("categories", {}),
        last_reviewed=raw.get("last_reviewed", ""),
        source_url=raw.get("source_url", ""),
    )


def category_labels() -> dict[str, str]:
    return dict(_load().get("categories", {}))


def profile_status(profile_id: str | None) -> dict[str, Any]:
    """Что профиль реально прикрывает, а что только обещает.

    Это самый важный метод для честности: если стример выбрал профиль с
    «кровью», а модель её не ловит, он должен это видеть, а не надеяться.
    """
    profile = get_profile(profile_id)
    covered: list[str] = []
    not_covered: list[str] = []
    for name in profile.enabled_categories():
        if name in DETECTABLE_CATEGORIES:
            covered.append(name)
        else:
            not_covered.append(name)
    labels = category_labels()
    return {
        "profile": profile.id,
        "label": profile.label,
        "blur_threshold": profile.blur_threshold,
        "last_reviewed": profile.last_reviewed,
        "source_url": profile.source_url,
        "covered": [{"id": c, "label": labels.get(c, c)} for c in covered],
        "not_covered": [{"id": c, "label": labels.get(c, c)} for c in not_covered],
        "warning": (
            "Эти категории включены в профиле, но текущая модель их не распознаёт: "
            + ", ".join(labels.get(c, c) for c in not_covered)
            if not_covered
            else ""
        ),
    }
