"""Тесты таймлайна и логики принятия решения (pre-roll/post-roll, PANIC)."""
from __future__ import annotations

import pytest

from streamblur.config import Settings
from streamblur.engine import BlurEngine, Timeline

SEGMENTS = [
    {"start": 60.0, "end": 90.0, "kind": "violence", "reason": "драка"},
    {"start": 300.0, "end": 330.0, "kind": "nudity", "reason": "ню"},
]


@pytest.fixture()
def timeline() -> Timeline:
    return Timeline(SEGMENTS)


def test_timeline_find(timeline: Timeline):
    assert timeline.find(59.9) is None
    assert timeline.find(60.0)["reason"] == "драка"
    assert timeline.find(89.9)["reason"] == "драка"
    assert timeline.find(90.0) is None
    assert timeline.contains(315.0)


def test_timeline_next_segment(timeline: Timeline):
    assert timeline.next_segment(0.0)["start"] == 60.0
    assert timeline.next_segment(61.0)["start"] == 300.0
    assert timeline.next_segment(400.0) is None


def test_timeline_ignores_inverted_segments():
    tl = Timeline([{"start": 10, "end": 5}, {"start": 0, "end": 5}])
    assert len(tl) == 1
    assert tl.total_seconds() == 5.0


def test_timeline_reload(timeline: Timeline):
    timeline.reload([{"start": 0, "end": 1, "kind": "x", "reason": "y"}])
    assert len(timeline) == 1
    assert timeline.find(0.5) is not None


class _StubBlur:
    """Замена BlurController: запоминает команды, ничего не дёргает в OBS."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    @property
    def status(self):  # noqa: D102
        from streamblur.obs.blur import BlurState, BlurStatus

        state = BlurState.ON if self.calls and self.calls[-1] else BlurState.OFF
        return BlurStatus(state=state, connected=True, backend="mock")

    def set_blur(self, enabled: bool, reason: str = ""):  # noqa: D102
        self.calls.append(bool(enabled))
        return self.status


class _StubClock:
    def __init__(self, position: float = 0.0) -> None:
        self.position = position

    def current_position(self) -> float:
        return self.position

    def status(self):  # noqa: D102
        from streamblur.clock.base import ClockStatus

        return ClockStatus(running=True, source="stub", position=self.position, confidence=1.0)


def _engine(position: float = 0.0) -> tuple[BlurEngine, _StubBlur, _StubClock, Settings]:
    from streamblur.database import Database

    settings = Settings()
    clock = _StubClock(position)
    blur = _StubBlur()
    engine = BlurEngine(Database(":memory:"), clock, blur, lambda: settings)  # type: ignore[arg-type]
    engine.timeline.reload(SEGMENTS)
    return engine, blur, clock, settings


def test_decide_off_outside_segments():
    engine, *_ = _engine()
    should, reason, seg = engine.decide(10.0)
    assert should is False
    assert seg is None


def test_decide_on_inside_segment():
    engine, *_ = _engine()
    should, reason, seg = engine.decide(70.0)
    assert should is True
    assert seg["reason"] == "драка"
    assert "драка" in reason


def test_decide_pre_roll():
    engine, _, _, settings = _engine()
    should, reason, _ = engine.decide(60.0 - settings.clock.pre_roll / 2)
    assert should is True
    assert "pre-roll" in reason


def test_decide_post_roll():
    engine, _, _, settings = _engine()
    should, reason, _ = engine.decide(90.0 + settings.clock.post_roll / 2)
    assert should is True
    assert "post-roll" in reason


def test_decide_after_post_roll_is_off():
    engine, _, _, settings = _engine()
    should, _, _ = engine.decide(90.0 + settings.clock.post_roll + 1.0)
    assert should is False


def test_panic_overrides_everything():
    engine, *_ = _engine()
    engine.panic = True
    should, reason, _ = engine.decide(10.0)
    assert should is True
    assert reason == "PANIC"


def test_tick_switches_blur_on_and_off():
    engine, blur, clock, _ = _engine()
    clock.position = 70.0
    engine.tick()
    assert blur.calls[-1] is True
    clock.position = 200.0
    engine.tick()
    assert blur.calls[-1] is False
    # повторный tick в том же состоянии не дёргает OBS лишний раз
    calls = len(blur.calls)
    engine.tick()
    assert len(blur.calls) == calls


def test_tick_logs_events():
    engine, _, clock, _ = _engine()
    clock.position = 310.0
    engine.tick()
    kinds = [e["kind"] for e in engine.recent_events()]
    assert "blur_on" in kinds


def test_set_panic_turns_blur_on_and_off():
    engine, blur, _, _ = _engine()
    engine.set_panic(True)
    assert blur.calls[-1] is True
    assert engine.snapshot().panic is True
    engine.set_panic(False)
    assert blur.calls[-1] is False
