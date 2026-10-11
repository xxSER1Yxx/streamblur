"""Тесты провайдера «ссылка на фильм»: автоподбор ID, метаданных и таймингов.

Сеть не используется — проверяются чистые функции разбора, поэтому тесты
работают офлайн и не ломаются из-за чужого сайта.
"""
from __future__ import annotations

import pytest

from streamblur.timings import get_provider
from streamblur.timings.link_auto import (
    URL_RULES,
    card_from_jsonld,
    card_from_meta,
    detect_id,
    extract_timings,
    parse_page_card,
    timings_from_html,
    timings_from_json,
)

KINOPS_URL = "https://kinops.web.app/movie/5506641"

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Movie","name":"Ночной патруль",
 "datePublished":"2023-05-04","duration":"PT1H44M","genre":["триллер","криминал"],
 "productionCompany":[{"@type":"Organization","name":"Example Pictures"},
                      {"@type":"Organization","name":"Night Shift Films"}],
 "countryOfOrigin":{"@type":"Country","name":"США"},
 "description":"История одного дежурства.",
 "sameAs":"https://www.imdb.com/title/tt1234567/"}
</script></head><body></body></html>
"""

META_PAGE = """
<html><head>
<meta property="og:title" content="Тихая гавань (2019)">
<meta property="og:description" content="Драма о переезде.">
<meta property="og:image" content="https://img.test/p.jpg">
<meta property="video:release_date" content="2019-02-10">
<meta property="video:genre" content="драма, мелодрама">
</head><body></body></html>
"""


# ------------------------------------------------------------------ id из URL
@pytest.mark.parametrize(
    "url,expected",
    [
        (KINOPS_URL, ("kinopoisk", "5506641")),
        ("https://kinobox.in/movie/12345", ("kinopoisk", "12345")),
        ("https://www.kinopoisk.ru/film/326/", ("kinopoisk", "326")),
        ("https://www.imdb.com/title/tt0111161/", ("imdb", "tt0111161")),
        ("https://www.themoviedb.org/movie/550", ("tmdb", "550")),
        ("https://shikimori.one/animes/1", ("shikimori", "1")),
    ],
)
def test_detect_id_known_sites(url, expected):
    assert detect_id(url) == expected


def test_detect_id_unknown_site():
    assert detect_id("https://example.com/watch/abc") is None


def test_every_rule_is_compilable():
    import re

    for rule in URL_RULES:
        assert re.compile(rule.pattern), rule.pattern


# ------------------------------------------------------------------ карточки
def test_card_from_jsonld():
    card = card_from_jsonld(JSONLD_PAGE)
    assert card["title"] == "Ночной патруль"
    assert card["year"] == 2023
    assert card["runtime_minutes"] == 104
    assert card["studios"] == ["Example Pictures", "Night Shift Films"]
    assert card["production_companies"] == ["Example Pictures", "Night Shift Films"]
    assert card["country"] == "США"
    assert card["imdb_id"] == "tt1234567"
    assert "триллер" in card["genres"]


def test_card_from_meta():
    card = card_from_meta(META_PAGE)
    assert card["title"] == "Тихая гавань"
    assert card["year"] == 2019
    assert card["genres"] == ["драма", "мелодрама"]


def test_parse_page_card_falls_back():
    assert parse_page_card(JSONLD_PAGE)["source"] == "json-ld"
    assert parse_page_card(META_PAGE)["source"] == "meta"
    assert parse_page_card("<html><title>Просто фильм (2020) — смотреть</title></html>")["year"] == 2020
    assert parse_page_card("<html><body>ничего</body></html>") == {}


# ------------------------------------------------------------------ тайминги
def test_timings_from_html_attributes():
    page = '<div class="scene" data-start="0:07:12" data-end="0:08:40">бой</div>'
    segments = timings_from_html(page)
    assert len(segments) == 1
    assert segments[0]["start"] == 432.0
    assert segments[0]["end"] == 520.0


def test_timings_from_html_text_range():
    page = "<div>Опасный момент: 00:23:05 - 00:24:12</div>"
    segments = timings_from_html(page)
    assert len(segments) == 1
    assert segments[0]["start"] == 23 * 60 + 5


def test_timings_from_json_recursive():
    payload = {
        "data": {
            "moments": [
                {"start": "0:10:00", "end": "0:11:00", "reason": "кровь"},
                {"from": 700, "to": 760},
            ]
        }
    }
    segments = timings_from_json(payload)
    assert len(segments) == 2
    assert {s["start"] for s in segments} == {600.0, 700.0}


def test_extract_timings_merges_and_sorts():
    page = """
    <script type="application/json">{"scenes":[{"start":300,"end":330}]}</script>
    <div data-start="0:01:00" data-end="0:01:10">x</div>
    <span>вставка 00:01:05 - 00:01:20</span>
    """
    segments = extract_timings(page)
    assert [s["start"] for s in segments] == sorted(s["start"] for s in segments)
    assert any(s["start"] == 300.0 for s in segments)


def test_extract_timings_empty_page():
    assert extract_timings("<html><body>нет ничего</body></html>") == []


# ---------------------------------------------------------------- провайдер
def test_provider_requires_url():
    provider = get_provider("link-auto", {})
    with pytest.raises(ValueError):
        provider.fetch({"title": "x"})


def test_provider_reports_no_timings_with_hint(monkeypatch):
    """Если на сайте таймингов нет — честно пишем об этом, а не молчим."""
    provider = get_provider("link-auto", {"url": KINOPS_URL})
    monkeypatch.setattr(provider, "fetch_page", lambda url: "<html><title>SPA</title></html>")
    result = provider.fetch({"title": "Фильм"})
    assert result.segments == []
    assert "таймингов нет" in result.message
    assert "5506641" in result.message  # ID из ссылки распознан


def test_provider_uses_page_card(monkeypatch):
    provider = get_provider("link-auto", {"url": "https://example.com/movie/1"})
    monkeypatch.setattr(provider, "fetch_page", lambda url: JSONLD_PAGE)
    result = provider.fetch({"title": "Фильм"})
    assert result.film is not None
    assert result.film["title"] == "Ночной патруль"
    assert result.film["studios"] == ["Example Pictures", "Night Shift Films"]


def test_provider_safety_grid_fallback(monkeypatch):
    """«Безопасная сетка» — страховка, когда таймингов нет вообще."""
    provider = get_provider(
        "link-auto", {"url": "https://example.com/movie/1", "safety_grid": 600,
                      "grid_duration": 30}
    )
    monkeypatch.setattr(provider, "fetch_page", lambda url: "<html><title>X</title></html>")
    result = provider.fetch({"title": "Фильм", "runtime_minutes": 30})
    assert result.segments, "сетка не построилась"
    assert result.segments[0]["end"] - result.segments[0]["start"] == 30
    assert result.segments[1]["start"] == 600


def test_provider_survives_unreachable_site(monkeypatch):
    import requests

    provider = get_provider("link-auto", {"url": KINOPS_URL})

    def boom(url):
        raise requests.ConnectionError("нет сети")

    monkeypatch.setattr(provider, "fetch_page", boom)
    result = provider.fetch({"title": "Фильм"})
    assert result.segments == []
    assert "не скачать" in result.message


def test_provider_adds_scheme_if_missing(monkeypatch):
    seen = {}

    provider = get_provider("link-auto", {"url": "kinops.web.app/movie/5506641"})

    def fake(url):
        seen["url"] = url
        return "<html><title>X</title></html>"

    monkeypatch.setattr(provider, "fetch_page", fake)
    provider.fetch({"title": "Фильм"})
    assert seen["url"] == KINOPS_URL
