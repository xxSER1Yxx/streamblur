"""Провайдер ``link-auto`` — «вставил ссылку, всё остальное программа сделала сама».

Расчёт на обычного пользователя: никаких селекторов, API-ключей и настроек.
Достаточно ссылки на страницу фильма.

Как это работает (по шагам, каждый шаг — fallback для предыдущего):

1. **ID из ссылки.** Известные шаблоны сайтов разбираются встроенными правилами:
   у ``kinops.web.app`` id в URL — это id Кинопоиска, у ``kinopoisk.ru`` — тоже,
   у ``imdb.com`` — tt-ид, у ``themoviedb.org`` — tmdb-ид.
2. **Метаданные со страницы.** JSON-LD (``schema.org/Movie``) → ``og:``-теги →
   ``<meta>`` → микроразметка ``itemprop`` → встроенный JSON. Так достаём
   год, студии/компании, страну, жанры, хронометраж, постер.
3. **Обогащение через TMDB.** Если ключ TMDB задан, по найденному id
   (kinopoisk / imdb / tmdb) подтягиваем полную карточку — студии и
   компании-производители там есть всегда.
4. **Тайминги.** Ищем в странице интервалы (``data-start/data-end``, диапазоны
   в тексте, встроенный JSON, ссылки на субтитры). Если на сайте их нет —
   честно говорим об этом и предлагаем «Безопасную сетку» или ручную разметку.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from ..consensus import build_consensus, describe_consensus
from ..timeutils import parse_timestamp
from .base import FieldSpec, TimingsProvider, TimingsResult, provider
from .subtitles import merge_segments
from .tmdb_provider import TMDBClient, normalize_film

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 StreamBlur/0.1"
)

# --------------------------------------------------------------- правила URL
@dataclass
class UrlRule:
    """Как из ссылки вытащить внешний id фильма."""

    pattern: str
    source: str          # kinopoisk | imdb | tmdb | shikimori | letterboxd | ...
    host_hint: str = ""

    def match(self, url: str) -> str | None:
        m = re.search(self.pattern, url or "")
        return m.group(1) if m else None


URL_RULES: list[UrlRule] = [
    # kinops.web.app / kinobox.in — id в URL это id Кинопоиска
    UrlRule(r"kinops\.[a-z.]+/(?:ru/)?movie/(\d+)", "kinopoisk"),
    UrlRule(r"kinobox\.[a-z.]+/(?:ru/)?movie/(\d+)", "kinopoisk"),
    UrlRule(r"kinopoisk\.[a-z.]+/film/(\d+)", "kinopoisk"),
    UrlRule(r"kinopoisk\.[a-z.]+/(?:ru/)?movie/(\d+)", "kinopoisk"),
    UrlRule(r"kp\.[a-z.]+/(?:ru/)?movie/(\d+)", "kinopoisk"),
    UrlRule(r"imdb\.com/title/(tt\d+)", "imdb"),
    UrlRule(r"themoviedb\.org/movie/(\d+)", "tmdb"),
    UrlRule(r"tmdb\.org/movie/(\d+)", "tmdb"),
    UrlRule(r"shikimori\.[a-z]+/animes/(\d+)", "shikimori"),
    UrlRule(r"letterboxd\.com/[a-z0-9_]+/film/([a-z0-9-]+)", "letterboxd"),
    UrlRule(r"ivi\.ru/watch/(\d+)", "ivi"),
    UrlRule(r"kinopoisk\.hd/(\d+)", "kinopoisk_hd"),
    UrlRule(r"film\.ru/(\d+)", "filmru"),
    UrlRule(r"worldart\.com/movie/movie\.php\?id=(\d+)", "worldart"),
]


def detect_id(url: str) -> tuple[str, str] | None:
    """(source, external_id) из ссылки или None."""
    for rule in URL_RULES:
        found = rule.match(url)
        if found:
            return rule.source, found
    return None


# ------------------------------------------------------- метаданные со страницы
_JSONLD_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_META_RE = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)\s*=\s*["']([^"']+)["'][^>]*"""
    r"""content\s*=\s*["']([^"']*)["']""",
    re.S | re.I,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _unescape(text: str) -> str:
    return html_lib.unescape(text or "").strip()


