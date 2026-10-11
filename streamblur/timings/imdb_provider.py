"""Провайдер ``imdb`` — метаданные и «parental guide» через OMDb API.

OMDb (https://www.omdbapi.com) отдаёт рейтинг, ``Content Rating`` и год — удобно,
когда TMDB-ключа нет. Тайминги строятся как страховочная сетка для фильмов с
рейтингом R/NC-17/18+.
"""
from __future__ import annotations

from typing import Any

import requests

from .base import FieldSpec, TimingsProvider, TimingsResult, registry
from .subtitles import merge_segments

API = "https://www.omdbapi.com/"
ADULT_RATINGS = {"r", "nc-17", "nc17", "x", "18", "18+", " unrated"}


def normalize_omdb(payload: dict[str, Any]) -> dict[str, Any]:
    year_raw = (payload.get("Year") or "").split("–")[0].strip()
    year = int(year_raw) if year_raw.isdigit() else None
    runtime = (payload.get("Runtime") or "").replace("min", "").strip()
    return {
        "title": payload.get("Title"),
        "original_title": payload.get("Title"),
        "year": year,
        "release_date": payload.get("Released"),
        "runtime_minutes": int(runtime) if runtime.isdigit() else None,
        "country": payload.get("Country"),
        "language": payload.get("Language"),
        "genres": [g.strip() for g in (payload.get("Genre") or "").split(",") if g.strip()],
        "studios": [],
        "production_companies": [
            c.strip() for c in (payload.get("Production") or "").split(",") if c.strip()
        ],
        "overview": payload.get("Plot"),
        "poster_url": payload.get("Poster") or None,
        "imdb_id": payload.get("imdbID"),
        "source": "omdb",
        "raw": payload,
    }


@registry.register
class IMDbProvider(TimingsProvider):
    id = "imdb"
    name = "IMDb / OMDb (рейтинг + сетка)"
    description = (
        "Метаданные из OMDb; для фильмов с рейтингом R/NC-17 строит страховочные "
        "интервалы. Нужен бесплатный ключ omdbapi.com."
    )
    languages = ["en", "ru"]
    needs_api_key = True
    fields = [
        FieldSpec(key="api_key", label="OMDb API-ключ", type="password", required=True),
        FieldSpec(key="safety_grid", label="Шаг сетки, сек (0 = выкл.)", type="number", default=0),
        FieldSpec(key="grid_duration", label="Длительность страховки, сек", type="number", default=20),
    ]

    def healthcheck(self) -> tuple[bool, str]:
        if not self.cfg("api_key"):
            return False, "Не задан OMDb API-ключ"
        return True, "ok"

    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        key = self.cfg("api_key")
        if not key:
            raise ValueError("Не задан OMDb API-ключ")
        params: dict[str, Any] = {"apikey": str(key), "plot": "short"}
        if film.get("imdb_id"):
            params["i"] = film["imdb_id"]
        else:
            params["t"] = film.get("title")
            if film.get("year"):
                params["y"] = film["year"]
        response = requests.get(API, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("Response") != "True":
            return TimingsResult([], message=f"OMDb: {payload.get('Error', 'не найдено')}")

        card = normalize_omdb(payload)
        rating = (payload.get("Rated") or "").lower().strip()
        grid = float(self.cfg("safety_grid", 0) or 0)
        runtime = card.get("runtime_minutes") or film.get("runtime_minutes") or 120
        segments: list[dict[str, Any]] = []
        message = f"Рейтинг: {rating or 'н/д'}"
        if rating in ADULT_RATINGS and grid > 0:
            t = 0.0
            total = float(runtime) * 60
            while t < total:
                segments.append(
                    {
                        "start": t,
                        "end": min(total, t + float(self.cfg("grid_duration", 20))),
                        "kind": "nsfw",
                        "reason": f"safety grid ({rating})",
                        "confidence": 0.3,
                    }
                )
                t += grid
            message += f" → страховочная сетка каждые {grid:.0f} с"
        return TimingsResult(merge_segments(segments, gap=0.0), film=card, message=message)
