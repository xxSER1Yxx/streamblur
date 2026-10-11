"""Тесты HTTP API и демо-режима (мок-OBS + виртуальный плеер + настоящий движок)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from streamblur.config import SettingsStore
from streamblur.web.app import AppState, create_app

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture()
def client(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(
        {
            "data_dir": str(tmp_path),
            "obs": {"backend": "mock"},
            "timings": {"auto_refresh_on_start": False},
            "clock": {"source": "manual"},
        }
    )
    state = AppState(store)
    with TestClient(create_app(state)) as test_client:
        test_client.sb = state
        yield test_client


# ------------------------------------------------------------------ базовое
def test_health(client):
    data = client.get("/api/health").json()
    assert data["ok"] is True
    assert isinstance(data["films"], int)


def test_settings_roundtrip(client):
    before = client.get("/api/settings").json()
    assert before["obs"]["backend"] == "mock"
    updated = client.post(
        "/api/settings", json={"patch": {"clock": {"pre_roll": 1.5}}}
    ).json()
    assert updated["clock"]["pre_roll"] == 1.5
    # остальные настройки не потерялись
    assert updated["obs"]["backend"] == "mock"


def test_state_has_expected_shape(client):
    data = client.get("/api/state").json()
    for key in ("position", "blur_on", "clock", "blur", "scheduler", "events"):
        assert key in data


def test_providers_listed_with_fields(client):
    data = client.get("/api/providers").json()
    ids = {p["id"] for p in data["providers"]}
    assert "local-file" in ids
    fields = {f["key"] for p in data["providers"] if p["id"] == "local-file" for f in p["fields"]}
    assert "path" in fields


def test_unknown_provider_404(client):
    response = client.post("/api/providers/нет-такого/test", json={"patch": {}})
    assert response.status_code == 404


# ------------------------------------------------------------------ фильмы
def test_film_crud_and_segments(client):
    film = client.post(
        "/api/films",
        json={
            "title": "Тестовый фильм",
            "year": 2024,
            "studios": ["Test Studio"],
            "production_companies": ["Test Films"],
            "country": "Россия",
        },
    ).json()
    assert film["id"] > 0
    assert film["studios"] == ["Test Studio"]

    listed = client.get("/api/films").json()
    assert listed["total"] == 1

    seg = client.post(
        f"/api/films/{film['id']}/segments",
        json={"start": "0:10:00", "end": "0:11:30", "reason": "драка"},
    ).json()
    assert seg["start"] == 600.0

    detail = client.get(f"/api/films/{film['id']}").json()
    assert len(detail["segments"]) == 1
    assert detail["segments"][0]["reason"] == "драка"

    assert client.delete(f"/api/segments/{seg['id']}").status_code == 200
    assert client.delete(f"/api/films/{film['id']}").status_code == 200
    assert client.get("/api/films").json()["total"] == 0


def test_invalid_segment_rejected(client):
    film = client.post("/api/films", json={"title": "X"}).json()
    response = client.post(
        f"/api/films/{film['id']}/segments", json={"start": "0:10:00", "end": "0:09:00"}
    )
    assert response.status_code == 400


def test_engine_load_film(client):
    film = client.post("/api/films", json={"title": "Под защитой"}).json()
    client.post(
        f"/api/films/{film['id']}/segments",
        json={"start": 0, "end": 600, "reason": "весь фильм"},
    )
    snapshot = client.post("/api/engine/load", json={"film_id": film["id"]}).json()
    assert snapshot["segments_total"] == 1
    assert snapshot["film_title"] == "Под защитой"

    client.post("/api/clock/sync", json={"position": 300})
    # движок опрашивает тайминги в своём потоке — даём ему тик
    deadline = time.time() + 5
    while time.time() < deadline and not client.get("/api/state").json()["blur_on"]:
        time.sleep(0.05)
    state = client.get("/api/state").json()
    assert state["blur_on"] is True, state["reason"]

    client.post("/api/engine/unload")
    deadline = time.time() + 5
    while time.time() < deadline and client.get("/api/state").json()["blur_on"]:
        time.sleep(0.05)
    assert client.get("/api/state").json()["blur_on"] is False


def test_engine_load_unknown_film(client):
    assert client.post("/api/engine/load", json={"film_id": 999}).status_code == 404


# ---------------------------------------------------------- тайминги из файла
def test_sync_from_local_file_provider(client, tmp_path: Path):
    target = tmp_path / "timings.json"
    target.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": "0:07:12", "end": "0:08:40", "reason": "драка"},
                    {"start": "0:23:05", "end": "0:24:12", "reason": "ню"},
                ]
            }
        ),
        encoding="utf-8",
    )
    client.post(
        "/api/settings",
        json={"patch": {"timings": {"provider": "local-file",
                                    "config": {"path": str(target)}}}},
    )
    film = client.post("/api/films", json={"title": "С таймингами"}).json()
    outcome = client.post(f"/api/films/{film['id']}/sync", json={}).json()
    assert outcome["ok"] is True, outcome
    assert outcome["segments"] == 2

    log = client.get("/api/sync/log").json()["log"]
    assert log[0]["status"] == "ok"

    detail = client.get(f"/api/films/{film['id']}").json()
    assert detail["segments"][0]["start"] == 432.0


def test_sync_error_reported(client):
    client.post(
        "/api/settings",
        json={"patch": {"timings": {"provider": "local-file",
                                    "config": {"path": "/нет/файла.json"}}}},
    )
    film = client.post("/api/films", json={"title": "Ошибка"}).json()
    outcome = client.post(f"/api/films/{film['id']}/sync", json={}).json()
    assert outcome["ok"] is False
    assert "не найден" in outcome["error"].lower()


def test_sample_repo_file_syncs(client):
    client.post(
        "/api/settings",
        json={"patch": {"timings": {"provider": "local-file",
                                    "config": {"path": str(DATA_DIR / "sample_timings.json")}}}},
    )
    film = client.post("/api/films", json={"title": "Пример"}).json()
    outcome = client.post(f"/api/films/{film['id']}/sync", json={}).json()
    assert outcome["ok"] is True
    assert outcome["segments"] == 5


# -------------------------------------------------------------- блюр и PANIC
def test_blur_test_endpoint(client):
    response = client.post("/api/blur/test", json={"seconds": 0.3})
    assert response.json()["ok"] is True
    assert client.get("/api/state").json()["blur"]["state"] == "on"
    time.sleep(0.7)
    assert client.get("/api/state").json()["blur"]["state"] == "off"


def test_panic_endpoint(client):
    state = client.post("/api/panic", json={"enabled": True}).json()
    assert state["panic"] is True
    assert state["blur_on"] is True
    state = client.post("/api/panic", json={"enabled": False}).json()
    assert state["panic"] is False


def test_manual_blur_toggle(client):
    assert client.post("/api/blur", json={"enabled": True}).json()["state"] == "on"
    assert client.post("/api/blur", json={"enabled": False}).json()["state"] == "off"


# --------------------------------------------------------------------- часы
def test_clock_manual_sync(client):
    status = client.post("/api/clock/sync", json={"position": "1:00:00"}).json()
    assert status["position"] == pytest.approx(3600.0, abs=1.0)
    assert status["manual"] is True


def test_clock_sync_bad_value(client):
    assert client.post("/api/clock/sync", json={"position": "мусор"}).status_code == 400


def test_clock_start_manual_source(client):
    status = client.post("/api/clock/start", json={"source": "manual"}).json()
    assert status["running"] is True
    assert status["source"] == "manual"
    assert client.post("/api/clock/stop").json()["running"] is False


# --------------------------------------------------------------------- демо
def test_demo_full_pipeline(client):
    state = client.sb
    """Самый важный тест: демо → мок-OBS получает команды включения фильтра."""
    response = client.post("/api/demo/start", json={"speed": 60.0}).json()
    assert response["running"] is True, response
    try:
        # 1-я опасная сцена демо начинается на 60-й секунде фильма; x60 → через ~1 с
        deadline = time.time() + 12
        saw_blur = False
        while time.time() < deadline:
            if state.engine.snapshot().blur_on:
                saw_blur = True
                break
            time.sleep(0.1)
        assert saw_blur, "движок не включил блюр на опасной сцене"
        # мок-OBS действительно получил команду
        assert any(entry["enabled"] for entry in state.demo.mock.blur_log), state.demo.mock.blur_log
    finally:
        client.post("/api/demo/stop").json()
    assert client.get("/api/demo/status").json()["running"] is False


def test_demo_stop_without_start(client):
    assert client.post("/api/demo/stop").json()["running"] is False


# ------------------------------------------------------------------ websocket
def test_websocket_streams_state(client):
    with client.websocket_connect("/ws") as socket:
        message = socket.receive_text()
        data = json.loads(message)
        assert "position" in data
        assert "blur" in data


def test_index_has_three_theme_options(client):
    """Тема — опция на выбор: тёмная, светлая и «авто» по настройкам системы."""
    html = client.get("/").text
    assert 'id="theme"' in html
    for value in ("auto", "dark", "light"):
        assert f'value="{value}"' in html
    # «авто» работает через системную настройку, а не через data-theme
    assert "prefers-color-scheme: light" in html
    # выбор запоминается и применяется до отрисовки, чтобы не было вспышки цвета
    assert "streamblur-theme" in html
    # светлая палитра реально определена, а не только переключатель
    assert '--bg:#f2f4f9' in html
