"""Тесты парсеров субтитров и построения таймингов из них."""
from __future__ import annotations

from pathlib import Path

from streamblur.timings.subtitles import (
    cues_to_segments,
    markers_for,
    merge_segments,
    parse_srt,
    parse_subtitles,
    parse_vtt,
)

SRT = """1
00:00:10,000 --> 00:00:14,000
Обычная реплика.

2
00:07:12,000 --> 00:07:19,500
[насилие] Удар, кровь на стене.

3
00:07:21,000 --> 00:07:24,000
Продолжение той же сцены, gore.

4
00:23:05,200 --> 00:23:48,900
(nudity) Полная нагота.
"""

VTT = """WEBVTT

00:01:00.000 --> 00:01:30.000
Обычный диалог

01:02:18.400 --> 01:03:02.100
[violence] Жестокая сцена
"""


def test_parse_srt():
    cues = parse_srt(SRT, language="ru")
    assert len(cues) == 4
    assert cues[0]["start"] == 10.0
    assert cues[1]["start"] == 7 * 60 + 12
    assert "насилие" in cues[1]["text"]


def test_parse_vtt():
    cues = parse_vtt(VTT, language="en")
    assert len(cues) == 2
    assert cues[1]["start"] == 3738.4


def test_parse_subtitles_autodetect():
    assert len(parse_subtitles(VTT)) == 2
    assert len(parse_subtitles(SRT)) == 4


def test_markers_only_mode():
    segments = cues_to_segments(parse_srt(SRT, "ru"), language="ru", mode="markers")
    # cue 2 и 3 склеиваются (разрыв 1.5 с), плюс нагота → 2 интервала
    assert len(segments) == 2
    assert segments[0]["start"] == 7 * 60 + 12
    assert segments[0]["end"] == 7 * 60 + 24
    assert "насилие" in segments[0]["reason"]


def test_all_mode_marks_everything():
    segments = cues_to_segments(parse_srt(SRT, "ru"), language="ru", mode="all", merge_gap=0.0)
    assert len(segments) == 4


def test_none_mode_returns_empty():
    assert cues_to_segments(parse_srt(SRT), mode="none") == []


def test_merge_segments():
    merged = merge_segments(
        [
            {"start": 0, "end": 10, "reason": "a"},
            {"start": 11, "end": 20, "reason": "b"},
            {"start": 100, "end": 110, "reason": "c"},
        ],
        gap=1.5,
    )
    assert len(merged) == 2
    assert merged[0]["end"] == 20
    assert merged[1]["start"] == 100


def test_markers_cover_several_languages():
    for lang in ("ru", "en", "de", "ja", "unknown-lang"):
        markers = markers_for(lang)
        assert markers, f"для {lang} нет маркеров"
        assert "18+" in markers


def test_sample_srt_file_from_repo():
    path = Path(__file__).resolve().parent.parent / "data" / "sample_timings.srt"
    cues = parse_srt(path.read_text(encoding="utf-8"), "ru")
    segments = cues_to_segments(cues, language="ru")
    assert len(segments) >= 4
    assert all(s["end"] > s["start"] for s in segments)
