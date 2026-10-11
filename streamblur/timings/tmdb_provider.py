"""Провайдер ``tmdb`` — карточка фильма из TMDB + тайминги по «ключевым словам».

TMDB не отдаёт таймкоды сцен, поэтому здесь две вещи:

1. **метаданные** (год, студии, компании-производители, страна, жанры) — их же использует
   модуль ``film_db``;
2. **тайминги-заглушка**: если у фильма есть ``adult``-флаг или жанры ужасов/триллера,
   можно сгенерировать «страховочные» интервалы по сетке (``safety_grid``) — чтобы
   стример точно не остался без защиты, пока не подтянул точные тайминги.

API-ключ: https://www.themoviedb.org/settings/api (бесплатно).
"""
from __future__ import annotations

from typing import Any

import requests

from .base import FieldSpec, TimingsProvider, TimingsResult, registry
from .subtitles import merge_segments

API = "https://api.themoviedb.org/3"


def normalize_film(payload: dict[str, Any], language: str = "ru-RU") -> dict[str, Any]:
    """TMDB-ответ → наша карточка фильма."""
    release = payload.get("release_date") or payload.get("first_air_date") or ""
    companies = payload.get("production_companies") or []
    countries = payload.get("production_countries") or []
    return {
        "title": payload.get("title") or payload.get("name") or "",
        "original_title": payload.get("original_title") or payload.get("original_name"),
        "year": int(release[:4]) if release[:4].isdigit() else None,
        "release_date": release or None,
        "runtime_minutes": payload.get("runtime"),
        "country": ", ".join(c.get("name", "") for c in countries) or None,
        "language": payload.get("original_language"),
        "genres": [g.get("name", "") for g in payload.get("genres", [])],
        "studios": [c.get("name", "") for c in companies],
        "production_companies": [c.get("name", "") for c in companies],
        "overview": payload.get("overview"),
        "poster_url": (
            f"https://image.tmdb.org/t/p/w342{payload['poster_path']}"
            if payload.get("poster_path")
            else None
        ),
        "tmdb_id": payload.get("id"),
        "imdb_id": (payload.get("imdb_id") or None),
        "source": "tmdb",
        "raw": payload,
    }


class TMDBClient:
    def __init__(self, api_key: str, language: str = "ru-RU") -> None:
        if not api_key:
            raise ValueError("Не задан TMDB API-ключ (Настройки → База фильмов)")
        self.api_key = api_key
        self.language = language

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params.update({"api_key": self.api_key, "language": self.language})
        response = requests.get(f"{API}{path}", params=params, timeout=20)
        if response.status_code == 401:
            raise PermissionError("TMDB отверг API-ключ (401)")
        response.raise_for_status()
        return response.json()

    def search(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "include_adult": "true"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", **params)
        return data.get("results", [])

    def details(self, tmdb_id: int) -> dict[str, Any]:
        return self._get(f"/movie/{tmdb_id}", append_to_response="credits")

    def find_by_imdb(self, imdb_id: str) -> dict[str, Any] | None:
        return self.find_by_external("imdb", imdb_id)

    def find_by_external(self, source: str, external_id: str) -> dict[str, Any] | None:
        """Найти фильм в TMDB по внешнему id.

        ``source`` — название источника в наших терминах (``kinopoisk``, ``imdb``,
        ``shikimori``, ``letterboxd``, ``tvdb``…). Возвращает полную карточку или None.
        """
        mapping = {
            "kinopoisk": "kinopoisk_id",
            "kp": "kinopoisk_id",
            "imdb": "imdb_id",
            "tmdb": "tmdb_id",
            "shikimori": "shikimori_id",
            "letterboxd": "letterboxd_id",
            "tvdb": "tvdb_id",
            "facebook": "facebook_id",
            "instagram": "instagram_id",
            "twitter": "twitter_id",
            "wikidata": "wikidata_id",
        }
        external_source = mapping.get(str(source).lower())
        if not external_source:
            return None
        data = self._get(f"/find/{external_id}", external_source=external_source)
        results = data.get("movie_results") or []
        if not results:
            return None
        return self.details(int(results[0]["id"]))


@registry.register
class TMDBProvider(TimingsProvider):
    id = "tmdb"
    name = "TMDB (метаданные + страховочная сетка)"
    description = (
        "Тянет год/студии/компании из TMDB и, если фильм помечен как взрослый, "
        "строит страховочные интервалы по сетке времени."
    )
    languages = ["ru-RU", "en-US", "de-DE", "fr-FR", "es-ES", "pt-BR", "it-IT", "pl-PL",
                 "tr-TR", "ja-JP", "ko-KR", "zh-CN", "uk-UA", "ar-SA", "hi-IN"]
    needs_api_key = True
    fields = [
        FieldSpec(key="api_key", label="TMDB API-ключ", type="password"),
        FieldSpec(key="tmdb_language", label="Язык карточки", default="ru-RU"),
        FieldSpec(
            key="safety_grid",
            label="Страховочная сетка, сек (0 = выкл.)",
            type="number",
            default=0,
            help="Для adult-фильмов блюрить каждые N секунд по M — грубая, но рабочая защита",
        ),
        FieldSpec(key="grid_duration", label="Длительность страховки, сек", type="number", default=20),
    ]

    def healthcheck(self) -> tuple[bool, str]:
        if not self.cfg("api_key"):
            return False, "Не задан TMDB API-ключ"
        return True, "ok"

    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        api_key = self.cfg("api_key")
        grid = float(self.cfg("safety_grid", 0) or 0)
        message = "Тайминги не строились (страховочная сетка выключена)"
        segments: list[dict[str, Any]] = []
        runtime = film.get("runtime_minutes") or 120
        if api_key:
            try:
                client = TMDBClient(str(api_key), str(self.cfg("tmdb_language", "ru-RU")))
                details = None
                if film.get("tmdb_id"):
                    details = client.details(int(film["tmdb_id"]))
                elif film.get("imdb_id"):
                    details = client.find_by_imdb(str(film["imdb_id"]))
                if details:
                    runtime = details.get("runtime") or runtime
                    adult = bool(details.get("adult"))
                    if adult and grid > 0:
                        segments = _grid(runtime * 60, grid, float(self.cfg("grid_duration", 20)))
                        message = f"adult=true → страховочная сетка каждые {grid:.0f} с"
            except (requests.RequestException, ValueError, PermissionError) as exc:
                message = f"TMDB недоступен: {exc}"
        elif grid > 0:
            segments = _grid(runtime * 60, grid, float(self.cfg("grid_duration", 20)))
            message = f"Без API-ключа: сетка каждые {grid:.0f} с"
        return TimingsResult(merge_segments(segments, gap=0.0), message=message)


def _grid(total_seconds: float, step: float, duration: float) -> list[dict[str, Any]]:
    """Страховочные интервалы каждые ``step`` секунд по ``duration``."""
    out: list[dict[str, Any]] = []
    t = 0.0
    while t < total_seconds:
        out.append(
            {
                "start": t,
                "end": min(total_seconds, t + duration),
                "kind": "nsfw",
                "reason": "safety grid",
                "confidence": 0.3,
            }
        )
        t += step
    return out
