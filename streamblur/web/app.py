"""HTTP/WebSocket-интерфейс: локальная панель управления стримера."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..clock import ClockTracker
from ..config import Settings, SettingsStore
from ..database import Database
from ..demo import DemoRunner
from ..engine import BlurEngine
from ..events import EventBus
from ..film_db import FilmService
from ..obs.blur import BlurController
from ..obs.client import OBSClient
from ..scheduler import SyncJob, SyncScheduler
from ..sync import SyncService
from ..timeutils import format_timestamp, parse_timestamp
from ..safety import SafetyNet, SafetySettings, describe_environment as safety_environment, list_profiles, profile_status
from .. import __repo_url__, __version__
from ..timings import _MODULE_IMPORT_ERRORS, get_provider, list_provider_dicts

log = logging.getLogger("streamblur.web")
# app.py лежит в streamblur/web/, там же и index.html
WEB_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------- схемы
class Patch(BaseModel):
    patch: dict[str, Any]


class FilmIn(BaseModel):
    title: str
    year: int | None = None
    studios: list[str] | None = None
    production_companies: list[str] | None = None
    country: str | None = None
    runtime_minutes: int | None = None
    genres: list[str] | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    overview: str | None = None


class SegmentIn(BaseModel):
    start: float | str
    end: float | str
    reason: str | None = None
    kind: str = "nsfw"


class SyncIn(BaseModel):
    provider: str | None = None
    config: dict[str, Any] | None = None
    language: str | None = None


class ClockSyncIn(BaseModel):
    position: float | str


class OcrRegionIn(BaseModel):
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0


# ---------------------------------------------------------------- состояние
class AppState:
    """Собирает все подсистемы вместе. Создаётся один раз на процесс."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self.store = store or SettingsStore()
        self.settings: Settings = self.store.load()
        self.db = Database(self.settings.db_path)
        self.bus = EventBus()
        self.clock = ClockTracker(settings=self.settings)
        self.clock.on_log = lambda message: self.bus.publish("clock", message=message)
        self.blur = BlurController(
            self.settings.obs,
            client=OBSClient(self.settings.obs.host, self.settings.obs.port,
                             self.settings.obs.password),
        )
        # Страховка берёт кадры с экрана. Если OCR-часы уже снимают экран —
        # переиспользуем их граббер, чтобы не снимать одно и то же дважды.
        self.safety = SafetyNet(
            SafetySettings.from_settings(self.settings),
            grab=self._safety_frame,
            on_event=lambda entry: self.bus.publish(
                "safety", message=self._safety_message(entry), **{
                    k: v for k, v in entry.items() if k != "items"
                },
            ),
        )
        self._safety_grabber = None
        self.engine = BlurEngine(self.db, self.clock, self.blur, lambda: self.store.current,
                                 safety=self.safety)
        self.engine.on_event = lambda event: self.bus.publish(
            f"engine:{event.kind}",
            message=event.message,
            position=event.position,
            time=format_timestamp(event.position),
        )
        self.films = FilmService(
            self.db,
            tmdb_key=self.settings.films.tmdb_api_key,
            tmdb_language=self.settings.films.tmdb_language,
            omdb_key=self.settings.films.imdb_api_key,
        )
        self.sync = SyncService(self.db, self.films)
        self.scheduler = SyncScheduler(tick_seconds=20.0)
        self.scheduler.add(
            SyncJob(
                name="timings",
                callback=self._scheduled_sync,
                interval=self.settings.timings.refresh_interval,
                at=self.settings.timings.refresh_time,
            )
        )
        self.demo = DemoRunner(
            self.db,
            self.engine,
            on_log=lambda message: self.bus.publish("demo", message=message),
        )
        self.clients: set[WebSocket] = set()
        self._broadcaster: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------- жизненный
    def start(self) -> None:
        self.scheduler.start()
        self.engine.start()
        if self.settings.timings.auto_refresh_on_start:
            threading.Thread(target=self._scheduled_sync, daemon=True).start()
        self._stop.clear()
        self._broadcaster = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._broadcaster.start()

    def _apply_safety_settings(self) -> None:
        """Настройки страховки изменились — пересобрать и перезапустить.

        Если страховка выключена, поток и захват экрана освобождаются полностью:
        выключенная страховка не должна ничего снимать и ничего есть.
        """
        new_settings = SafetySettings.from_settings(self.settings)
        was_enabled = self.safety.settings.enabled
        self.safety.settings = new_settings
        self.safety.status.test_mode = new_settings.test_mode
        self.safety.status.enabled = new_settings.enabled
        if not new_settings.enabled:
            self.safety.stop()
            self._stop_safety_grabber()
        elif new_settings.enabled and (not was_enabled or not self.safety.status.running):
            self.safety.detector = None   # пересоздать: мог смениться путь/размер входа
            self.safety.start()

    def _stop_safety_grabber(self) -> None:
        grabber = self._safety_grabber
        self._safety_grabber = None
        if grabber is not None:
            try:
                grabber.stop()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        self._stop.set()
        self.demo.stop()
        self.safety.stop()          # сначала поток страховки, потом граббер
        self._stop_safety_grabber()
        self.engine.stop()
        self.clock.stop()
        self.blur.disconnect()
        self.scheduler.stop()
        if self._broadcaster is not None:
            self._broadcaster.join(timeout=1.5)
            self._broadcaster = None
        self._loop = None

    def apply_settings(self) -> None:
        """После изменения настроек в UI — протолкнуть их в подсистемы."""
        self.settings = self.store.load()
        self._apply_safety_settings()
        obs = self.settings.obs
        self.blur.settings = obs
        self.blur.client.host, self.blur.client.port = obs.host, obs.port
        self.blur.client.password = obs.password
        self.clock.settings = self.settings
        self.films.tmdb_key = self.settings.films.tmdb_api_key
        self.films.tmdb_language = self.settings.films.tmdb_language
        self.films.omdb_key = self.settings.films.imdb_api_key
        self.scheduler.configure(
            "timings", self.settings.timings.refresh_interval, self.settings.timings.refresh_time
        )

    # ------------------------------------------------------------- синхрон.
    def _scheduled_sync(self) -> None:
        cfg = self.settings.timings
        try:
            outcome = self.sync.sync_all(
                cfg.provider, cfg.config, cfg.language, self.settings.refresh_seconds()
            )
            self.bus.publish(
                "sync",
                message=f"Автообновление: фильмов {outcome['films']}, ошибок {outcome['errors']}",
                **{k: v for k, v in outcome.items() if k != "results"},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled sync failed")
            self.bus.publish("sync", message=f"Автообновление не удалось: {exc}", error=str(exc))

    # ------------------------------------------------------------ рассылка
    def _broadcast_loop(self) -> None:  # pragma: no cover - поток
        while not self._stop.is_set():
            loop = self._loop
            if loop is None or loop.is_closed():
                self._stop.wait(0.25)
                continue
            payload = self.state_payload()
            for socket in list(self.clients):
                try:
                    asyncio.run_coroutine_threadsafe(_safe_send(socket, payload), loop)
                except RuntimeError:
                    self.clients.discard(socket)
            self._stop.wait(0.25)

    # ------------------------------------------------------------- страховка
    def _safety_frame(self):
        """Один кадр экрана для страховки.

        Сначала пробуем граббер OCR-часов (если часы уже снимают экран — второй
        захват не нужен), иначе заводим свой и держим его, пока страховка жива.
        """
        source = self.clock.source
        grabber = getattr(source, "_grabber", None)
        if grabber is not None:
            frame = grabber.grab()
            if frame is not None:
                return frame.data
        if self._safety_grabber is None:
            try:
                from ..clock.ocr_clock import ScreenGrabber

                grabber = ScreenGrabber(self.settings.ocr)
                ok, _ = grabber.start()
                if not ok:
                    return None
                self._safety_grabber = grabber
            except Exception:  # noqa: BLE001
                return None
        frame = self._safety_grabber.grab()
        return frame.data if frame is not None else None

    @staticmethod
    def _safety_message(entry: dict[str, Any]) -> str:
        confidence = entry.get("confidence", 0)
        label = entry.get("label") or entry.get("category") or "срабатывание"
        if entry.get("test_mode"):
            return (
                f"Страховка (проверка): похоже на «{label}», уверенность {confidence:.2f} — "
                "блюр НЕ включался"
            )
        return f"Страховка: «{label}», уверенность {confidence:.2f} — блюр включён"

    def state_payload(self) -> dict[str, Any]:
        snapshot = self.engine.snapshot().as_dict()
        snapshot["clock"] = self.clock.status().as_dict()
        snapshot["blur"] = self.blur.status.as_dict()
        snapshot["scheduler"] = self.scheduler.status()
        snapshot["safety"] = self.safety.snapshot()
        return snapshot


async def _safe_send(socket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await socket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------- фабрика
def create_app(state: AppState | None = None) -> FastAPI:
    app_state = state or AppState()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_state._loop = asyncio.get_running_loop()
        app_state.start()
        try:
            yield
        finally:
            app_state.shutdown()

    app = FastAPI(title="StreamBlur", version="0.1.0", lifespan=lifespan)
    app.state.sb = app_state

    # ------------------------------------------------------------------ misc
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "repo_url": __repo_url__,
            "films": app_state.db.count_films(),
            "provider_errors": _MODULE_IMPORT_ERRORS,
        }

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        payload = app_state.state_payload()
        payload["events"] = app_state.bus.last(60)
        return payload

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return json.loads(app_state.settings.model_dump_json())

    @app.post("/api/settings")
    def put_settings(body: dict[str, Any] | None = None) -> dict[str, Any]:
        updated = app_state.store.update(_payload(body))
        app_state.apply_settings()
        app_state.bus.publish("settings", message="Настройки сохранены")
        return json.loads(updated.model_dump_json())

    # -------------------------------------------------------------- провайдеры
    @app.get("/api/providers")
    def providers() -> dict[str, Any]:
        return {"providers": list_provider_dicts(), "errors": _MODULE_IMPORT_ERRORS}

    @app.post("/api/providers/{provider_id}/test")
    def test_provider(provider_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            provider = get_provider(provider_id, _payload(body) or {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ok, message = provider.healthcheck()
        return {"ok": ok, "message": message}

    # ---------------------------------------------------------------- фильмы
    @app.get("/api/films")
    def films(query: str | None = None) -> dict[str, Any]:
        return {"films": app_state.db.list_films(query), "total": app_state.db.count_films()}

    @app.get("/api/films/{film_id}")
    def film(film_id: int) -> dict[str, Any]:
        data = app_state.db.get_film(film_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Фильм не найден")
        data["segments"] = app_state.db.list_segments(film_id)
        return data

    @app.post("/api/films")
    def create_film(body: FilmIn) -> dict[str, Any]:
        return app_state.films.add_manual(**body.model_dump(exclude_none=True))

    @app.delete("/api/films/{film_id}")
    def remove_film(film_id: int) -> dict[str, Any]:
        if not app_state.db.delete_film(film_id):
            raise HTTPException(status_code=404, detail="Фильм не найден")
        return {"ok": True}

    @app.post("/api/films/search")
    def search_films(body: dict[str, Any] | None = None) -> dict[str, Any]:
        query = str(_payload(body).get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="Пустой запрос")
        year = _payload(body).get("year")
        results = app_state.films.search_remote(query, int(year) if year else None)
        if not results:
            return {
                "results": [],
                "hint": "Не найдено. Проверьте TMDB/OMDb ключ в настройках или добавьте фильм вручную.",
            }
        return {"results": results}

    @app.post("/api/films/import")
    def import_film(body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _payload(body):
            raise HTTPException(status_code=400, detail="Пустая карточка")
        return app_state.films.import_film(_payload(body))

    @app.post("/api/films/{film_id}/refresh")
    def refresh_film(film_id: int) -> dict[str, Any]:
        try:
            return app_state.films.refresh_film(film_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # -------------------------------------------------------------- сегменты
    @app.post("/api/films/{film_id}/segments")
    def add_segment(film_id: int, body: SegmentIn) -> dict[str, Any]:
        start = _seconds(body.start)
        end = _seconds(body.end)
        if start is None or end is None or end <= start:
            raise HTTPException(status_code=400, detail="Некорректный интервал")
        if app_state.db.get_film(film_id) is None:
            raise HTTPException(status_code=404, detail="Фильм не найден")
        seg = app_state.db.add_segment(
            film_id,
            {"start": start, "end": end, "reason": body.reason, "kind": body.kind,
             "provider": "manual"},
        )
        app_state.engine.reload_segments()
        return seg

    @app.delete("/api/segments/{segment_id}")
    def delete_segment(segment_id: int) -> dict[str, Any]:
        if not app_state.db.delete_segment(segment_id):
            raise HTTPException(status_code=404, detail="Интервал не найден")
        app_state.engine.reload_segments()
        return {"ok": True}

    # ----------------------------------------------------------- синхронизация
    @app.post("/api/films/{film_id}/sync")
    def sync_film(film_id: int, body: SyncIn) -> dict[str, Any]:
        cfg = app_state.settings.timings
        provider_id = body.provider or cfg.provider
        config = body.config if body.config is not None else cfg.config
        language = body.language or cfg.language
        try:
            outcome = app_state.sync.sync_film(film_id, provider_id, config, language)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        app_state.engine.reload_segments()
        app_state.bus.publish(
            "sync", message=outcome.get("message") or outcome.get("error") or "",
            **{k: v for k, v in outcome.items() if k != "message"},
        )
        return outcome

    @app.post("/api/sync/run")
    def run_sync_now() -> dict[str, Any]:
        thread = threading.Thread(target=app_state._scheduled_sync, daemon=True)
        thread.start()
        return {"ok": True, "message": "Обновление запущено в фоне"}

    @app.get("/api/sync/log")
    def sync_log() -> dict[str, Any]:
        return {"log": app_state.db.recent_sync(60)}

    # ------------------------------------------------------------ OBS / блюр
    @app.get("/api/obs/diagnose")
    def obs_diagnose() -> dict[str, Any]:
        return app_state.blur.diagnose()

    @app.post("/api/obs/connect")
    def obs_connect() -> dict[str, Any]:
        return app_state.blur.connect().as_dict()

    @app.post("/api/obs/disconnect")
    def obs_disconnect() -> dict[str, Any]:
        app_state.blur.disconnect()
        return app_state.blur.status.as_dict()

    @app.post("/api/obs/scenes")
    def obs_scenes() -> dict[str, Any]:
        try:
            client = app_state.blur.client
            client.connect()
            scenes = client.scenes()
            current = client.current_scene()
            sources = client.inputs_of_scene(current) if current else []
            client.close()
            return {"scenes": scenes, "current": current, "sources": sources}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/blur/test")
    def blur_test(body: dict[str, Any] | None = None) -> dict[str, Any]:
        seconds = float(_payload(body).get("seconds", 2.0))
        app_state.blur.set_blur(True, reason="тест из UI")
        app_state.bus.publish("blur", message=f"Тест блюра на {seconds:.1f} с", on=True)

        def restore() -> None:
            time.sleep(max(0.2, seconds))
            if not app_state.engine.panic:
                app_state.blur.set_blur(False, reason="тест завершён")
                app_state.bus.publish("blur", message="Тест блюра завершён", on=False)

        threading.Thread(target=restore, daemon=True).start()
        return {"ok": True}

    @app.post("/api/blur")
    def set_blur(body: dict[str, Any] | None = None) -> dict[str, Any]:
        enabled = bool(_payload(body).get("enabled", False))
        return app_state.blur.set_blur(enabled, reason="ручное управление").as_dict()

    @app.post("/api/panic")
    def panic(body: dict[str, Any] | None = None) -> dict[str, Any]:
        enabled = bool(_payload(body).get("enabled", True))
        snapshot = app_state.engine.set_panic(enabled)
        return snapshot.as_dict()


    # --------------------------------------------------------------- страховка
    @app.get("/api/safety/status")
    def safety_status() -> dict[str, Any]:
        """Состояние страховки + честно: что прикрыто, а что нет."""
        data = app_state.safety.snapshot()
        from ..safety.models import status as model_catalog

        data["environment"] = safety_environment()
        data["catalog"] = model_catalog()
        data["profiles"] = [p.as_dict() for p in list_profiles()]
        data["recent"] = app_state.safety.recent(30)
        return data

    @app.post("/api/safety/check")
    def safety_check(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Один кадр с экрана прямо сейчас — что увидела нейросеть.

        Это главный инструмент проверки «для человека»: навели на подозрительный
        момент, нажали кнопку, увидели срабатывание и уверенность.
        """
        net = app_state.safety
        if not net.settings.enabled:
            raise HTTPException(
                status_code=409,
                detail="Страховка выключена. Включите её в настройках, чтобы проверить.",
            )
        if not net.status.running:
            ok, message = net.start()
            if not ok:
                return {"ok": False, "error": net.status.error or message,
                        "detections": [], "verdict": None}
        verdict = net.check_once()
        return {
            "ok": True,
            "verdict": verdict.as_dict(),
            "detections": [d.as_dict() for d in verdict.detections],
            "threshold": round(net.threshold(), 3),
            "test_mode": net.settings.test_mode,
        }

    @app.post("/api/safety/test-mode")
    def safety_test_mode(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Переключить режим проверки: логировать или реально блюрить."""
        payload = _payload(body)
        enabled = bool(payload.get("enabled", True))
        settings = app_state.store.current
        app_state.store.update({"safety": {"test_mode": enabled}})
        app_state.apply_settings()
        return {"ok": True, "test_mode": app_state.safety.settings.test_mode,
                "settings": app_state.store.current.safety.model_dump()}

    # ------------------------------------------------------------------ часы
    @app.post("/api/clock/start")
    def clock_start(body: dict[str, Any] | None = None) -> dict[str, Any]:
        source = _payload(body).get("source")
        status = app_state.clock.start(source)
        app_state.bus.publish(
            "clock", message=f"Источник времени: {status.source}",
            **{k: v for k, v in status.as_dict().items() if k != "message"},
        )
        return status.as_dict()

    @app.post("/api/clock/stop")
    def clock_stop() -> dict[str, Any]:
        app_state.clock.stop()
        return {"running": False}

    @app.post("/api/clock/sync")
    def clock_sync(body: ClockSyncIn) -> dict[str, Any]:
        position = _seconds(body.position)
        if position is None:
            raise HTTPException(status_code=400, detail="Не удалось разобрать время")
        return app_state.clock.sync_to(position).as_dict()

    @app.post("/api/clock/ocr/region")
    def ocr_region(body: OcrRegionIn) -> dict[str, Any]:
        updated = app_state.store.update({"clock": {"ocr": body.model_dump()}})
        app_state.apply_settings()
        return {"ok": True, "region": updated.clock.ocr.model_dump()}

    @app.post("/api/clock/ocr/probe")
    def ocr_probe() -> dict[str, Any]:
        from ..clock.ocr_clock import OcrClock

        probe = OcrClock(app_state.settings.clock.ocr)
        ok, message = probe.start()
        if not ok:
            return {"ok": False, "error": message}
        try:
            result = probe.probe()
            image = probe.probe_image()
            if image is not None:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                result["preview"] = "data:image/png;base64," + base64.b64encode(
                    buffer.getvalue()
                ).decode()
            return result
        finally:
            probe.stop()

    # ---------------------------------------------------------------- движок
    @app.post("/api/engine/load")
    def engine_load(body: dict[str, Any] | None = None) -> dict[str, Any]:
        film_id = _payload(body).get("film_id")
        if film_id is None:
            raise HTTPException(status_code=400, detail="Не указан film_id")
        try:
            snapshot = app_state.engine.load_film(int(film_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return snapshot.as_dict()

    @app.post("/api/engine/unload")
    def engine_unload() -> dict[str, Any]:
        app_state.engine.clear_film()
        return app_state.engine.snapshot().as_dict()

    @app.get("/api/events")
    def events() -> dict[str, Any]:
        return {"events": app_state.bus.last(100)}

    # ------------------------------------------------------------------ демо
    @app.post("/api/demo/start")
    def demo_start(body: dict[str, Any] | None = None) -> dict[str, Any]:
        speed = float(_payload(body).get("speed", 4.0) or 4.0)
        return app_state.demo.start(speed=speed).as_dict()

    @app.post("/api/demo/stop")
    def demo_stop() -> dict[str, Any]:
        return app_state.demo.stop().as_dict()

    @app.get("/api/demo/status")
    def demo_status() -> dict[str, Any]:
        return app_state.demo.status.as_dict()

    # -------------------------------------------------------------- WebSocket
    @app.websocket("/ws")
    async def websocket_endpoint(socket: WebSocket) -> None:
        await socket.accept()
        app_state.clients.add(socket)
        try:
            await socket.send_text(
                json.dumps(app_state.state_payload(), ensure_ascii=False)
            )
            while True:
                message = await socket.receive_text()
                if message == "ping":
                    await socket.send_text(
                        json.dumps({"type": "pong", "ts": time.time()})
                    )
        except WebSocketDisconnect:
            pass
        finally:
            app_state.clients.discard(socket)

    # ------------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    return app


def _payload(body: dict[str, Any] | None) -> dict[str, Any]:
    """Тело запроса как словарь.

    Принимает и «голый» JSON (``{"seconds": 2}``), и старую обёртку
    ``{"patch": {...}}`` — чтобы ни UI, ни сторонние скрипты не ломались.
    """
    data = body or {}
    inner = data.get("patch")
    return inner if isinstance(inner, dict) else data


def _seconds(value: float | str) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return parse_timestamp(str(value))
