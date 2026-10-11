"""Провайдер ``local-file``: тайминги из локального файла (JSON / SRT / VTT).

Формат JSON (``{"segments": [...]}`` или просто список)::

    {"segments": [{"start": "0:12:04", "end": "0:14:31", "reason": "насилие"}]}

Время можно задавать строкой ``H:MM:SS`` или числом секунд.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..timeutils import parse_timestamp
from .base import FieldSpec, TimingsProvider, TimingsResult, registry
from .subtitles import merge_segments, parse_subtitles


def _to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_timestamp(str(value))


def load_segments_from_text(
    text: str, *, language: str | None = None, merge_gap: float = 1.5
) -> list[dict[str, Any]]:
    """Разобрать содержимое файла любого поддерживаемого формата."""
    stripped = (text or "").lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
        raw_segments = data.get("segments", data) if isinstance(data, dict) else data
        if not isinstance(raw_segments, list):
            raise ValueError('JSON должен содержать список сегментов или ключ "segments"')
        segments: list[dict[str, Any]] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            start = _to_seconds(item.get("start", item.get("from", item.get("t0"))))
            end = _to_seconds(item.get("end", item.get("to", item.get("t1"))))
            if start is None or end is None:
                continue
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "kind": item.get("kind", "nsfw"),
                    "reason": item.get("reason") or item.get("note"),
                    "language": item.get("language", language),
                    "confidence": float(item.get("confidence", 1.0)),
                }
            )
        return merge_segments(segments, gap=merge_gap)

    cues = parse_subtitles(text, language=language)
    return [
        {
            "start": float(c["start"]),
            "end": float(c["end"]),
            "kind": "nsfw",
            "reason": (c.get("text") or "")[:160] or None,
            "language": language,
            "confidence": 0.7,
        }
        for c in cues
    ]


@registry.register
class LocalFileProvider(TimingsProvider):
    id = "local-file"
    name = "Локальный файл (JSON / SRT / VTT)"
    description = (
        "Тайминги из файла на диске. Самый надёжный вариант: вы сами решаете, "
        "какие сцены прятать."
    )
    languages = ["*"]
    fields = [
        FieldSpec(
            key="path",
            label="Путь к файлу",
            required=True,
            placeholder="C:\\timings\\movie.json или ./timings/movie.srt",
            help="Поддерживаются .json, .srt, .vtt",
        ),
        FieldSpec(
            key="mode",
            label="Режим",
            type="select",
            default="markers",
            options=[
                {"id": "markers", "label": "Только реплики с маркерами"},
                {"id": "all", "label": "Все реплики субтитров"},
                {"id": "raw", "label": "Все сегменты как есть (для JSON)"},
            ],
        ),
        FieldSpec(
            key="merge_gap",
            label="Склеивать интервалы ближе чем, сек",
            type="number",
            default=1.5,
        ),
    ]

    def fetch(self, film: dict[str, Any], language: str = "ru", film_id: int | None = None):
        path = self.config.get("path")
        if not path:
            raise ValueError("Не указан путь к файлу таймингов")
        p = Path(str(path)).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {p}")
        text = p.read_text(encoding="utf-8", errors="replace")
        gap = float(self.cfg("merge_gap", 1.5) or 0)
        segments = load_segments_from_text(text, language=language, merge_gap=gap)
        mode = self.cfg("mode", "raw")
        if mode == "none":
            segments = []
        if not segments:
            return TimingsResult(
                [], message=f"В файле {p.name} не найдено опасных интервалов"
            )
        return TimingsResult(
            segments, message=f"Загружено {len(segments)} интервалов из {p.name}"
        )
