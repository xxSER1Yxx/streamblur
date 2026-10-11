"""Сервис синхронизации: «подтянуть тайминги для фильма из выбранного провайдера».

Один вход — ``sync_film``. Его дёргают и кнопка в UI, и планировщик
(день/неделя/месяц), и CLI.
"""
from __future__ import annotations

import logging
from typing import Any

from .database import Database
from .film_db import FilmService
from .timings import get_provider

log = logging.getLogger("streamblur.sync")


class SyncService:
    def __init__(self, db: Database, films: FilmService) -> None:
        self.db = db
        self.films = films

    # ------------------------------------------------------------- тайминги
    def sync_film(
        self,
        film_id: int,
        provider_id: str,
        config: dict[str, Any] | None = None,
        language: str = "ru",
    ) -> dict[str, Any]:
        film = self.db.get_film(film_id)
        if film is None:
            raise KeyError(f"Фильм #{film_id} не найден")
        provider = get_provider(provider_id, config or {})
        try:
            result = provider.fetch(film, language=language, film_id=film_id)
        except Exception as exc:  # провайдер может бросить что угодно (сеть, файл, API)
            self.db.log_sync(
                film_id=film_id, provider=provider_id, kind="timings", status="error",
                message=f"{type(exc).__name__}: {exc}",
            )
            return {
                "ok": False,
                "film_id": film_id,
                "provider": provider_id,
                "segments": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        if result.film:
            self.films.import_film(result.film)

        count = self.db.replace_segments(film_id, result.segments, provider_id)
        self.db.log_sync(
            film_id=film_id, provider=provider_id, kind="timings", status="ok",
            segments=count, message=result.message or f"{count} интервалов",
        )
        return {
            "ok": True,
            "film_id": film_id,
            "provider": provider_id,
            "segments": count,
            "message": result.message,
        }

    # --------------------------------------------------- автообновление всего
    def sync_all(self, provider_id: str, config: dict[str, Any] | None, language: str,
                 interval_seconds: int) -> dict[str, Any]:
        """Обновить метаданные + тайминги для всех фильмов в базе (задача планировщика)."""
        refreshed = self.films.refresh_due(interval_seconds)
        results: list[dict[str, Any]] = []
        errors = 0
        for film in self.db.list_films(limit=1000):
            try:
                outcome = self.sync_film(int(film["id"]), provider_id, config, language)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                outcome = {"ok": False, "error": str(exc)}
            results.append({"film": film["title"], **outcome})
            if not outcome.get("ok"):
                errors += 0
        return {
            "metadata_refreshed": refreshed,
            "films": len(results),
            "errors": sum(1 for r in results if not r.get("ok")),
            "results": results[:50],
        }
