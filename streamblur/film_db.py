"""Встроенная база фильмов: поиск, импорт карточек, автообновление метаданных.

Источник карточек — TMDB (по умолчанию) или OMDb. Если ключей нет, база работает
полностью офлайн: фильм добавляется вручную (название + год + студия), а тайминги
подтягиваются из локального файла.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .database import Database
from .timings.imdb_provider import normalize_omdb
from .timings.tmdb_provider import TMDBClient, normalize_film

log = logging.getLogger("streamblur.films")

STUB_GENRES_HINTS = ("horror", "thriller", "ужас", "триллер")


class FilmService:
    def __init__(self, db: Database, tmdb_key: str = "", tmdb_language: str = "ru-RU",
                 omdb_key: str = "") -> None:
        self.db = db
        self.tmdb_key = tmdb_key
        self.tmdb_language = tmdb_language
        self.omdb_key = omdb_key

    # --------------------------------------------------------------- поиск
    def search_remote(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        """Поиск по внешним источникам → список карточек (ещё не в БД)."""
        results: list[dict[str, Any]] = []
        if self.tmdb_key:
            try:
                client = TMDBClient(self.tmdb_key, self.tmdb_language)
                for item in client.search(query, year)[:20]:
                    release = item.get("release_date") or ""
                    results.append(
                        {
                            "title": item.get("title"),
                            "original_title": item.get("original_title"),
                            "year": int(release[:4]) if release[:4].isdigit() else None,
                            "overview": (item.get("overview") or "")[:400],
                            "tmdb_id": item.get("id"),
                            "poster_url": (
                                f"https://image.tmdb.org/t/p/w185{item['poster_path']}"
                                if item.get("poster_path")
                                else None
                            ),
                            "source": "tmdb",
                            "in_db": self.db.get_film(int(item["id"])) is not None
                            if item.get("id")
                            else False,
                        }
                    )
            except (requests.RequestException, ValueError, PermissionError) as exc:
                log.warning("TMDB search failed: %s", exc)
        if not results and self.omdb_key:
            try:
                response = requests.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": self.omdb_key, "s": query, "type": "movie"},
                    timeout=20,
                )
                payload = response.json()
                for item in payload.get("Search", [])[:20]:
                    year_raw = (item.get("Year") or "").split("–")[0]
                    results.append(
                        {
                            "title": item.get("Title"),
                            "year": int(year_raw) if year_raw.isdigit() else None,
                            "imdb_id": item.get("imdbID"),
                            "poster_url": item.get("Poster") or None,
                            "source": "omdb",
                            "in_db": False,
                        }
                    )
            except (requests.RequestException, ValueError) as exc:
                log.warning("OMDb search failed: %s", exc)
        return results

    # -------------------------------------------------------------- импорт
    def import_film(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Добавить/обновить фильм из уже найденной карточки (search_remote)."""
        card = payload
        if payload.get("tmdb_id") and self.tmdb_key:
            try:
                client = TMDBClient(self.tmdb_key, self.tmdb_language)
                card = normalize_film(client.details(int(payload["tmdb_id"])), self.tmdb_language)
            except (requests.RequestException, ValueError, PermissionError) as exc:
                log.warning("TMDB details failed: %s", exc)
        elif payload.get("imdb_id") and self.omdb_key:
            try:
                response = requests.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": self.omdb_key, "i": payload["imdb_id"]},
                    timeout=20,
                )
                data = response.json()
                if data.get("Response") == "True":
                    card = normalize_omdb(data)
            except (requests.RequestException, ValueError) as exc:
                log.warning("OMDb details failed: %s", exc)

        known = {
            "title", "original_title", "year", "release_date", "runtime_minutes", "country",
            "language", "genres", "studios", "production_companies", "overview", "poster_url",
            "tmdb_id", "imdb_id", "source", "raw",
        }
        clean = {k: v for k, v in card.items() if k in known and v is not None}
        clean.setdefault("title", payload.get("title") or "Без названия")
        film = self.db.upsert_film(**clean)
        self.db.log_sync(
            film_id=film["id"], provider=str(card.get("source") or "manual"),
            kind="metadata", status="ok", message="Карточка обновлена",
        )
        return film

    def add_manual(
        self,
        title: str,
        year: int | None = None,
        studios: list[str] | None = None,
        production_companies: list[str] | None = None,
        country: str | None = None,
        runtime_minutes: int | None = None,
        genres: list[str] | None = None,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
        overview: str | None = None,
    ) -> dict[str, Any]:
        film = self.db.upsert_film(
            title=title, year=year, studios=studios or [],
            production_companies=production_companies or [], country=country,
            runtime_minutes=runtime_minutes, genres=genres or [],
            imdb_id=imdb_id, tmdb_id=tmdb_id, overview=overview, source="manual",
        )
        self.db.log_sync(
            film_id=film["id"], provider="manual", kind="metadata", status="ok",
            message="Добавлен вручную",
        )
        return film

    # -------------------------------------------------------- автообновление
    def refresh_film(self, film_id: int) -> dict[str, Any]:
        """Обновить метаданные фильма из внешнего источника."""
        film = self.db.get_film(film_id)
        if not film:
            raise KeyError(f"Фильм #{film_id} не найден")
        if film.get("tmdb_id") and self.tmdb_key:
            try:
                client = TMDBClient(self.tmdb_key, self.tmdb_language)
                card = normalize_film(client.details(int(film["tmdb_id"])), self.tmdb_language)
                known = {
                    "title", "original_title", "year", "release_date", "runtime_minutes",
                    "country", "language", "genres", "studios", "production_companies",
                    "overview", "poster_url", "tmdb_id", "imdb_id", "source", "raw",
                }
                clean = {k: v for k, v in card.items() if k in known and v is not None}
                clean["title"] = film["title"] or clean.get("title", "")
                updated = self.db.upsert_film(**clean)
                self.db.log_sync(
                    film_id=film_id, provider="tmdb", kind="metadata", status="ok",
                    message="Автообновление карточки",
                )
                return updated
            except (requests.RequestException, ValueError, PermissionError) as exc:
                self.db.log_sync(
                    film_id=film_id, provider="tmdb", kind="metadata", status="error",
                    message=str(exc),
                )
                raise
        self.db.log_sync(
            film_id=film_id, provider=film.get("source") or "manual", kind="metadata",
            status="skip", message="Нет внешнего id или API-ключа",
        )
        return film

    def refresh_due(self, interval_seconds: int) -> list[int]:
        """Обновить фильмы, у которых истёк срок давности метаданных."""
        if interval_seconds <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_seconds)
        refreshed: list[int] = []
        for film in self.db.list_films(limit=1000):
            stamp = film.get("updated_at")
            if not stamp:
                continue
            try:
                updated = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated >= cutoff:
                continue
            if not (film.get("tmdb_id") or film.get("imdb_id")):
                continue
            try:
                self.refresh_film(int(film["id"]))
                refreshed.append(int(film["id"]))
            except Exception as exc:  # один фильм не должен ломать весь цикл
                log.warning("refresh_film(%s) failed: %s", film.get("id"), exc)
        return refreshed
