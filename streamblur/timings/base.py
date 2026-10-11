"""Базовый класс провайдера таймингов и реестр провайдеров."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from pydantic import BaseModel


class FieldSpec(BaseModel):
    """Описание одного поля настроек провайдера — по нему UI рисует форму."""

    key: str
    label: str
    type: str = "text"          # text | textarea | url | password | number | select | file
    required: bool = False
    placeholder: str = ""
    help: str = ""
    default: Any = None
    options: list[dict[str, str]] = field(default_factory=list)  # для type=select


class ProviderInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    languages: list[str] = []
    needs_api_key: bool = False
    fields: list[FieldSpec] = []


@dataclass
class TimingsResult:
    """Что вернул провайдер: тайминги и (опционально) карточку фильма."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    film: dict[str, Any] | None = None
    message: str = ""

    def __post_init__(self) -> None:
        # Нормализация: сортировка, отсечение мусора.
        cleaned: list[dict[str, Any]] = []
        for seg in self.segments:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
            if end <= start:
                continue
            cleaned.append(
                {
                    "start": start,
                    "end": end,
                    "kind": seg.get("kind", "nsfw"),
                    "reason": seg.get("reason"),
                    "language": seg.get("language"),
                    "confidence": float(seg.get("confidence", 1.0)),
                }
            )
        cleaned.sort(key=lambda s: s["start"])
        self.segments = cleaned


class TimingsProvider:
    """Интерфейс источника таймингов.

    Подкласс обязан задать ``id``/``name`` и реализовать ``fetch``.
    ``config`` приходит из настроек пользователя (UI) — поэтому провайдер не должен
    хранить состояние между вызовами.
    """

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    languages: ClassVar[list[str]] = []
    needs_api_key: ClassVar[bool] = False
    fields: ClassVar[list[FieldSpec]] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})

    # ------------------------------------------------------------------ API
    def fetch(
        self, film: dict[str, Any], language: str = "ru", film_id: int | None = None
    ) -> TimingsResult:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str]:
        """Быстрая проверка доступности источника. По умолчанию — «всегда ок»."""
        return True, "ok"

    # -------------------------------------------------------------- helpers
    def cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            languages=list(self.languages),
            needs_api_key=self.needs_api_key,
            fields=list(self.fields),
        )


class ProviderRegistry:
    def __init__(self) -> None:
        self._items: dict[str, type[TimingsProvider]] = {}

    def register(self, cls: type[TimingsProvider]) -> type[TimingsProvider]:
        if not cls.id:
            raise ValueError(f"Провайдер {cls.__name__} не задал id")
        self._items[cls.id] = cls
        return cls

    def create(self, provider_id: str, config: dict[str, Any] | None = None) -> TimingsProvider:
        if provider_id not in self._items:
            raise KeyError(provider_id)
        return self._items[provider_id](config)

    def ids(self) -> list[str]:
        return list(self._items)

    def get_class(self, provider_id: str) -> type[TimingsProvider]:
        return self._items[provider_id]

    def describe(self) -> list[ProviderInfo]:
        return [cls().info() for cls in self._items.values()]


registry = ProviderRegistry()


def provider(cls: type[TimingsProvider]) -> type[TimingsProvider]:
    """Декоратор регистрации провайдера.

    Важно: возвращаем класс, иначе ``@provider`` подменит имя модуля на ``None``.
    """
    registry.register(cls)
    return cls