def parse_jsonld(page: str) -> list[dict[str, Any]]:
    """Все JSON-LD блоки, отфильтрованные по типу Movie/TV/CreativeWork."""
    found: list[dict[str, Any]] = []
    for raw in _JSONLD_RE.findall(page or ""):
        try:
            data = json.loads(_unescape(raw))
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                items.extend(g for g in graph if isinstance(g, dict))
                continue
            types = item.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any(t in {"Movie", "Film", "TVEpisode", "TVSeries", "CreativeWork"} for t in types):
                found.append(item)
    return found


def parse_meta_tags(page: str) -> dict[str, str]:
    """``og:``/``name``/``itemprop`` мета-теги в один словарь."""
    out: dict[str, str] = {}
    for key, value in _META_RE.findall(page or ""):
        out.setdefault(key.strip().lower(), _unescape(value))
    return out


def _org_names(value: Any) -> list[str]:
    """``productionCompany`` из JSON-LD бывает строкой, словарём или списком."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        name = value.get("name") or value.get("alternateName")
        return [str(name).strip()] if name else []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(_org_names(item))
        return names
    return []


def card_from_jsonld(page: str) -> dict[str, Any]:
    """Карточка фильма из JSON-LD."""
    blocks = parse_jsonld(page)
    if not blocks:
        return {}
    data = blocks[0]
    date = data.get("datePublished") or data.get("dateCreated") or ""
    year = int(date[:4]) if isinstance(date, str) and date[:4].isdigit() else None
    duration = data.get("duration") or ""
    runtime = None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(duration))
    if m and (m.group(1) or m.group(2)):
        runtime = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    genres = data.get("genre")
    genres = [str(genres)] if isinstance(genres, str) else [str(g) for g in (genres or [])]
    countries = data.get("countryOfOrigin") or data.get("contentLocation")
    card: dict[str, Any] = {
        "title": data.get("name"),
        "original_title": data.get("alternateName"),
        "year": year,
        "release_date": date or None,
        "runtime_minutes": runtime,
        "genres": genres,
        "studios": _org_names(data.get("productionCompany")),
        "production_companies": _org_names(data.get("productionCompany")),
        "country": ", ".join(_org_names(countries)),
        "overview": data.get("description"),
        "poster_url": data.get("image") if isinstance(data.get("image"), str) else None,
        "source": "json-ld",
        "raw": data,
    }
    imdb = data.get("sameAs")
    if isinstance(imdb, str) and "imdb.com/title/tt" in imdb:
        card["imdb_id"] = imdb.split("/title/")[1].split("/")[0]
    return {k: v for k, v in card.items() if v}


def card_from_meta(page: str) -> dict[str, Any]:
    """Карточка из og:/meta-тегов — fallback, когда JSON-LD нет."""
    meta = parse_meta_tags(page)
    if not meta:
        return {}
    title = meta.get("og:title") or meta.get("title") or meta.get("name")
    if title:
        title = re.sub(r"\s*[|(]\s*(\d{4})\s*[|)]?\s*$", "", title).strip()
    year = None
    year_raw = (
        meta.get("video:release_date")
        or meta.get("ya:ovs:release_date")
        or meta.get("release_date")
        or ""
    )
    if year_raw[:4].isdigit():
        year = int(year_raw[:4])
    if year is None and title:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", meta.get("og:title", ""))
        year = int(m.group(1)) if m else None
    genres = meta.get("video:genre") or meta.get("genre") or meta.get("keywords") or ""
    card: dict[str, Any] = {
        "title": title,
        "year": year,
        "overview": meta.get("og:description") or meta.get("description"),
        "poster_url": meta.get("og:image"),
        "country": meta.get("video:country") or meta.get("country"),
        "genres": [g.strip() for g in genres.split(",") if g.strip()],
        "source": "meta",
    }
    return {k: v for k, v in card.items() if v}


def card_from_title(page: str) -> dict[str, Any]:
    """Совсем слабый fallback: заголовок вкладки «Название (2024)»."""
    m = _TITLE_RE.search(page or "")
    if not m:
        return {}
    title = _unescape(m.group(1))
    year = None
    ym = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    if ym:
        year = int(ym.group(1))
    title = re.split(r"\s*[|—-]\s*(Kinopoisk|IMDb|Kinobox|смотреть)", title, maxsplit=1,
                     flags=re.I)[0].strip()
    title = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", title).strip()
    if not title:
        return {}
    return {"title": title, "year": year, "source": "title"}


def parse_page_card(page: str) -> dict[str, Any]:
    """Пройти всю цепочку fallback'ов и вернуть лучшую карточку."""
    for builder in (card_from_jsonld, card_from_meta, card_from_title):
        card = builder(page)
        if card.get("title"):
            return card
    return {}


