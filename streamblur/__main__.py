"""Точка входа CLI: ``streamblur ui | demo | mock-obs | sync | films | doctor``."""
from __future__ import annotations

import argparse
import pathlib
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _pick_port(host: str, preferred: int, attempts: int = 20) -> int:
    """Найти свободный порт: если 8765 занят — берём следующий.

    Обычный пользователь не будет разбираться, почему программа не запустилась.
    """
    import socket

    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    return preferred


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    import threading
    import webbrowser

    def opener() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — браузер мог не найтись, это не ошибка
            pass

    threading.Thread(target=opener, daemon=True, name="open-browser").start()


def cmd_ui(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import AppState, create_app

    store = AppState().store if args.reset else None
    if args.reset:
        path = store.current.config_path if store else None
        if path and Path(path).exists():
            Path(path).unlink()
            print(f"Удалён файл настроек {path}")
    state = AppState()
    if args.backend:
        state.store.update({"obs": {"backend": args.backend}})
        state.apply_settings()
    if args.mock_port:
        from .mock_obs_server import MockOBS

        server = MockOBS("127.0.0.1", args.mock_port)
        server.start()
        state.store.update({"obs": {"host": "127.0.0.1", "port": server.port}})
        state.apply_settings()
        print(f"Мок-OBS поднят на порту {server.port}")

    host = args.host or state.settings.host
    port = args.port or _pick_port(host, state.settings.port)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"StreamBlur v{__version__} → {url}")
    print("Окно программы — это браузер. Закрывать программу можно вместе с этим окном.")
    if not args.no_browser:
        _open_browser_later(url)
    uvicorn.run(create_app(state), host=host, port=port, log_level="info")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Демо целиком в одном процессе: мок-OBS + UI + виртуальный плеер."""
    import uvicorn

    from .web.app import AppState, create_app

    state = AppState()
    state.store.update(
        {
            "obs": {"backend": "mock"},
            "clock": {"source": "manual"},
            "timings": {"auto_refresh_on_start": False},
        }
    )
    state.apply_settings()
    app = create_app(state)

    import threading

    def start_demo() -> None:
        time.sleep(2.0)
        status = state.demo.start(speed=args.speed)
        print(f"[demo] {status.message}")

    threading.Thread(target=start_demo, daemon=True).start()
    print(f"Демо: http://{args.host}:{args.port} → вкладка «Демо»")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_mock_obs(args: argparse.Namespace) -> int:
    from .mock_obs_server import main as mock_main

    sys.argv = ["streamblur-mock-obs", "--host", args.host, "--port", str(args.port)]
    if args.password:
        sys.argv += ["--password", args.password]
    mock_main()
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .config import SettingsStore
    from .database import Database
    from .film_db import FilmService
    from .sync import SyncService

    store = SettingsStore()
    settings = store.load()
    db = Database(settings.db_path)
    films = FilmService(
        db,
        tmdb_key=settings.films.tmdb_api_key,
        tmdb_language=settings.films.tmdb_language,
        omdb_key=settings.films.imdb_api_key,
    )
    service = SyncService(db, films)
    provider = args.provider or settings.timings.provider
    config = json.loads(args.config) if args.config else settings.timings.config
    if args.film_id:
        outcome = service.sync_film(args.film_id, provider, config, args.language)
    else:
        outcome = service.sync_all(provider, config, args.language, settings.refresh_seconds())
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome.get("errors", 0) == 0 or outcome.get("ok") else 1


def cmd_films(args: argparse.Namespace) -> int:
    from .config import SettingsStore
    from .database import Database
    from .film_db import FilmService

    settings = SettingsStore().load()
    db = Database(settings.db_path)
    service = FilmService(
        db,
        tmdb_key=settings.films.tmdb_api_key,
        tmdb_language=settings.films.tmdb_language,
        omdb_key=settings.films.imdb_api_key,
    )
    if args.add:
        film = service.add_manual(
            args.add,
            year=args.year,
            studios=_split(args.studios),
            production_companies=_split(args.companies),
            country=args.country,
            runtime_minutes=args.runtime,
        )
        print(json.dumps(film, ensure_ascii=False, indent=2))
    elif args.import_file:
        payload = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("films", [payload])
        for item in items:
            film = service.add_manual(**{
                k: v for k, v in item.items()
                if k in {"title", "year", "studios", "production_companies", "country",
                         "runtime_minutes", "genres", "imdb_id", "tmdb_id", "overview"}
            })
            print(f"+ #{film['id']} {film['title']} ({film['year'] or '—'})")
    else:
        rows = db.list_films(args.query)
        for film in rows:
            studios = ", ".join(film["studios"] + film["production_companies"]) or "—"
            print(
                f"#{film['id']:<4} {film['title'][:40]:<42} {film['year'] or '—':<6} "
                f"интервалов: {film['segments_count']:<4} {studios[:40]}"
            )
        if not rows:
            print("База пуста. Добавьте: streamblur films --add \"Название\" --year 2024")
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    """Пошаговая самодиагностика: что именно ломается и на каком шаге.

    Нужна прежде всего для собранного EXE: когда программа умирает молча
    (пустой stderr, код 1), обычный doctor не успевает ничего рассказать.
    Здесь каждый шаг печатается немедленно и обернут в try/except, поэтому
    последний напечатанный шаг и есть место поломки.
    """
    import traceback

    def step(label: str, func):
        print(f"→ {label}", flush=True)
        try:
            result = func()
        except BaseException:  # noqa: BLE001 — нужно увидеть даже SystemExit
            print(f"✗ {label}: ошибка", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            return None
        print(f"✓ {label}: {result}", flush=True)
        return result

    print(f"StreamBlur v{__version__} selftest", flush=True)
    step(f"python {sys.version.split()[0]} | frozen={getattr(sys, 'frozen', False)}",
         lambda: sys.executable)
    step("faulthandler", lambda: __import__("faulthandler").is_enabled())
    step("numpy", lambda: __import__("numpy").__version__)
    step("PIL", lambda: __import__("PIL").__version__)
    step("cv2", lambda: __import__("cv2").__file__)
    step("mss", lambda: __import__("mss").__version__)
    step("onnxruntime", lambda: __import__("onnxruntime").__version__)
    step("pydantic", lambda: __import__("pydantic").VERSION)
    step("sqlalchemy", lambda: __import__("sqlalchemy").__version__)

    def _settings():
        from .config import SettingsStore

        loaded = SettingsStore().load()
        return f"данные={loaded.data_dir} база={loaded.db_path}"

    step("настройки и папка данных", _settings)

    def _database():
        from .config import Settings
        from .database import Database

        return f"фильмов: {Database(Settings().db_path).count_films()}"

    step("база данных", _database)

    def _providers():
        from .timings import list_providers

        return ", ".join(p.id for p in list_providers())

    step("провайдеры таймингов", _providers)

    def _ocr():
        from .clock.ocr_clock import _MSS_OK, _OCR_OK

        return f"mss={'ok' if _MSS_OK else 'НЕТ'} OCR={'ok' if _OCR_OK else 'НЕТ'}"

    step("захват экрана и OCR", _ocr)

    def _safety():
        from .safety import describe_environment

        env = describe_environment()
        return f"моделей: {len(env['model_files'])} | onnxruntime: {env.get('onnxruntime') or 'нет'}"

    step("страховка", _safety)

    def _obs():
        from .config import Settings
        from .obs.blur import BlurController

        report = BlurController(Settings().obs).diagnose()
        return "подключён" if report.get("ok") else f"нет связи: {report.get('error')}"

    step("связь с OBS", _obs)

    print("selftest завершён", flush=True)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    """Диагностика окружения: что установлено, что подключается."""
    from .clock.ocr_clock import _MSS_OK, _OCR_OK
    from .config import SettingsStore
    from .database import Database
    from .obs.blur import BlurController
    from .timings import _MODULE_IMPORT_ERRORS, list_providers

    settings = SettingsStore().load()
    print(f"StreamBlur v{__version__}", flush=True)
    print(f"  python      : {sys.version.split()[0]}", flush=True)
    print(f"  данные      : {settings.data_dir}", flush=True)
    print(f"  база        : {settings.db_path} "
          f"(фильмов: {Database(settings.db_path).count_films()})", flush=True)
    print(f"  mss (экран) : {'ok' if _MSS_OK else 'НЕТ — pip install mss'}", flush=True)
    print(f"  OCR         : {'ok' if _OCR_OK else 'НЕТ — pip install rapidocr-onnxruntime'}",
          flush=True)
    print(f"  провайдеры  : {', '.join(p.id for p in list_providers())}", flush=True)
    if _MODULE_IMPORT_ERRORS:
        for name, error in _MODULE_IMPORT_ERRORS.items():
            print(f"    ! {name}: {error}")
    print("  OBS         : проверяю связь…", flush=True)
    controller = BlurController(settings.obs)
    report = controller.diagnose()
    if report.get("ok"):
        print(f"  OBS         : подключён ({report.get('obs_version', 'mock')})")
        if report.get("sources"):
            print(f"    сцена «{report.get('current_scene')}»: {', '.join(report['sources'])}")
    else:
        print(f"  OBS         : нет связи — {report.get('error')}")
        print("    → OBS: Инструменты → Настройки obs-websocket → включить")
    return 0


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _safety_check_images(net, paths: list[str]) -> int:
    """Прогнать модель по файлам и напечатать вердикт по каждому.

    Возвращает 0, если все файлы обработаны. Печатает честный ответ: что увидела
    модель и с какой уверенностью, включая «ничего не увидела».
    """
    import numpy as np
    from PIL import Image

    print(f"\nПроверяю {len(paths)} файл(ов), порог блюра {net.threshold():.2f}\n")
    problems = 0
    for path in paths:
        target = pathlib.Path(path)
        if not target.exists():
            print(f"  {target.name:<28} НЕТ ТАКОГО ФАЙЛА")
            problems += 1
            continue
        try:
            frame = np.asarray(Image.open(target).convert("RGB"))
        except Exception as exc:  # noqa: BLE001
            print(f"  {target.name:<28} не читается: {exc}")
            problems += 1
            continue
        verdict = net.check_once(frame)
        flag = "БЛЮР" if verdict.blur else "  —  "
        if verdict.detections:
            item = verdict.detections[0]
            print(f"  {target.name:<28} {flag}  {item.category:<8} "
                  f"{item.label:<8} {item.confidence:.3f}")
        else:
            print(f"  {target.name:<28} {flag}  чисто")
    net.stop()
    print(f"\nВсего проверено: {net.status.frames_checked}")
    return 0 if problems == 0 else 1


def cmd_safety_check(args: argparse.Namespace) -> int:
    """Личная проверка страховки: смотрим кадры и печатаем, что найдено.

    Команда существует именно для человека: нейросеть нельзя «протестировать»
    автоматически на адекватность — только посмотреть глазами на реальном видео.
    """
    from .clock.ocr_clock import OcrClock, ScreenGrabber
    from .config import Settings
    from .safety import SafetyNet, SafetySettings, profile_status

    settings = Settings()
    safety_settings = SafetySettings.from_settings(settings)
    if args.platform:
        safety_settings.platform = args.platform
    if args.confidence:
        safety_settings.confidence = args.confidence
    safety_settings.enabled = True          # для проверки включаем принудительно
    safety_settings.test_mode = True        # и никогда не блюрим из консоли

    status = profile_status(safety_settings.platform)
    print(f"Профиль: {status['label']} (порог блюра {status['blur_threshold']})")
    for item in status["covered"]:
        print(f"  ✅ распознаётся: {item['label']}")
    for item in status["not_covered"]:
        print(f"  ⚠️  НЕ распознаётся текущей моделью: {item['label']}")
    if status["warning"]:
        print(f"  {status['warning']}")

    net = SafetyNet(safety_settings)
    ok, message = net.start()
    if not ok:
        print(f"\nСтраховка не запустилась: {message}")
        print("Скачайте веса модели: python -m streamblur safety-models --download")
        return 1
    print(f"\n{message}")

    # ---------------------------------------------------- проверка по файлам
    # Экран не у всех доступен (сервер, удалённая машина, скриншот с телефона).
    # Поэтому проверить модель можно и на своих изображениях.
    if args.image:
        return _safety_check_images(net, args.image)

    grabber = ScreenGrabber(settings.ocr)
    ok, message = grabber.start()
    if not ok:
        print(f"Не удалось захватить экран: {message}")
        net.stop()
        return 1

    region = grabber.region()
    print(f"Смотрю экран {region['width']}x{region['height']}, "
          f"{args.frames} кадров, порог уверенности {safety_settings.confidence}")
    print("Нажмите Ctrl+C, чтобы закончить.\n")

    hits = 0
    try:
        for index in range(args.frames):
            frame = grabber.grab()
            if frame is None:
                print("  кадр не получен")
                continue
            verdict = net.check_once(frame.data)
            if verdict.detections:
                hits += 1
                for detection in verdict.detections:
                    flag = "БЛЮР" if verdict.blur and detection.category == verdict.category else "—"
                    print(f"  [{index + 1:>3}] {detection.label:<12} "
                          f"уверенность {detection.confidence:.2f} "
                          f"категория {detection.category:<8} {flag}")
            else:
                print(f"  [{index + 1:>3}] чисто")
            time.sleep(max(0.05, 1.0 / max(0.5, safety_settings.fps)))
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    finally:
        grabber.stop()
        net.stop()

    print(f"\nИтого: проверено {net.status.frames_checked} кадров, "
          f"срабатываний в {hits} кадрах")
    print("Если срабатываний слишком много — поднимите порог уверенности. "
          "Если нужная сцена не поймана — опустите.")
    return 0


def cmd_safety_models(args: argparse.Namespace) -> int:
    """Показать, скачать и проверить веса моделей страховки."""
    from .safety.models import KNOWN_MODELS, install, status as model_status

    info = model_status()
    print(f"Папка моделей: {info['models_dir']}\n")
    for item in info["models"]:
        state = "установлена" if item["installed"] else "НЕТ"
        verified = ""
        if item["installed"]:
            verified = " (контрольная сумма верна)" if item["verified"] else " (! сумма НЕ совпала)"
        print(f"  {item['filename']:<22} {state}{verified}  {item['size_mb']:.0f} МБ")
        print(f"    категория:  {item['category']} | лицензия: {item['license']}")
        print(f"    откуда:     {item['source']}")
        if item["note"]:
            print(f"    примечание: {item['note']}")
        print()

    if not args.download:
        if any(not m["installed"] for m in info["models"]):
            print("Чтобы скачать недостающее:")
            print("  python -m streamblur safety-models --download")
        print("\nДетектор оружия (YOLO) в каталог не входит — его ONNX-веса")
        print("нужно положить в папку моделей вручную, см. docs/SAFETY.md")
        return 0

    results = []
    for model in KNOWN_MODELS:
        print(f"Скачиваю {model.filename} ({model.size_mb:.0f} МБ)…")
        ok, message = install(model)
        print(f"  → {message}")
        results.append(ok)
    return 0 if all(results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamblur", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    # глобальный, чтобы «StreamBlur.exe --no-browser» работал без подкоманды
    parser.add_argument("--no-browser", action="store_true",
                        help="не открывать браузер автоматически")
    # подкоманда не обязательна: двойной клик по EXE = запуск панели
    sub = parser.add_subparsers(dest="command")

    ui = sub.add_parser("ui", help="запустить веб-панель")
    ui.add_argument("--host", default=None)
    ui.add_argument("--port", type=int, default=None)
    ui.add_argument("--backend", default=None,
                    help="streamfx | move | obs-blur | source-toggle | mock")
    ui.add_argument("--mock-port", type=int, default=0,
                    help="поднять мок-OBS на этом порту и подключиться к нему")
    ui.add_argument("--reset", action="store_true", help="удалить настройки перед стартом")
    ui.set_defaults(func=cmd_ui)

    safety = sub.add_parser("safety-check",
                            help="лично проверить, что ловит нейросеть-страховка")
    safety.add_argument("--frames", type=int, default=20, help="сколько кадров посмотреть")
    safety.add_argument("--platform", default=None,
                        help="twitch | youtube | kick | paranoid | off")
    safety.add_argument("--confidence", type=float, default=None,
                        help="порог уверенности детектора (0..1)")
    safety.add_argument("--image", nargs="+", metavar="ФАЙЛ",
                        help="проверить не экран, а свои изображения (можно несколько)")
    safety.set_defaults(func=cmd_safety_check)

    safety_models = sub.add_parser("safety-models",
                                   help="скачать и проверить веса моделей страховки")
    safety_models.add_argument("--download", action="store_true",
                               help="скачать недостающие модели")
    safety_models.set_defaults(func=cmd_safety_models)

    demo = sub.add_parser("demo", help="демо: мок-OBS + виртуальный плеер + UI")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--speed", type=float, default=4.0, help="ускорение фильма")
    demo.set_defaults(func=cmd_demo)

    mock = sub.add_parser("mock-obs", help="запустить мок obs-websocket отдельно")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=4456)
    mock.add_argument("--password", default="")
    mock.set_defaults(func=cmd_mock_obs)

    sync = sub.add_parser("sync", help="подтянуть тайминги")
    sync.add_argument("--film-id", type=int, default=None)
    sync.add_argument("--provider", default=None)
    sync.add_argument("--config", default=None, help="JSON с настройками провайдера")
    sync.add_argument("--language", default="ru")
    sync.set_defaults(func=cmd_sync)

    films = sub.add_parser("films", help="база фильмов")
    films.add_argument("--query", default=None)
    films.add_argument("--add", default=None, help="добавить фильм по названию")
    films.add_argument("--import-file", default=None, help="JSON со списком фильмов")
    films.add_argument("--year", type=int)
    films.add_argument("--country")
    films.add_argument("--runtime", type=int)
    films.add_argument("--studios")
    films.add_argument("--companies")
    films.set_defaults(func=cmd_films)

    selftest = sub.add_parser("selftest",
                              help="пошаговая самодиагностика (что именно сломано)")
    selftest.set_defaults(func=cmd_selftest)
    doctor = sub.add_parser("doctor", help="диагностика окружения")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _install_crash_diagnostics() -> None:
    """Сделать так, чтобы причина падения всегда оказывалась в логе.

    Без этого при жёстком падении в нативной библиотеке (onnxruntime, cv2, mss)
    процесс умирает молча: пустой stderr и код возврата 1 — и понять, что
    случилось, невозможно. Именно так это выглядело в CI на Windows.
    """
    import faulthandler
    import sys

    try:
        sys.stderr.reconfigure(line_buffering=True)
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 — не все потоки это умеют
        pass
    # all_threads=True — дамп по всем потокам, а не только по упавшему.
    faulthandler.enable(file=sys.stderr, all_threads=True)


def _hide_console_for_ui() -> None:
    """Спрятать чёрное окно консоли, когда запущена команда ui.

    EXE собран с console=True — иначе у процесса нет stdout и все консольные
    команды (doctor, safety-check, safety-models) молча не печатают ничего.
    Но пользователю, который просто открывает программу, чёрное окно не нужно:
    панель живёт в браузере. Поэтому окно прячем, а вывод остаётся.

    Не прячем, если вывод перенаправлен в файл или конвейер: значит, программу
    вызвали из терминала намеренно и человек хочет видеть, что она пишет.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        if not handle or handle == -1:               # вывода нет — прятать нечего
            return
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return                                   # живая консоль — это терминал
        window = kernel32.GetConsoleWindow()
        if window:
            user32.ShowWindow(window, 0)             # SW_HIDE
    except Exception:  # noqa: BLE001 — косметика не должна ронять программу
        pass


def main(argv: list[str] | None = None) -> int:
    _install_crash_diagnostics()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args([*(argv or []), "ui"])
    if getattr(args, "command", None) == "ui":
        _hide_console_for_ui()
    _configure_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
