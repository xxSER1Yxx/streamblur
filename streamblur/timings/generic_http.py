"""Провайдер ``generic-http`` — «подключи свой сайт».

Работает с любым источником, отдающим JSON или HTML. Всё описывается в настройках
провайдера, без единой строчки кода:

* ``url`` — шаблон с плейсхолдерами ``{title}``, ``{year}``, ``{imdb_id}``,
  ``{tmdb_id}``, ``{language}``;
* ``format`` — ``json`` или ``html``;
* для JSON: ``json_segments_path`` (путь к списку, напр. ``data.moments``) и имена полей
  ``field_start`` / ``field_end`` / ``field_reason``;
* для HTML: ``selector`` (CSS) — каждый найденный элемент даёт атрибуты
  ``data-start`` / ``data-end``, либо текст вида ``00:12:04 - 00:14:31``.

Это «универсальный адаптер»: добавлять новый сайт через UI можно без обновления программы.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import quote

import requests

from ..timeutils import parse_timestamp
from .base import FieldSpec, TimingsProvider, TimingsResult, registry
from .subtitles import merge_segments

_RANGE_RE = re.compile(
    r"(\d{1,3}:\d{1,2}(?::\d{1,2})?)\s*(?:-|–|—|to|до)\s*(\d{1,3}:\d{1,2}(?::\d{1,2})?)"
)


def _dig(data: Any, path: str) -> Any:
    """``data.moments.0.start`` — простой JSONPath для настроек из UI."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _select(html_text: str, selector: str) -> list[str]:
    """Мини-CSS-селектор (tag / .class / #id / [attr]) без тяжёлых зависимостей."""
    if not selector:
        return []
    tag = "[a-zA-Z0-9]+"
    pattern = rf"<{tag}[^>]*>"
    matches = re.findall(pattern, html_text, flags=re.S)
    selector = selector.strip()
    if selector.startswith("#"):
        wanted_id = selector[1:]
        return [m for m in matches if re.search(rf'id\s*=\s*["\']?{re.escape(wanted_id)}', m)]
    if selector.startswith("."):
        wanted_cls = selector[1:]
        return [
            m
            for m in matches
            if re.search(rf'class\s*=\s*["\'][^"\']*\b{re.escape(wanted_cls)}\b', m)
        ]
    if selector.startswith("[") and selector.endswith("]"):
        attr = selector[1:-1]
        return [m for m in matches if re.search(rf"{re.escape(attr)}\s*=", m)]
    if re.fullmatch(tag, selector):
        return [m for m in matches if m.lower().startswith(f"<{selector.lower()}")]
    return []


def segments_from_json(
    payload: Any,
    *,
    list_path: str = "",
    field_start: str = "start",
    field_end: str = "end",
    field_reason: str = "reason",
    language: str | None = None,
) -> list[dict[str, Any]]:
    node = _dig(payload, list_path)
    if node is None:
        node = payload
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list):
        return []
    out: list[dict[str, Any]] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        start = parse_timestamp(str(_dig(item, field_start))) if _dig(item, field_start) is not None else None
        end = parse_timestamp(str(_dig(item, field_end))) if _dig(item, field_end) is not None else None
        if start is None or end is None:
            continue
        reason = _dig(item, field_reason)
        out.append(
            {
                "start": float(start),
                "end": float(end),
                "kind": "nsfw",
                "reason": str(reason)[:200] if reason is not None else None,
                "language": language,
                "confidence": 0.75,
            }
        )
    return out


def _elements(html_text: str, selector: str) -> list[str]:
    """Открытый тег + его текст до закрывающего тега.

    Нужно, чтобы таймкоды можно было читать и из атрибутов (``data-start``),
    и из текста элемента (``00:12:04 - 00:14:31``).
    """
    out: list[str] = []
    for opening in _select(html_text, selector):
        m = re.match(r"<\s*([a-zA-Z0-9]+)", opening)
        if not m:
            out.append(opening)
            continue
        tag = m.group(1)
        start = html_text.find(opening)
        end = html_text.lower().find(f"</{tag.lower()}", start)
        out.append(html_text[start : (end + len(tag) + 3) if end > 0 else start + len(opening)])
    return out


