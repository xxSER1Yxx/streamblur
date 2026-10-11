"""Тесты хранилища: фильмы, тайминги, журнал синхронизаций, сервис синка."""
from __future__ import annotations

import pytest

from streamblur.database import Database
from streamblur.film_db import FilmService
from streamblur.sync import SyncService


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


def test_upsert_creates_then_updates(db: Database):
    first = db.upsert_film(title="Дюна", year=2021, studios=["Legendary"])
    assert first["id"] > 0
    assert first["studios"] == ["Legendary"]
    second = db.upsert_film(title="Дюна", year=2021, production_companies=["Warner Bros."])
    assert second["id"] == first["id"]
    assert second["production_companies"] == ["Warner Bros."]
    assert second["studios"] == ["Legendary"]  # не затёрли то, чего не прислали
    assert db.count_films() == 1


def test_upsert_matches_by_tmdb_id(db: Database):
    a = db.upsert_film(title="Дюна", tmdb_id=438631)
    b = db.upsert_film(title="Dune", tmdb_id=438631)
    assert a["id"] == b["id"]
    assert db.get_film(a["id"])["title"] == "Dune"


def test_find_film_case_insensitive(db: Database):
    db.upsert_film(title="Ночной патруль", year=2023)
    assert db.find_film("ночной") is not None
    assert db.find_film("Ночной", 2023) is not None
    assert db.find_film("Ночной", 1999) is None
    assert db.find_film("") is None


def test_replace_segments_is_idempotent(db: Database):
    film = db.upsert_film(title="X")
    segs = [
        {"start": 10, "end": 20, "reason": "a"},
        {"start": 50, "end": 40, "reason": "мусор"},
    ]
    assert db.replace_segments(film["id"], segs, "p1") == 1
    assert db.replace_segments(film["id"], segs, "p1") == 1
    assert len(db.list_segments(film["id"])) == 1


def test_segments_from_different_providers_coexist(db: Database):
    film = db.upsert_film(title="X")
    db.replace_segments(film["id"], [{"start": 0, "end": 5}], "p1")
    db.replace_segments(film["id"], [{"start": 100, "end": 105}], "p2")
    rows = db.list_segments(film["id"])
    assert len(rows) == 2
    db.replace_segments(film["id"], [], "p1")
    assert len(db.list_segments(film["id"])) == 1


def test_add_and_delete_manual_segment(db: Database):
    film = db.upsert_film(title="X")
    seg = db.add_segment(film["id"], {"start": 5, "end": 9, "reason": "руками"})
    assert seg["provider"] == "manual"
    assert db.delete_segment(seg["id"]) is True
    assert db.delete_segment(seg["id"]) is False


def test_delete_film_removes_segments(db: Database):
    film = db.upsert_film(title="X")
    db.replace_segments(film["id"], [{"start": 0, "end": 5}], "p1")
    assert db.delete_film(film["id"]) is True
    assert db.get_film(film["id"]) is None
    assert db.list_segments(film["id"]) == []


def test_sync_log_and_kv(db: Database):
    film = db.upsert_film(title="X")
    db.log_sync(film_id=film["id"], provider="p", kind="timings", status="ok", segments=3)
    log = db.recent_sync()
    assert log[0]["segments"] == 3
    db.kv_set("k", "v")
    assert db.kv_get("k") == "v"
    db.kv_delete("k")
    assert db.kv_get("k") is None


# --------------------------------------------------------------- SyncService
class _FakeProviderResult:
    def __init__(self, segments, film=None, message=""):
        self.segments = segments
        self.film = film
        self.message = message


def test_sync_service_writes_segments_and_log(db: Database, monkeypatch):
    film = db.upsert_film(title="Тест")
    service = SyncService(db, FilmService(db))

    def fake_get_provider(provider_id: str, config=None):
        class P:
            def __init__(self, config):
                self.config = config

            def fetch(self, film, language="ru", film_id=None):
                return _FakeProviderResult(
                    [{"start": 10, "end": 20, "reason": "x"}], message="ok"
                )

        return P(config)

    import streamblur.sync as sync_module

    monkeypatch.setattr(sync_module, "get_provider", fake_get_provider)
    outcome = service.sync_film(film["id"], "fake")
    assert outcome["ok"] is True
    assert outcome["segments"] == 1
    assert db.list_segments(film["id"])[0]["reason"] == "x"
    assert db.recent_sync()[0]["status"] == "ok"


def test_sync_service_reports_provider_error(db: Database, monkeypatch):
    film = db.upsert_film(title="Тест")
    service = SyncService(db, FilmService(db))

    def fake_get_provider(provider_id: str, config=None):
        class P:
            def __init__(self, config):
                pass

            def fetch(self, film, language="ru", film_id=None):
                raise ConnectionError("нет сети")

        return P(config)

    import streamblur.sync as sync_module

    monkeypatch.setattr(sync_module, "get_provider", fake_get_provider)
    outcome = service.sync_film(film["id"], "fake")
    assert outcome["ok"] is False
    assert "нет сети" in outcome["error"]
    assert db.recent_sync()[0]["status"] == "error"


def test_sync_service_unknown_film(db: Database):
    service = SyncService(db, FilmService(db))
    with pytest.raises(KeyError):
        service.sync_film(9999, "local-file")