# ------------------------------------------------------------------- тайминги
_TIME_RANGE_RE = re.compile(
    r"(?<![\d:])(\d{1,3}:\d{2}(?::\d{2})?)\s*(?:-|–|—|to|до)\s*(\d{1,3}:\d{2}(?::\d{2})?)(?![\d:])"
)
_ATTR_SEG_RE = re.compile(
    r'data-(?:start|from|begin)\s*=\s*["\']?([^"\'\s>]+)["\']?[^>]*?'
    r'data-(?:end|to|stop)\s*=\s*["\']?([^"\'\s>]+)',
    re.I,
)
_JSON_SEG_KEYS = ("start", "from", "begin", "t0", "time")
_JSON_SEG_ENDS = ("end", "to", "stop", "t1", "until")


def parse_time_value(value: Any) -> float | None:
    """Время из JSON/HTML: число = секунды, строка = таймкод.

    ``parse_timestamp`` намеренно не принимает «300» без двоеточий — иначе любая
    цифра в тексте считалась бы временем. Здесь же контекст известен, поэтому
    число трактуем как секунды.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        return parse_timestamp(text)
    try:
        return float(text)
    except ValueError:
        return None


def timings_from_html(page: str, language: str | None = None) -> list[dict[str, Any]]:
    """Вытащить интервалы из HTML: атрибуты и текстовые диапазоны."""
    out: list[dict[str, Any]] = []
    for start, end in _ATTR_SEG_RE.findall(page or ""):
        s, e = parse_timestamp(start), parse_timestamp(end)
        if s is not None and e is not None and e > s:
            out.append({"start": s, "end": e, "kind": "nsfw", "reason": "data-атрибуты страницы",
                        "language": language, "confidence": 0.7})
    text = re.sub(r"<script.*?</script>", " ", page or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for start, end in _TIME_RANGE_RE.findall(text):
        s, e = parse_timestamp(start), parse_timestamp(end)
        if s is not None and e is not None and e > s and (e - s) < 3 * 3600:
            out.append({"start": s, "end": e, "kind": "nsfw", "reason": "диапазон в тексте",
                        "language": language, "confidence": 0.6})
    return out


def timings_from_json(payload: Any, language: str | None = None) -> list[dict[str, Any]]:
    """Рекурсивно найти в JSON объекты, похожие на интервалы времени."""
    out: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(out) > 500:
            return
        if isinstance(node, dict):
            start = next((node[k] for k in _JSON_SEG_KEYS if k in node), None)
            end = next((node[k] for k in _JSON_SEG_ENDS if k in node), None)
            if start is not None and end is not None:
                s, e = parse_time_value(start), parse_time_value(end)
                if s is not None and e is not None and e > s and (e - s) < 3 * 3600:
                    out.append({
                        "start": s, "end": e, "kind": "nsfw",
                        "reason": str(node.get("reason") or node.get("type") or "json"),
                        "language": language, "confidence": 0.7,
                    })
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(payload)
    return out


_EMBEDDED_JSON_RE = re.compile(r'<script[^>]*type\s*=\s*["\']application/json["\'][^>]*>(.*?)</script>',
                              re.S | re.I)


def collect_raw_timings(page: str, language: str | None = None) -> list[dict[str, Any]]:
    """Все найденные на странице интервалы, БЕЗ склейки и с меткой источника.

    Метка ``source`` нужна консенсусу: интервал из встроенного JSON и интервал из
    ``data-``атрибутов — это два независимых «голоса» за одну и ту же сцену.
    """
    found: list[dict[str, Any]] = []
    for raw in _EMBEDDED_JSON_RE.findall(page or ""):
        try:
            for seg in timings_from_json(json.loads(_unescape(raw)), language):
                found.append({**seg, "source": "json"})
        except (json.JSONDecodeError, ValueError):
            continue
    for seg in timings_from_html(page, language):
        origin = "data-атрибут" if "data-" in (seg.get("reason") or "") else "текст"
        found.append({**seg, "source": origin})
    return found


def extract_timings(page: str, language: str | None = None) -> list[dict[str, Any]]:
    """Все способы вытащить тайминги со страницы, в порядке надёжности."""
    return merge_segments(collect_raw_timings(page, language), gap=1.0)


# ------------------------------------------------------------------- провайдер
@provider
class LinkAutoProvider(TimingsProvider):
    id = "link-auto"
    name = "Ссылка на фильм (автоподбор)"
    description = (
        "Просто вставьте ссылку на страницу фильма — программа сама найдёт год, "
        "студии и компании, а тайминги возьмёт со страницы. Подходит для kinops/kinobox, "
        "Кинопоиска, IMDb, TMDB и большинства других сайтов."
    )
    languages = ["*"]
    fields = [
        FieldSpec(
            key="url",
            label="Ссылка на фильм",
            type="url",
            required=True,
            placeholder="https://kinops.web.app/movie/5506641",
            help="Можно ссылку с любого сайта: ID и метаданные подберутся автоматически.",
        ),
        FieldSpec(
            key="tmdb_api_key",
            label="TMDB API-ключ (не обязательно)",
            type="password",
            help="Если указать, карточка фильма будет полной: студии, компании, страна.",
        ),
        FieldSpec(
            key="tmdb_language",
            label="Язык карточки",
            default="ru-RU",
        ),
        FieldSpec(
            key="safety_grid",
            label="Безопасная сетка, сек (0 = выкл.)",
            type="number",
            default=0,
            help="Если на сайте нет таймингов — блюрить каждые N секунд. Грубая, но рабочая защита.",
        ),
        FieldSpec(key="grid_duration", label="Длительность страховки, сек", type="number", default=20),
        FieldSpec(key="timeout", label="Таймаут, сек", type="number", default=20),
        FieldSpec(
            key="consensus_mode",
            label="Как выбирать сцену, если её размечали разные люди",
            type="select",
            default="consensus",
            options=[
                {"id": "consensus", "label": "По большинству голосов (рекомендуется)"},
                {"id": "union", "label": "Осторожно: блюрить по любому упоминанию"},
                {"id": "strict", "label": "Строго: только если отметили все"},
            ],
        ),
        FieldSpec(
            key="min_votes",
            label="Минимум голосов за сцену",
            type="number",
            default=1,
            help="1 = брать всё найденное. 2+ = только сцены, которые отметили несколько источников.",
        ),
    ]

    # ------------------------------------------------------------------ HTTP
    def fetch_page(self, url: str) -> str:
        timeout = float(self.config.get("timeout", 20) or 20)
        response = requests.get(
            url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}, timeout=timeout
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    # ----------------------------------------------------------------- fetch
    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        url = str(self.config.get("url") or "").strip()
        if not url:
            raise ValueError("Вставьте ссылку на фильм")
        if not urlparse(url).scheme.startswith("http"):
            url = "https://" + url

        notes: list[str] = []
        detected = detect_id(url)
        if detected:
            notes.append(f"ID из ссылки: {detected[0]}={detected[1]}")
        else:
            notes.append("ID в ссылке не распознан — читаю страницу целиком")

        # 1) страница
        page = ""
        try:
            page = self.fetch_page(url)
        except requests.RequestException as exc:
            notes.append(f"страницу не скачать: {exc}")
        card = parse_page_card(page) if page else {}

        # 2) обогащение через TMDB по найденному id
        card = self._enrich(card, detected, notes)

        # 3) тайминги: собираем все источники и выбираем по большинству голосов
        raw_segments = collect_raw_timings(page, language) if page else []
        if raw_segments:
            segments = build_consensus(
                raw_segments,
                mode=str(self.config.get("consensus_mode", "consensus") or "consensus"),
                min_votes=int(self.config.get("min_votes", 1) or 1),
                language=language,
            )
            summary = describe_consensus(
                raw_segments,
                mode=str(self.config.get("consensus_mode", "consensus") or "consensus"),
                min_votes=int(self.config.get("min_votes", 1) or 1),
            )
            notes.append(
                f"найдено {summary['raw_segments']} отметок от {summary['sources']} источник(ов), "
                f"сцен после консенсуса: {summary['kept']}"
                + (f", отброшено {summary['dropped']}" if summary["dropped"] else "")
            )
        else:
            segments = []
            grid = float(self.config.get("safety_grid", 0) or 0)
            if grid > 0:
                runtime = int(card.get("runtime_minutes") or film.get("runtime_minutes") or 120)
                segments = _safety_grid(runtime * 60, grid,
                                        float(self.config.get("grid_duration", 20)))
                notes.append(f"на сайте таймингов нет → безопасная сетка каждые {grid:.0f} с")
            else:
                notes.append(
                    "на сайте таймингов нет. Добавьте интервалы вручную во вкладке «Тайминги» "
                    "или включите «Безопасную сетку» в настройках провайдера."
                )

        return TimingsResult(
            segments,
            film=(card or None) if card.get("title") else None,
            message="; ".join(notes),
        )

    # ------------------------------------------------------------- обогащение
    def _enrich(self, card: dict[str, Any], detected: tuple[str, str] | None,
                notes: list[str]) -> dict[str, Any]:
        api_key = str(self.config.get("tmdb_api_key") or "").strip()
        if not api_key:
            return card
        try:
            client = TMDBClient(api_key, str(self.config.get("tmdb_language", "ru-RU")))
            details = None
            if detected:
                source, external_id = detected
                if source == "tmdb" and external_id.isdigit():
                    details = client.details(int(external_id))
                elif source == "imdb":
                    details = client.find_by_imdb(external_id)
                else:
                    details = client.find_by_external(source, external_id)
            if details is None and card.get("title"):
                year = card.get("year")
                results = client.search(str(card["title"]), year)
                if results:
                    details = client.details(int(results[0]["id"]))
            if details is None:
                notes.append("TMDB: фильм не найден")
                return card
            remote = normalize_film(details, str(self.config.get("tmdb_language", "ru-RU")))
            merged = dict(card)
            for key, value in remote.items():
                if not value:
                    continue
                if key in {"studios", "production_companies", "genres"}:
                    merged[key] = value or merged.get(key)
                else:
                    merged[key] = merged.get(key) or value
            merged["source"] = "tmdb"
            notes.append("карточка дополнена из TMDB")
            return merged
        except (requests.RequestException, ValueError, PermissionError) as exc:
            notes.append(f"TMDB недоступен: {exc}")
            return card


def _safety_grid(total_seconds: float, step: float, duration: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = 0.0
    while t < max(0.0, total_seconds):
        out.append({
            "start": t,
            "end": min(total_seconds, t + duration),
            "kind": "nsfw",
            "reason": "безопасная сетка",
            "confidence": 0.3,
        })
        t += max(1.0, step)
    return out
