"""Работа со временем: парсинг таймеров плееров и таймкодов субтитров.

Плееры рисуют время по-разному, а OCR добавляет свои ошибки (``l`` вместо ``1``,
``O`` вместо ``0``, ``.`` вместо ``:``). Поэтому здесь один вход и много допусков.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

# ``(?<![\d:])`` — не начинать таймкод с середины числа (иначе из «1:23:45»
# вытаскивается «23:45»); ``(?![\d:])`` — не обрывать его на середине.
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,3}(?::\d{1,2}){1,2}(?:[.,]\d{1,3})?)(?![\d:])")

_DIGITS = "0123456789"

# Подмены «буква → цифра», которые безопасны всегда.
_SAFE_FIXES = str.maketrans(
    {
        "O": "0", "o": "0", "D": "0",
        "Z": "2", "z": "2",
        "S": "5", "s": "5",
        "b": "6",
        "B": "8",
        "g": "9", "q": "9",
        ".": ":", ",": ":", ";": ":", "·": ":", "'": ":",
        " ": "",
    }
)

# ``l``/``I``/``|``/``i`` → ``1`` — но только рядом с другой цифрой.
# Иначе «l:12:34» (где ``l`` — это 1 из «12:34») превратится в «1:12:34»,
# то есть в 1 час 12 минут вместо 12 минут 34 секунд.
_AMBIGUOUS_FIXES = {"l": "1", "I": "1", "|": "1", "i": "1", "T": "7", "t": "7"}


def fix_ocr_text(text: str) -> str:
    """Чинит типичные ошибки OCR в строке таймера.

    Работает контекстно: неоднозначные буквы заменяются на цифры только тогда,
    когда соседний символ уже цифра.
    """
    if not text:
        return ""
    cleaned = text.translate(_SAFE_FIXES)
    out: list[str] = []
    for index, char in enumerate(cleaned):
        replacement = _AMBIGUOUS_FIXES.get(char)
        if replacement is None:
            out.append(char)
            continue
        prev = cleaned[index - 1] if index else ""
        nxt = cleaned[index + 1] if index + 1 < len(cleaned) else ""
        # символ считается частью таймкода, если рядом цифра или разделитель «:»
        if prev in _DIGITS or nxt in _DIGITS or prev == ":" or nxt == ":":
            out.append(replacement)
        # иначе отбрасываем — это буква из постороннего слова
    return "".join(out)


# Совместимость со старым именем.
_OCR_FIXES = _SAFE_FIXES


def parse_timestamp(value: str) -> float | None:
    """``'1:23:45,678'`` / ``'12:34'`` / ``'01:02:03.5'`` → секунды.

    Возвращает ``None``, если строка не похожа на время.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for match in _TIME_RE.finditer(text):
        start = match.start()
        before = text[start - 1] if start > 0 else ""
        # NB: проверка именно через множество — ``"" in "-+"`` в Python равно True
        if before in {"-", "+"}:  # «-1:00» — это не время
            continue
        return parse_hms(match.group(1))
    return None


def parse_hms(value: str) -> float | None:
    """Строгий парсер ``H:MM:SS[,ms]`` без поиска по строке."""
    if not value:
        return None
    raw = value.strip().replace(",", ".")
    if raw.startswith(("-", "+")):  # «-1:00» — не время
        return None
    parts = raw.split(":")
    if len(parts) < 2 or len(parts) > 3:
        return None
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in numbers):
        return None
    seconds = 0.0
    for part in numbers:
        seconds = seconds * 60 + part
    return seconds


def parse_ocr_time(text: str) -> tuple[float | None, float]:
    """Парсер для грязного OCR-текста.

    Возвращает ``(секунды, уверенность)``. Уверенность 0.0 — ничего не нашли,
    0.5 — нашли «похожее», 0.9+ — чистый таймкод.
    """
    if not text:
        return None, 0.0
    raw = text.strip()
    clean = fix_ocr_text(raw)
    seconds = parse_timestamp(clean)
    if seconds is None:
        return None, 0.0
    confidence = 0.9 if clean == raw else 0.72
    return seconds, max(0.1, confidence)


def format_timestamp(seconds: float, with_ms: bool = False) -> str:
    """Секунды → ``H:MM:SS`` (человекочитаемый формат для UI)."""
    if seconds is None:
        return "--:--"
    seconds = max(0.0, float(seconds))
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    base = f"{hours}:{minutes:02d}:{secs:02d}"
    if with_ms:
        return f"{base}.{int(round((seconds - total) * 100)):02d}"
    return base


def to_delta(seconds: float) -> timedelta:
    return timedelta(seconds=max(0.0, float(seconds)))
