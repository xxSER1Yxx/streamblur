"""Тесты поведения часов: пауза, перемотка, протухшие данные.

Главный сценарий, ради которого всё это делается: стример поставил фильм на паузу
во время опасной сцены — блюр обязан остаться, а время не должно «уезжать» вперёд.
"""
from __future__ import annotations

import time

import pytest

from streamblur.clock import ClockTracker
from streamblur.clock.base import ClockReading
from streamblur.config import Settings


@pytest.fixture()
def clock() -> ClockTracker:
    settings = Settings()
    tracker = ClockTracker(settings=settings)
    tracker.running = True  # имитируем запущенный источник, поток не нужен
    return tracker


def feed(clock: ClockTracker, position: float, paused: bool | None = None) -> None:
    clock.apply_reading(ClockReading(position=position, confidence=0.9, paused=paused))


def test_position_extrapolates_while_playing(clock: ClockTracker):
    feed(clock, 100.0)
    clock.last_fix_monotonic = time.monotonic() - 2.0  # «прошло 2 секунды»
    assert clock.current_position() == pytest.approx(102.0, abs=0.2)
    assert clock.is_frozen() is False


def test_freeze_after_repeated_same_readings(clock: ClockTracker):
    """Три одинаковых показания подряд = пауза (pause_streak=3)."""
    feed(clock, 500.0)
    assert clock.is_frozen() is False
    feed(clock, 500.0)
    assert clock.is_frozen() is False
    feed(clock, 500.0)
    assert clock.is_frozen() is True
    # время не уезжает вперёд, сколько бы ни прошло
    clock.last_fix_monotonic = time.monotonic() - 30.0
    assert clock.current_position() == pytest.approx(500.0)


def test_blur_does_not_drop_on_pause(clock: ClockTracker):
    """Сценарий из ТЗ: пауза прямо внутри опасной сцены."""
    from streamblur.database import Database
    from streamblur.engine import BlurEngine

    class _Blur:
        def __init__(self):
            self.enabled = None

        @property
        def status(self):
            from streamblur.obs.blur import BlurState, BlurStatus

            state = BlurState.ON if self.enabled else BlurState.OFF
            return BlurStatus(state=state, connected=True)

        def set_blur(self, enabled, reason=""):
            self.enabled = enabled
            return self.status

    settings = Settings()
    blur = _Blur()
    engine = BlurEngine(Database(":memory:"), clock, blur, lambda: settings)  # type: ignore[arg-type]
    engine.timeline.reload([{"start": 480.0, "end": 540.0, "kind": "nudity", "reason": "ню"}])

    # играем, заходим в сцену
    feed(clock, 500.0)
    engine.tick()
    assert blur.enabled is True

    # стример жмёт паузу и отходит на 10 «минут»
    for _ in range(6):
        feed(clock, 500.0)
    clock.last_fix_monotonic = time.monotonic() - 600.0
    engine.tick()

    assert clock.is_frozen() is True
    assert clock.current_position() == pytest.approx(500.0)
    assert blur.enabled is True, "блюр слетел на паузе"


def test_unfreeze_when_time_moves_again(clock: ClockTracker):
    for _ in range(4):
        feed(clock, 300.0)
    assert clock.is_frozen() is True
    feed(clock, 305.0)
    assert clock.is_frozen() is False
    assert clock.current_position() >= 305.0


def test_explicit_paused_flag_freezes_immediately(clock: ClockTracker):
    feed(clock, 100.0, paused=True)
    assert clock.is_frozen() is True
    clock.last_fix_monotonic = time.monotonic() - 60.0
    assert clock.current_position() == pytest.approx(100.0)


def test_explicit_resume_unfreezes(clock: ClockTracker):
    feed(clock, 100.0, paused=True)
    assert clock.is_frozen() is True
    feed(clock, 120.0, paused=False)
    assert clock.is_frozen() is False


def test_seek_is_accepted_immediately(clock: ClockTracker):
    logs: list[str] = []
    clock.on_log = logs.append
    feed(clock, 100.0)
    feed(clock, 3000.0)  # перемотка вперёд
    assert clock.current_position() == pytest.approx(3000.0, abs=1.0)
    assert any("Перемотка" in entry for entry in logs)


def test_stale_data_stops_extrapolation(clock: ClockTracker):
    """Если источник перестал отвечать — не придумываем время."""
    feed(clock, 100.0)
    clock.last_fix_monotonic = time.monotonic() - 120.0
    assert clock.current_position() == pytest.approx(100.0)


def test_status_reports_frozen(clock: ClockTracker):
    for _ in range(4):
        feed(clock, 10.0)
    status = clock.status()
    assert status.frozen is True
    assert status.as_dict()["frozen"] is True


def test_manual_sync_resets_pause_tracking(clock: ClockTracker):
    for _ in range(4):
        feed(clock, 10.0)
    assert clock.is_frozen() is True
    clock.sync_to(1234.0)
    assert clock.is_frozen() is False
    assert clock.current_position() == pytest.approx(1234.0, abs=1.0)
