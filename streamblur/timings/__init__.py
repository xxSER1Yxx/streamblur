"""Реестр провайдеров таймингов.

Провайдер — это обычный Python-класс с атрибутом ``id`` и методом ``fetch``.
Добавить свой сайт = положить файл в этот пакет (см. ``docs/PROVIDERS.md``).
Реестр собирается автоматически по импорту модулей пакета.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Any

from .base import ProviderInfo, TimingsProvider, TimingsResult, registry

_MODULE_IMPORT_ERRORS: dict[str, str] = {}

# В сборке PyInstaller (``sys.frozen``) файлы пакета лежат в архиве, и
# ``pkgutil.iter_modules`` их не видит — провайдеры молча пропали бы из списка.
# Поэтому список модулей продублирован здесь и импортируется напрямую.
_PROVIDER_MODULES = (
    "generic_http",
    "imdb_provider",
    "link_auto",
    "local_file",
    "opensubtitles",
    "tmdb_provider",
)


def _import(name: str) -> None:
    try:
        importlib.import_module(f"{__name__}.{name}")
    except Exception as exc:  # провайдер с недостающей зависимостью не роняет всё
        _MODULE_IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"


def discover() -> None:
    """Импортировать все модули пакета, чтобы провайдеры зарегистрировались."""
    if getattr(sys, "frozen", False):
        for name in _PROVIDER_MODULES:
            _import(name)
        return
    for mod in pkgutil.iter_modules(__path__):
        name = mod.name
        if name.startswith("_") or name in {"base"}:
            continue
        _import(name)


discover()


def get_provider(provider_id: str, config: dict[str, Any] | None = None) -> TimingsProvider:
    """Готовый к работе экземпляр провайдера.

    Возвращает **инстанс**, а не класс: ``get_provider("local-file", {"path": ...})``.
    """
    try:
        return registry.create(provider_id, config)
    except KeyError as exc:
        available = ", ".join(sorted(registry.ids())) or "—"
        raise KeyError(f"Провайдер '{provider_id}' не найден. Доступны: {available}") from exc


def list_providers() -> list[ProviderInfo]:
    return registry.describe()


def list_provider_dicts() -> list[dict[str, Any]]:
    return [info.model_dump() for info in registry.describe()]


__all__ = [
    "ProviderInfo",
    "TimingsProvider",
    "TimingsResult",
    "registry",
    "get_provider",
    "list_providers",
    "list_provider_dicts",
    "_MODULE_IMPORT_ERRORS",
]
