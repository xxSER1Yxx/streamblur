"""Провайдер ``opensubtitles`` — тайминги из субтитров OpenSubtitles.com.

Использует официальный REST API (``https://api.opensubtitles.com/api/v1``).
Нужен бесплатный API-ключ: https://www.opensubtitles.com/ru/consumers

Логика: ищем субтитры по IMDb/TMDB id → берём лучшую дорожку нужного языка →
качаем файл → ищем в репликах маркеры опасного контента → получаем интервалы.
"""
from __future__ import annotations

import gzip
import io
from typing import Any

import requests

from .base import FieldSpec, TimingsProvider, TimingsResult, registry
from .subtitles import cues_to_segments, parse_subtitles

API_BASE = "https://api.opensubtitles.com/api/v1"
CONSUMER = "opensubtitles"
USER_AGENT = "StreamBlur v0.1"


def _extract_text(content: bytes, encoding: str | None) -> str:
    if content[:2] == b"\x1f\x8b":  # gzip
        content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
    for enc in [encoding, "utf-8", "utf-8-sig", "cp1251", "latin-1"]:
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


@registry.register
class OpenSubtitlesProvider(TimingsProvider):
    id = CONSUMER
    name = "OpenSubtitles (по субтитрам)"
    description = (
        "Ищет дорожку субтитров нужного языка и помечает сцены, "
        "где в репликах есть маркеры насилия/наготы. Нужен бесплатный API-ключ."
    )
    languages = ["ru", "en", "uk", "de", "fr", "es", "pt", "it", "pl", "tr", "ja", "ko", "zh"]
    needs_api_key = True
    fields = [
        FieldSpec(key="api_key", label="API-ключ OpenSubtitles", type="password", required=True),
        FieldSpec(
            key="mode",
            label="Что считать опасным",
            type="select",
            default="markers",
            options=[
                {"id": "markers", "label": "Только реплики с маркерами"},
                {"id": "all", "label": "Все реплики (глушить субтитры целиком)"},
            ],
        ),
        FieldSpec(key="merge_gap", label="Склеивать интервалы ближе чем, сек", type="number", default=1.5),
        FieldSpec(key="hearing_impaired", label="Искать SDH-дорожку", type="select", default="true",
                  options=[{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}]),
    ]

    # ------------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        key = self.cfg("api_key")
        if not key:
            raise ValueError("Не задан API-ключ OpenSubtitles (настройки провайдера)")
        return {
            "Api-Key": str(key),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def healthcheck(self) -> tuple[bool, str]:
        try:
            self._headers()
        except ValueError as exc:
            return False, str(exc)
        return True, "ok"

    def search(self, film: dict[str, Any], language: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"languages": language}
        if film.get("imdb_id"):
            imdb = str(film["imdb_id"])
            params["imdb_id"] = imdb[2:] if imdb.startswith("tt") else imdb
        elif film.get("tmdb_id"):
            params["tmdb_id"] = film["tmdb_id"]
        else:
            params["query"] = film.get("title")
            if film.get("year"):
                params["year"] = film["year"]
        if str(self.cfg("hearing_impaired", "true")).lower() == "true":
            params["hearing_impaired"] = "include"
        response = requests.get(
            f"{API_BASE}/subtitles", params=params, headers=self._headers(), timeout=20
        )
        response.raise_for_status()
        return list(response.json().get("data", []))

    def download(self, file_id: int) -> str:
        response = requests.post(
            f"{API_BASE}/download",
            json={"file_id": file_id},
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        link = response.json().get("link")
        if not link:
            raise ValueError("OpenSubtitles не вернул ссылку на файл")
        content = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=60).content
        return _extract_text(content, None)

    # ----------------------------------------------------------------- fetch
    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        results = self.search(film, language)
        if not results:
            return TimingsResult([], message="Дорожка субтитров не найдена")
        best = _pick_best(results, language)
        file_id = _file_id(best)
        if not file_id:
            return TimingsResult([], message="У найденной дорожки нет файла")
        text = self.download(file_id)
        cues = parse_subtitles(text, language=language)
        segments = cues_to_segments(
            cues,
            language=language,
            mode=str(self.cfg("mode", "markers")),
            merge_gap=float(self.cfg("merge_gap", 1.5) or 0),
        )
        message = f"{best.get('attributes', {}).get('files', [{}])[0].get('file_name', '?')}: {len(segments)} интервалов"
        return TimingsResult(segments, message=message)


def _file_id(item: dict[str, Any]) -> int | None:
    files = item.get("attributes", {}).get("files") or []
    if files:
        return files[0].get("file_id")
    return None


def _pick_best(results: list[dict[str, Any]], language: str) -> dict[str, Any]:
    """Предпочесть дорожку с наибольшим числом загрузок и совпадением языка."""
    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        attrs = item.get("attributes", {})
        lang_ok = 1 if (attrs.get("language") or "").lower().startswith(language[:2]) else 0
        downloads = int(attrs.get("download_count") or 0)
        points = int(attrs.get("points") or 0)
        return (lang_ok, downloads, points)

    return max(results, key=score)
