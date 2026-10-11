"""Точка входа для PyInstaller.

Отдельный файл нужен потому, что у консоольной программы ``python -m streamblur``
нет «файла», на который может указать PyInstaller.

Что здесь происходит при двойном клике по EXE:

1. Консольное окно не показывается (``console=False`` в spec) — программа
   открывается сразу в браузере.
2. Если панель уже запущена (второй клик по ярлыку) — просто открываем браузер
   на уже работающем экземпляре, вместо ошибки «порт занят».
3. Любая авария пишется в ``streamblur-crash.log`` рядом с программой:
   без консоли пользователю больше некуда посмотреть.
"""
from __future__ import annotations

import os
import sys
import traceback
import webbrowser
from pathlib import Path


def _crash_log_path() -> Path:
    """Куда писать аварию: рядом с EXE, иначе — в домашнюю папку."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base / "streamblur-crash.log"
    except OSError:
        return Path.home() / "streamblur-crash.log"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from streamblur.__main__ import main as cli_main

    # «StreamBlur.exe» без аргументов = запустить панель
    argv = sys.argv[1:] or ["ui"]
    return cli_main(argv)


if __name__ == "__main__":
    # данные пользователя (настройки, база) — в %APPDATA%, а не в Program Files,
    # куда обычный пользователь писать не может
    os.environ.setdefault("STREAMBLUR_HOME", str(Path.home() / "StreamBlur"))
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — консольного окна нет, пишем в файл
        log = _crash_log_path()
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("\n=== StreamBlur crash ===\n")
            handle.write(traceback.format_exc())
        try:
            webbrowser.open(log.as_uri())
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(1)
