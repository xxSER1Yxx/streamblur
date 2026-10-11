"""Настройки приложения.

Всё состояние живёт в JSON-файле рядом с БД (``data/settings.json``), чтобы его можно
было редактировать и из веб-UI, и руками. Модель данных — pydantic, поэтому любая
несовместимая правка файла упадёт с понятной ошибкой, а не молча сломает стрим.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RefreshInterval = Literal["off", "daily", "weekly", "monthly"]
ClockSource = Literal["ocr", "dom", "manual"]
BlurBackend = Literal["streamfx", "move", "source-toggle", "mock"]

INTERVAL_SECONDS: dict[str, int] = {
    "off": 0,
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
}


def _default_data_dir() -> Path:
    env = os.environ.get("STREAMBLUR_HOME")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        # Установленная программа живёт в Program Files — туда без прав
        # администратора не записать ни базу, ни настройки. Поэтому данные
        # держим в профиле пользователя.
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home())
            return Path(base) / "StreamBlur"
        return Path.home() / ".local" / "share" / "streamblur"
    return Path(__file__).resolve().parent.parent / "data"


class ObsSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4455
    password: str = ""
    scene: str = ""            # сцена, в которой живёт источник с фильмом ("" = текущая)
    source: str = ""           # источник захвата экрана/плеера
    backend: BlurBackend = "streamfx"
    filter_name: str = "StreamBlur Blur"
    toggle_source: str = ""    # для backend=source-toggle: имя источника-заглушки
    reconnect_seconds: int = 5


class OcrSettings(BaseModel):
    """Регион экрана, где плеер рисует время (0 = весь экран)."""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    poll_seconds: float = 0.5
    threshold: float = 0.62    # порог уверенности OCR-движка
    upscale: int = 3           # увеличение кадра перед OCR (мелкие цифры)


class ClockSettings(BaseModel):
    source: ClockSource = "manual"
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    dom_url: str = "http://127.0.0.1:8799/time"   # эндпоинт расширения/CDP-моста
    dom_poll_seconds: float = 0.25
    # сглаживание сдвига между нашим временем и временем плеера
    offset_smooth: float = 0.3
    # на сколько секунд блюрить раньше и снимать позже (запас на лаг)
    pre_roll: float = 0.6
    post_roll: float = 0.8
    # --- пауза -------------------------------------------------------------
    # Сколько одинаковых показаний таймера подряд считать паузой (включая текущее:
    # при 3 замерзаем на третьем одинаковом значении).
    pause_streak: int = 3
    # Насколько может отличаться показание, чтобы всё ещё считаться «тем же».
    pause_tolerance: float = 0.2
    # Через сколько секунд без данных перестать досчитывать время вперёд.
    stale_after: float = 3.0


class EngineSettings(BaseModel):
    poll_seconds: float = 0.05
    panic_hold_seconds: float = 0.0   # 0 = пока не отключат вручную
    fail_safe: Literal["blur", "ignore"] = "blur"
    """Если связь с OBS потеряна в момент опасной сцены — что считать безопасным."""


class TimingsSettings(BaseModel):
    provider: str = "local-file"
    language: str = "ru"
    config: dict[str, Any] = Field(default_factory=dict)
    refresh_interval: RefreshInterval = "weekly"
    refresh_time: str = "04:00"        # локальное время автообновления
    auto_refresh_on_start: bool = True


class FilmSettings(BaseModel):
    provider: str = "tmdb"
    tmdb_api_key: str = ""
    tmdb_language: str = "ru-RU"
    imdb_api_key: str = ""


class UiSettings(BaseModel):
    language: str = "ru"
    theme: str = "dark"


class SafetyNetSettings(BaseModel):
    """Локальная нейросеть-страховка поверх таймингов.

    По умолчанию выключена и в режиме проверки: пишет, что увидела, но блюр не
    трогает. Включать на «блюрить по-настоящему» стоит только после того, как
    вы лично посмотрели срабатывания во вкладке «Страховка».
    """

    enabled: bool = False
    platform: str = "twitch"        # twitch | youtube | kick | paranoid | off
    blur_threshold: float = 0.0     # 0 = брать порог из профиля платформы
    fps: float = 2.0                # кадров в секунду: 2–4 достаточно, 30 не нужно
    input_size: int = 640
    confidence: float = 0.4
    hold_seconds: float = 1.5       # сколько держать блюр после срабатывания
    model_path: str = ""            # детектор предметов; пусто = <данные>/models/*.onnx
    nsfw_model_path: str = ""       # классификатор наготы; пусто = искать там же
    test_mode: bool = True          # True = только логировать, не блюрить


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=_default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8765
    obs: ObsSettings = Field(default_factory=ObsSettings)
    clock: ClockSettings = Field(default_factory=ClockSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    timings: TimingsSettings = Field(default_factory=TimingsSettings)
    films: FilmSettings = Field(default_factory=FilmSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    safety: SafetyNetSettings = Field(default_factory=SafetyNetSettings)
    demo: bool = False

    # ---------------------------------------------------------------- helpers
    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "streamblur.db"

    @property
    def config_path(self) -> Path:
        return Path(self.data_dir) / "settings.json"

    def refresh_seconds(self) -> int:
        return INTERVAL_SECONDS.get(self.timings.refresh_interval, 0)

    def save(self) -> None:
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(self.model_dump_json())
        payload["data_dir"] = str(payload["data_dir"])
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class SettingsStore:
    """Потокобезопасная обёртка: UI-поток и движок читают одни и те же настройки."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self._settings: Settings | None = None

    # ------------------------------------------------------------- lifecycle
    def load(self) -> Settings:
        with self._lock:
            if self._settings is not None:
                return self._settings
            path = self._path
            if path is None:
                path = _default_data_dir() / "settings.json"
            self._path = path
            data: dict[str, Any] = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    bad = path.with_suffix(".broken.json")
                    bad.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                    data = {}
            if "data_dir" in data:
                data["data_dir"] = Path(str(data["data_dir"])).expanduser()
            self._settings = Settings(**data)
            Path(self._settings.data_dir).mkdir(parents=True, exist_ok=True)
            return self._settings

    @property
    def current(self) -> Settings:
        return self.load()

    def update(self, patch: dict[str, Any]) -> Settings:
        """Глубокий merge + атомарная запись на диск."""
        with self._lock:
            cur = self.load()
            merged = _deep_merge(json.loads(cur.model_dump_json()), patch)
            if "data_dir" in merged:
                merged["data_dir"] = str(Path(str(merged["data_dir"])).expanduser())
            else:
                merged["data_dir"] = str(cur.data_dir)
            self._settings = Settings(**merged)
            self._settings.save()
            return self._settings


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
