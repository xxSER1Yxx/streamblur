"""Тесты планировщика автообновления (день / неделя / месяц)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from streamblur.scheduler import SyncJob, SyncScheduler, is_due, next_run, parse_hhmm

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_parse_hhmm():
    assert parse_hhmm("04:30") == (4, 30)
    assert parse_hhmm("25:99") == (23, 59)
    assert parse_hhmm("мусор") == (0, 0)


def test_next_run_off():
    assert next_run("off", "04:00", NOW) is None


def test_next_run_daily_in_future_today():
    when = next_run("daily", "23:00", NOW)
    assert when is not None
    assert when.date() == NOW.date()
    assert when.hour == 23


def test_next_run_daily_rolls_to_tomorrow():
    when = next_run("daily", "04:00", NOW)
    assert when.date() == (NOW + timedelta(days=1)).date()


def test_next_run_weekly_respects_last_run():
    last = NOW - timedelta(days=1)
    when = next_run("weekly", "04:00", NOW, last)
    assert when >= last + timedelta(days=7)


def test_next_run_monthly_at_least_30_days():
    last = NOW - timedelta(days=5)
    when = next_run("monthly", "04:00", NOW, last)
    assert when >= last + timedelta(days=30)


def test_is_due_never_ran():
    assert is_due("daily", "04:00", NOW, None) is True


def test_is_due_off():
    assert is_due("off", "04:00", NOW, None) is False


def test_is_due_too_early():
    last = NOW - timedelta(hours=1)
    assert is_due("daily", "04:00", NOW, last) is False


def test_is_due_after_interval():
    last = NOW - timedelta(days=2)
    assert is_due("daily", "04:00", NOW, last) is True


def test_is_due_weekly_not_yet():
    last = NOW - timedelta(days=3)
    assert is_due("weekly", "04:00", NOW, last) is False


def test_scheduler_executes_job_when_due():
    calls: list[int] = []
    scheduler = SyncScheduler(tick_seconds=0.05)
    scheduler._clock = lambda: NOW
    scheduler.add(SyncJob(name="timings", callback=lambda: calls.append(1), interval="daily"))
    scheduler.tick()  # один проход планировщика
    assert calls == [1]
    job = scheduler.job("timings")
    assert job.runs == 1
    assert job.last_run == NOW


def test_scheduler_skips_when_not_due():
    calls: list[int] = []
    scheduler = SyncScheduler(tick_seconds=0.05)
    scheduler._clock = lambda: NOW
    job = SyncJob(name="timings", callback=lambda: calls.append(1), interval="weekly",
                  last_run=NOW - timedelta(days=1))
    scheduler.add(job)
    scheduler.tick()
    assert calls == []


def test_scheduler_run_now_forces_execution():
    calls: list[int] = []
    scheduler = SyncScheduler()
    scheduler.add(SyncJob(name="timings", callback=lambda: calls.append(1), interval="off"))
    assert scheduler.run_now("timings") is True
    assert calls == [1]
    assert scheduler.run_now("нет-такого") is False


def test_scheduler_captures_job_errors():
    def boom() -> None:
        raise RuntimeError("сеть упала")

    scheduler = SyncScheduler()
    scheduler.add(SyncJob(name="timings", callback=boom, interval="daily"))
    scheduler.tick()
    status = scheduler.status()["timings"]
    assert status["runs"] == 0
    assert "сеть упала" in status["last_error"]


def test_scheduler_configure_turns_off():
    scheduler = SyncScheduler()
    scheduler.add(SyncJob(name="timings", callback=lambda: None, interval="daily"))
    scheduler.configure("timings", "off", "04:00")
    assert scheduler.status()["timings"]["next_run"] is None
