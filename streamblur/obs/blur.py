"""Управление размытием в OBS.

Поддерживаются три бэкенда — какой выбрать, зависит от установленных плагинов:

``streamfx``
    Фильтр ``StreamFX Blur`` (плагин StreamFX). Параметры: ``Blur.Type``/``Blur.Size``.
``move``
    Фильтр ``Move Transition → Blur`` (плагин obs-move-transition), вид
    ``blur_filter``.
``source-toggle``
    Без плагинов: включается/выключается **отдельный источник-заглушка**
    (размытый скриншот или чёрный экран с текстом «BRB»), лежащий поверх захвата.
``mock``
    Демонстрационный режим — ничего не делает, только пишет в лог. Нужен для тестов
    и для проверки логики без OBS.

Ключевое требование к размытию — **весь экран** (полный кадр захвата), поэтому
по умолчанию размер размытия выкручен на максимум.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..config import ObsSettings
from .client import OBSClient, OBSClientError

log = logging.getLogger("streamblur.blur")


class BlurState(str, Enum):
    OFF = "off"
    ON = "on"
    ERROR = "error"


@dataclass
class BlurStatus:
    state: BlurState = BlurState.OFF
    backend: str = "streamfx"
    source: str = ""
    filter_name: str = ""
    connected: bool = False
    since: float | None = None
    last_error: str | None = None
    switches: int = 0
    blur_size: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "backend": self.backend,
            "source": self.source,
            "filter_name": self.filter_name,
            "connected": self.connected,
            "since": self.since,
            "last_error": self.last_error,
            "switches": self.switches,
            "blur_size": self.blur_size,
        }


# Настройки фильтров по бэкендам: (filterKind, settings).
_FILTER_PRESETS: dict[str, tuple[str, dict[str, Any]]] = {
    "streamfx": ("StreamFX Blur", {"Blur.Type": "Box Blur", "Blur.Size": 64.0}),
    "move": ("blur_filter", {"blur.type": "box", "blur.size": 64.0, "resolution_scale": 0.25}),
    "obs-blur": ("obs-blur-filter", {"blur_size": 64.0}),
}


class BlurController:
    """Включает/выключает размытие. Один экземпляр на всё приложение."""

    def __init__(
        self,
        settings: ObsSettings,
        client: OBSClient | None = None,
        on_state: Callable[[BlurStatus], None] | None = None,
        blur_size: float = 64.0,
    ) -> None:
        self.settings = settings
        self.client = client or OBSClient(settings.host, settings.port, settings.password)
        self.on_state = on_state
        self.blur_size = float(blur_size)
        self._lock = threading.RLock()
        self._status = BlurStatus(backend=settings.backend)
        self._reconnect_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.auto_reconnect = True

    # -------------------------------------------------------------- status
    @property
    def status(self) -> BlurStatus:
        with self._lock:
            return self._status

    def _publish(self, **changes: Any) -> BlurStatus:
        with self._lock:
            for key, value in changes.items():
                setattr(self._status, key, value)
            snapshot = self._status
        if self.on_state:
            try:
                self.on_state(snapshot)
            except Exception:  # noqa: BLE001 — подписчик не должен ронять движок
                log.exception("on_state callback failed")
        return snapshot

    # ------------------------------------------------------------- connect
    def connect(self) -> BlurStatus:
        with self._lock:
            backend = str(self.settings.backend)
            self._status.backend = backend
            self._status.filter_name = self.settings.filter_name
            self._status.source = self.settings.source
        if backend == "mock":
            return self._publish(
                connected=True, state=BlurState.OFF, last_error=None,
                source=self.settings.source or "mock-source",
            )
        try:
            self.client.host = self.settings.host
            self.client.port = self.settings.port
            self.client.password = self.settings.password
            self.client.connect()
        except (OBSClientError, OSError, ValueError) as exc:
            self._start_reconnect()
            return self._publish(connected=False, state=BlurState.ERROR, last_error=str(exc))

        message: str | None = None
        try:
            self._resolve_source()
            if backend in _FILTER_PRESETS:
                self._ensure_filter()
            elif backend == "source-toggle":
                if not self.settings.toggle_source:
                    message = "Не указан источник-заглушка для backend=source-toggle"
        except (OBSClientError, ValueError) as exc:
            self._start_reconnect()
            return self._publish(connected=False, state=BlurState.ERROR, last_error=str(exc))
        return self._publish(
            connected=True, state=BlurState.OFF, last_error=message,
            source=self.settings.source, blur_size=self.blur_size,
        )

    def disconnect(self) -> None:
        self._stop.set()
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self._publish(connected=False, state=BlurState.OFF)

    def _resolve_source(self) -> None:
        """Если источник не задан — берём первый источник текущей сцены."""
        if self.settings.source:
            return
        scene = self.settings.scene or self.client.current_scene()
        self.settings.scene = scene
        inputs = self.client.inputs_of_scene(scene)
        if not inputs:
            raise OBSClientError(f"В сцене «{scene}» нет источников — укажите источник вручную")
        self.settings.source = inputs[0]
        log.info("Выбран источник по умолчанию: %s", self.settings.source)

    def _ensure_filter(self) -> None:
        """Создать фильтр размытия, если его ещё нет (и выключить)."""
        backend = str(self.settings.backend)
        if backend not in _FILTER_PRESETS:
            return
        kind, preset = _FILTER_PRESETS[backend]
        filters = self.client.filters_of(self.settings.source)
        existing = {f.get("filterName"): f for f in filters}
        name = self.settings.filter_name
        if name in existing:
            kind = existing[name].get("filterKind", kind)
        else:
            self.client.create_filter(self.settings.source, name, kind, dict(preset))
        settings = dict(preset)
        settings.update({k: v for k, v in preset.items() if "ize" in k or "scale" in k})
        if backend == "streamfx":
            settings["Blur.Size"] = self.blur_size
        elif backend == "move":
            settings["blur.size"] = self.blur_size
        else:
            settings["blur_size"] = self.blur_size
        try:
            self.client.set_filter_settings(self.settings.source, name, settings)
        except OBSClientError as exc:
            log.debug("Не удалось выставить силу размытия: %s", exc)
        self.client.set_filter_enabled(self.settings.source, name, False)
        self._publish(filter_name=name, blur_size=self.blur_size)

    # --------------------------------------------------------------- switch
    def set_blur(self, enabled: bool, reason: str = "") -> BlurStatus:
        """Главный метод: включить/выключить размытие всего экрана.

        Возвращает ``BlurStatus`` (не ``BlurState``) — в нём же причина ошибки,
        если переключить не удалось.
        """
        with self._lock:
            already = self._status.state == BlurState.ON
            if enabled == already and self._status.connected:
                return self._status
        if not self._status.connected and str(self.settings.backend) != "mock":
            self.connect()
        try:
            self._apply(enabled)
        except (OBSClientError, OSError) as exc:
            self._start_reconnect()
            return self._publish(
                state=BlurState.ERROR, last_error=f"{type(exc).__name__}: {exc}"
            )
        return self._publish(
            state=BlurState.ON if enabled else BlurState.OFF,
            connected=True,
            last_error=None,
            since=time.time(),
            switches=self._status.switches + 1,
        )

    def _apply(self, enabled: bool) -> None:
        backend = str(self.settings.backend)
        if backend == "mock":
            log.info("[mock] blur %s (%s)", "ON" if enabled else "OFF", self.settings.source)
            return
        if backend == "source-toggle":
            scene = self.settings.scene or self.client.current_scene()
            self.client.set_scene_item_enabled(scene, self.settings.toggle_source, enabled)
            return
        if backend not in _FILTER_PRESETS:
            raise ValueError(f"Неизвестный бэкенд размытия: {backend}")
        # фильтр могли удалить в OBS руками — пересоздаём, иначе стрим останется без защиты
        names = {f.get("filterName") for f in self.client.filters_of(self.settings.source)}
        if self.settings.filter_name not in names:
            log.warning("Фильтр «%s» отсутствует — создаю заново", self.settings.filter_name)
            self._ensure_filter()
        self.client.set_filter_enabled(self.settings.source, self.settings.filter_name, enabled)

    # ------------------------------------------------------------ reconnect
    def _start_reconnect(self) -> None:
        if not self.auto_reconnect:
            return
        thread = self._reconnect_thread
        if thread is not None and thread.is_alive():
            return  # уже переподключаемся

        def loop() -> None:  # pragma: no cover - фоновый поток
            delay = max(1.0, float(self.settings.reconnect_seconds or 5))
            while not self._stop.wait(delay):
                try:
                    status = self.connect()
                    if status.connected:
                        log.info("OBS переподключен")
                        return
                except Exception:  # noqa: BLE001
                    continue

        self._stop.clear()
        thread = threading.Thread(target=loop, name="obs-reconnect", daemon=True)
        self._reconnect_thread = thread
        thread.start()

    # ------------------------------------------------------------- проверка
    def diagnose(self) -> dict[str, Any]:
        """«Проверить OBS» из UI: подробный отчёт, что найдено."""
        if str(self.settings.backend) == "mock":
            return {"ok": True, "backend": "mock", "message": "Демо-режим, OBS не нужен"}
        try:
            self.client.host = self.settings.host
            self.client.port = self.settings.port
            self.client.password = self.settings.password
            self.client.connect()
            version = self.client.version()
            scenes = self.client.scenes()
            scene = self.settings.scene or self.client.current_scene()
            inputs = self.client.inputs_of_scene(scene) if scene else []
            filters = (
                self.client.filters_of(self.settings.source) if self.settings.source else []
            )
            self.client.close()
            return {
                "ok": True,
                "obs_version": version.get("obsVersion"),
                "websocket_version": version.get("obsWebSocketVersion"),
                "scenes": scenes,
                "current_scene": scene,
                "sources": inputs,
                "filters": [
                    {"name": f.get("filterName"), "kind": f.get("filterKind"),
                     "enabled": f.get("filterEnabled")}
                    for f in filters
                ],
            }
        except (OBSClientError, OSError, ValueError) as exc:
            self.client.close()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}



