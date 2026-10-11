"""Тесты парсеров времени: обычные таймкоды, субтитры и грязный OCR-текст."""
from __future__ import annotations

import pytest

from streamblur.timeutils import (
    fix_ocr_text,
    format_timestamp,
    parse_hms,
    parse_ocr_time,
    parse_timestamp,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0:12:34", 12 * 60 + 34),
        ("1:02:03", 3723.0),
        ("12:34", 754.0),
        ("01:02:03,500", 3723.5),
        ("00:00:07.250", 7.25),
        ("время 1:23:45 осталось", 5025.0),  # 1:23:45 = 5025 с
        ("2:00:00", 7200.0),
    ],
)
def test_parse_timestamp(value, expected):
    assert parse_timestamp(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "нет времени", "abc", "--:--", "12", "-1:00"])
def test_parse_timestamp_rejects_garbage(value):
    assert parse_timestamp(value) is None


def test_parse_hms_strict():
    assert parse_hms("10:20") == pytest.approx(620.0)
    assert parse_hms("1:2:3") == pytest.approx(3723.0)
    assert parse_hms("1:2:3:4") is None
    assert parse_hms("aa:bb") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0:12:34", 754.0),
        ("O:12:34", 754.0),       # O → 0
        ("l:12:34", 4354.0),      # l рядом с «:» → 1, получается 1:12:34
        ("0.12.34", 754.0),       # точки вместо двоеточий
        ("0:12:34 / 1:44:02", 754.0),
        ("I:02:O3", 3723.0),
        ("0:l2:34", 754.0),       # l внутри числа → 1
        ("Пауза 12:34", 754.0),   # буквы из посторонних слов не ломают парсер
    ],
)
def test_parse_ocr_time_fixes_common_errors(text, expected):
    position, confidence = parse_ocr_time(text)
    assert position == pytest.approx(expected)
    assert confidence > 0.3


def test_fix_ocr_text_does_not_invent_hours():
    """``l`` в начале «l:12:34» не должен молча превращать минуты в часы."""
    assert fix_ocr_text("l:12:34") == "1:12:34"
    assert parse_timestamp(fix_ocr_text("l:12:34")) == 4354.0
    assert fix_ocr_text("0:l2:34") == "0:12:34"
    # буквы из посторонних слов не должны ломать распознавание таймера
    assert parse_ocr_time("Время 12:34")[0] == 754.0


def test_parse_ocr_time_no_digits():
    assert parse_ocr_time("пауза") == (None, 0.0)
    assert parse_ocr_time("") == (None, 0.0)


def test_parse_ocr_time_confidence_lower_for_fixed_text():
    _, clean = parse_ocr_time("0:12:34")
    _, dirty = parse_ocr_time("O:l2:34")
    assert dirty < clean


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0:00:00"), (754, "0:12:34"), (3723, "1:02:03"), (7200, "2:00:00")],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_format_timestamp_none():
    assert format_timestamp(None) == "--:--"
