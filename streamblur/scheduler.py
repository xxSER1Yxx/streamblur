"""Планировщик автообновления: день / неделя / месяц.

Намеренно примитивный и тестируемый: ``SyncScheduler`` только решает, *пора ли*,
а выполнение делает ``SyncService``. Так юнит-тесты не зависят от настенных часов.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .config import INTERVAL_SECONDS, RefreshInterval

log = logging.getLogger("streamblur.scheduler")


def parse_hhmm(value: str) -> tuple[int, int]:
    """``'04:30'`` → ``(4, 30)``; мусор → ``(0, 0)``."""
    try:
        hh, mm = str(value).split(":", 1)
        hour = max(0, min(23, int(hh)))
        minute = max(0, min(59, int(mm)))
        return hour, minute
    except (ValueError, AttributeError):
        return 0, 0


def next_run(
    interval: RefreshInterval,
    at: str = "04:00",
    now: datetime | None = None,
    last_run: datetime | None = None,
) -> datetime | None:
    """Когда запускать следующее обновление (или ``None`` для ``off``)."""
    seconds = INTERVAL_SECONDS.get(interval, 0)
    if seconds <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hour, minute = parse_hhmm(at)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = _add_days(candidate, 1)
    if last_run is not None:
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        earliest = last_run + _delta(seconds)
        while candidate < earliest:
            candidate = _add_days(candidate, 1)
    return candidate


def is_due(
    interval: RefreshInterval,
    at: str = "04:00",
    now: datetime | None = None,
    last_run: datetime | None = None,
) -> bool:
    """Пора ли обновляться прямо сейчас.

    Правило: никогда не запускались → да; иначе — если прошло больше интервала
    и наступило (или прошло) запланированное время суток.
    """
    seconds = INTERVAL_SECONDS.get(interval, 0)
    if seconds <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if last_run is None:
        return True
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    if (now - last_run).total_seconds() < seconds:
        return False
    hour, minute = parse_hhmm(at)
    window_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # даём часовой запас: если процесс подняли в 04:40 при at=04:00 — всё равно пора
    return now >= window_start or (now - last_run).total_seconds() >= seconds + 3600


def _add_days(dt: datetime, days: int) -> datetime:
    from datetime import timedelta

    return dt + timedelta(days=days)


def _delta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


@dataclass
class SyncJob:
    name: str
    callback: Callable[[], None]
    interval: RefreshInterval = "weekly"
    at: str = "04:00"
    last_run: datetime | None = None
    runs: int = 0
    last_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SyncScheduler(threading.Thread):
    """Фоновый поток: раз в ``tick`` секунд проверяет, не пора ли выполнять job'ы."""

    def __init__(self, tick_seconds: float = 30.0) -> None:
        super().__init__(name="streamblur-scheduler", daemon=True)
        self._jobs: dict[str, SyncJob] = {}
        self._stop = threading.Event()
        self.tick_seconds = tick_seconds
        self._clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    # ------------------------------------------------------------------ API
    def add(self, job: SyncJob) -> None:
        self._jobs[job.name] = job

    def job(self, name: str) -> SyncJob | None:
        return self._jobs.get(name)

    def configure(self, name: str, interval: RefreshInterval, at: str) -> None:
        job = self._jobs.get(name)
        if job is None:
            return
        job.interval = interval
        job.at = at
        if interval == "off":
            job.last_run = None

    def run_now(self, name: str) -> bool:
        """Принудительный запуск (кнопка «Обновить сейчас» в UI)."""
        job = self._jobs.get(name)
        if job is None:
            return False
        self._execute(job)
        return True

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for name, job in self._jobs.items():
            out[name] = {
                "interval": job.interval,
                "at": job.at,
                "runs": job.runs,
                "last_run": job.last_run.isoformat() if job.last_run else None,
                "next_run": (
                    next_run(job.interval, job.at, self._clock(), job.last_run).isoformat()
                    if job.interval != "off"
                    else None
                ),
                "last_error": job.last_error,
            }
        return out

    # ---------------------------------------------------------------- loop
    def _execute(self, job: SyncJob) -> None:
        with job.lock:
            try:
                job.callback()
                job.runs += 1
                job.last_error = None
            except Exception as exc:  # noqa: BLE001 — фоновый поток не должен падать
                job.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("sync job %s failed: %s", job.name, exc)
            finally:
                job.last_run = self._clock()

    def tick(self) -> int:
        """Один проход планировщика: выполнить всё, что созрело.

        Возвращает число выполненных задач. Метод синхронный и конечный —
        именно его вызывают тесты; ``run()`` крутит его в фоновом потоке.
        """
        now = self._clock()
        executed = 0
        for job in list(self._jobs.values()):
            if is_due(job.interval, job.at, now, job.last_run):
                self._execute(job)
                executed += 1
        return executed

    def run(self) -> None:  # pragma: no cover - фоновый поток
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.tick_seconds)