def segments_from_html(text: str, selector: str, language: str | None = None) -> list[dict[str, Any]]:
    raw_nodes = _elements(text or "", selector) if selector else []
    if not raw_nodes:
        raw_nodes = re.findall(r"<[^>]*>[^<]*", text or "", flags=re.S)
    out: list[dict[str, Any]] = []
    for node in raw_nodes:
        start_attr = re.search(r'data-start\s*=\s*["\']?([^"\'\s>]+)', node)
        end_attr = re.search(r'data-end\s*=\s*["\']?([^"\'\s>]+)', node)
        if start_attr and end_attr:
            start = parse_timestamp(start_attr.group(1))
            end = parse_timestamp(end_attr.group(1))
        else:
            rng = _RANGE_RE.search(node)
            if not rng:
                continue
            start = parse_timestamp(rng.group(1))
            end = parse_timestamp(rng.group(2))
        if start is None or end is None:
            continue
        out.append(
            {
                "start": float(start),
                "end": float(end),
                "kind": "nsfw",
                "reason": None,
                "language": language,
                "confidence": 0.7,
            }
        )
    return out


@registry.register
class GenericHttpProvider(TimingsProvider):
    id = "generic-http"
    name = "Свой сайт (generic HTTP/JSON/HTML)"
    description = (
        "Универсальный адаптер: укажите URL и где в ответе лежат тайминги. "
        "Подходит для большинства сайтов с таймкодами."
    )
    languages = ["*"]
    fields = [
        FieldSpec(
            key="url",
            label="URL шаблона",
            required=True,
            placeholder="https://example.com/api/timings?title={title}&lang={language}",
            help="Плейсхолдеры: {title} {year} {imdb_id} {tmdb_id} {language}",
        ),
        FieldSpec(
            key="format",
            label="Формат ответа",
            type="select",
            default="json",
            options=[{"id": "json", "label": "JSON"}, {"id": "html", "label": "HTML"}],
        ),
        FieldSpec(key="json_segments_path", label="Путь к списку (JSON)", placeholder="data.moments"),
        FieldSpec(key="field_start", label="Поле начала", default="start"),
        FieldSpec(key="field_end", label="Поле конца", default="end"),
        FieldSpec(key="field_reason", label="Поле причины", default="reason"),
        FieldSpec(key="selector", label="CSS-селектор (HTML)", placeholder=".scene-time"),
        FieldSpec(key="headers", label="Доп. заголовки (JSON)", type="textarea", placeholder='{"User-Agent": "..."}'),
        FieldSpec(key="timeout", label="Таймаут, сек", type="number", default=15),
    ]

    def build_url(self, film: dict[str, Any], language: str) -> str:
        template = str(self.cfg("url", ""))
        if not template:
            raise ValueError("Не указан URL шаблона")
        mapping = {
            "title": quote(str(film.get("title") or "")),
            "title_raw": str(film.get("title") or ""),
            "original_title": quote(str(film.get("original_title") or film.get("title") or "")),
            "year": str(film.get("year") or ""),
            "imdb_id": str(film.get("imdb_id") or ""),
            "tmdb_id": str(film.get("tmdb_id") or ""),
            "language": language or "ru",
        }
        try:
            return template.format(**mapping)
        except KeyError as exc:
            raise ValueError(f"Неизвестный плейсхолдер в URL: {exc}") from exc

    def request(self, url: str) -> requests.Response:
        headers = {"User-Agent": "StreamBlur/0.1 (+https://github.com/streamblur)"}
        raw_headers = self.cfg("headers")
        if raw_headers:
            try:
                headers.update(json.loads(raw_headers) if isinstance(raw_headers, str) else raw_headers)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Некорректный JSON в «Доп. заголовки»: {exc}") from exc
        timeout = float(self.cfg("timeout", 15) or 15)
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response

    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        url = self.build_url(film, language)
        response = self.request(url)
        fmt = str(self.cfg("format", "json")).lower()
        if fmt == "html":
            segments = segments_from_html(response.text, str(self.cfg("selector", "")), language)
        else:
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = json.loads(response.text or "{}")
            segments = segments_from_json(
                payload,
                list_path=str(self.cfg("json_segments_path", "") or ""),
                field_start=str(self.cfg("field_start", "start") or "start"),
                field_end=str(self.cfg("field_end", "end") or "end"),
                field_reason=str(self.cfg("field_reason", "reason") or "reason"),
                language=language,
            )
        segments = merge_segments(segments, gap=1.0)
        return TimingsResult(
            segments,
            message=f"{url} → {len(segments)} интервалов",
        )
