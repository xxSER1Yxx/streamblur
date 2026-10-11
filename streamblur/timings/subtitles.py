"""Парсеры субтитров SRT / WebVTT → тайминги.

Субтитры — самый доступный источник таймингов: во многих дорожках опасные сцены
помечены словами в самом тексте (``[насилие]``, ``(nudity)``, ``*gunshot*`` и т.п.).
Провайдер ``opensubtitles`` и ``local-file`` используют этот модуль.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..timeutils import parse_hms

_SRT_CUE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})",
)
_VTT_CUE = re.compile(
    r"(?:(\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(?:(\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3})"
)
_TIME_ANY = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})")
_TAG_RE = re.compile(r"<[^>]+>")
_BRACES_RE = re.compile(r"\{[^}]*\}")

# Ключевые слова-маркеры опасного контента по языкам дорожки.
DEFAULT_MARKERS: dict[str, list[str]] = {
    "ru": [
        "насилие", "кровь", "убий", "труп", "голый", "нагота", "секс", "мат",
        "вырезано", "цензура", "18+", "жесток", "расчлен",
    ],
    "en": [
        "violence", "gore", "blood", "kill", "nude", "nudity", "sex", "nsfw",
        "explicit", "graphic", "disturbing", "censored", "18+",
    ],
    "uk": ["насиль", "кров", "вбив", "труп", "голий", "нагота", "секс", "18+"],
    "de": ["gewalt", "blut", "mord", "nackt", "sex", "zensiert", "18+"],
    "fr": ["violence", "sang", "meurtre", "nu", "sexe", "censuré", "18+"],
    "es": ["violencia", "sangre", "muerte", "desnudo", "sexo", "censura", "18+"],
    "pt": ["violência", "sangue", "morte", "nu", "sexo", "censura", "18+"],
    "it": ["violenza", "sangue", "omicidio", "nudo", "sesso", "censura", "18+"],
    "pl": ["przemoc", "krew", "morderstwo", "nagość", "seks", "18+"],
    "tr": ["şiddet", "kan", "cinayet", "çıplak", "seks", "sansür", "18+"],
    "ja": ["暴力", "血", "殺", "裸", "性", "規制"],
    "ko": ["폭력", "피", "살인", "裸", "성", "검열"],
    "zh": ["暴力", "血", "杀", "裸", "性", "审查"],
    "ar": ["عنف", "دم", "قتل", "عري", "جنس"],
    "hi": ["हिंसा", "खून", "हत्या", "नग्न", "सेक्स"],
}

_ANY_LANGUAGE_MARKERS = ["18+", "nsfw", "nswf", "xxx", "gore"]


def markers_for(language: str | None) -> list[str]:
    """Маркеры для языка дорожки + универсальные."""
    lang = (language or "").lower()[:2]
    base = list(DEFAULT_MARKERS.get(lang, []))
    if not base:
        # Неизвестный язык — берём английские (чаще всего встречаются в субтитрах)
        base = list(DEFAULT_MARKERS["en"])
    seen: list[str] = []
    for item in base + _ANY_LANGUAGE_MARKERS:
        low = item.lower()
        if low not in seen:
            seen.append(low)
    return seen


def clean_text(raw: str) -> str:
    """Убрать теги и музыкальные/звуковые пометки из строки субтитра."""
    text = _TAG_RE.sub(" ", raw or "")
    text = _BRACES_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_seconds(value: str) -> float | None:
    return parse_hms(value.strip().replace(",", "."))


def parse_srt(content: str, language: str | None = None) -> list[dict[str, Any]]:
    """SRT → список cues ``{start, end, text}``."""
    cues: list[dict[str, Any]] = []
    lines = (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        match = _SRT_CUE.search(lines[i])
        if not match:
            i += 1
            continue
        start = _to_seconds(match.group(1))
        end = _to_seconds(match.group(2))
        text_lines: list[str] = []
        j = i + 1
        while j < len(lines) and lines[j].strip():
            text_lines.append(lines[j])
            j += 1
        if start is not None and end is not None:
            cues.append(
                {
                    "start": start,
                    "end": end,
                    "text": clean_text(" ".join(text_lines)),
                    "language": language,
                }
            )
        i = j + 1
    return cues


def parse_vtt(content: str, language: str | None = None) -> list[dict[str, Any]]:
    """WebVTT → список cues."""
    cues: list[dict[str, Any]] = []
    lines = (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        if "-->" not in lines[i]:
            i += 1
            continue
        times = _TIME_ANY.findall(lines[i])
        if len(times) < 2:
            i += 1
            continue
        start = _to_seconds(times[0])
        end = _to_seconds(times[1])
        text_lines: list[str] = []
        j = i + 1
        while j < len(lines) and lines[j].strip():
            text_lines.append(lines[j])
            j += 1
        if start is not None and end is not None:
            cues.append(
                {
                    "start": start,
                    "end": end,
                    "text": clean_text(" ".join(text_lines)),
                    "language": language,
                }
            )
        i = j + 1
    return cues


def parse_subtitles(content: str, language: str | None = None) -> list[dict[str, Any]]:
    """Автоопределение формата по содержимому."""
    if "WEBVTT" in (content or "")[:200]:
        return parse_vtt(content, language)
    if _SRT_CUE.search(content or ""):
        return parse_srt(content, language)
    return parse_vtt(content, language)


def cues_to_segments(
    cues: Iterable[dict[str, Any]],
    *,
    language: str | None = None,
    markers: list[str] | None = None,
    mode: str = "markers",
    kind: str = "nsfw",
    merge_gap: float = 1.5,
) -> list[dict[str, Any]]:
    """Cues → опасные сегменты.

    mode:
      * ``markers`` — только реплики, содержащие маркерные слова;
      * ``all``     — все реплики как опасные (для «глухой» дорожки);
      * ``none``    — ничего не возвращать (полезно для отладки).
    """
    if mode == "none":
        return []
    wanted = [m.lower() for m in (markers or markers_for(language))]
    segments: list[dict[str, Any]] = []
    for cue in cues:
        text = (cue.get("text") or "").lower()
        if mode == "all":
            reason = "all cues"
        else:
            hit = next((m for m in wanted if m and m in text), None)
            if hit is None:
                continue
            reason = f"marker: {hit}"
        segments.append(
            {
                "start": float(cue["start"]),
                "end": float(cue["end"]),
                "kind": kind,
                "reason": reason,
                "language": cue.get("language") or language,
                "confidence": 0.8 if mode == "markers" else 1.0,
            }
        )
    return merge_segments(segments, gap=merge_gap)


def merge_segments(segments: list[dict[str, Any]], gap: float = 1.5) -> list[dict[str, Any]]:
    """Склеить соседние интервалы — иначе блюр будет «мигать» каждые полсекунды.

    Вход сортируется сам: на неотсортированном списке «слияние соседей»
    молча выдавало сегменты в произвольном порядке.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s["start"], s["end"]))
    merged: list[dict[str, Any]] = [dict(ordered[0])]
    for seg in ordered[1:]:
        last = merged[-1]
        if seg["start"] - last["end"] <= gap:
            last["end"] = max(last["end"], seg["end"])
            if seg.get("reason") and seg["reason"] not in (last.get("reason") or ""):
                last["reason"] = f'{last.get("reason") or ""} + {seg["reason"]}'.strip(" +")
        else:
            merged.append(dict(seg))
    return merged
