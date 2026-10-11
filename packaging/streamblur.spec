# -*- mode: python ; coding: utf-8 -*-
"""Сборка StreamBlur в один исполняемый файл.

Запуск (из корня репозитория):

    python -m PyInstaller packaging/streamblur.spec --noconfirm --clean

Результат — ``dist/StreamBlur/`` (папка) и, при ``ONEFILE=1``, один файл
``dist/StreamBlur.exe`` / ``dist/StreamBlur``.

Почему по умолчанию папка, а не один файл
-----------------------------------------
Однофайловый EXE при каждом запуске распаковывает себя во временную папку:
это +5–20 секунд на старт и двойной расход диска. Папка стартует мгновенно и
весит столько же. Для «просто скачал и работает» папку упаковывает Inno Setup
(``packaging/installer.iss``) — получается обычный установщик.

Если всё же нужен один файл — соберите с переменной окружения:

    ONEFILE=1 python -m PyInstaller packaging/streamblur.spec --noconfirm --clean
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # .../streamblur (корень репозитория)
PKG = ROOT / "streamblur"
ONEFILE = os.environ.get("ONEFILE", "0") == "1"
APP_NAME = "StreamBlur"

# ---------------------------------------------------------------- данные
datas: list[tuple[str, str]] = []

# веб-интерфейс: web/index.html кладём рядом с web/app.py,
# потому что WEB_DIR = Path(__file__).resolve().parent
datas.append((str(PKG / "web" / "index.html"), "streamblur/web"))

# стартовый каталог фильмов и примеры таймингов
for name in ("seed_films.json", "sample_timings.json", "sample_timings.srt"):
    src = ROOT / "data" / name
    if src.exists():
        datas.append((str(src), "data"))

# профили платформ для страховки — это данные, PyInstaller их сам не находит
datas.append((str(PKG / "safety" / "profiles.json"), "streamblur/safety"))

# модели OCR (RapidOCR): без них распознавание таймера не заведётся.
# Это ~14 МБ — осознанная плата за «всё в одном EXE».
datas += collect_data_files("rapidocr_onnxruntime")

# ---------------------------------------------------------------- импорты
hiddenimports: list[str] = []
# uvicorn грузит свои части по строкам — PyInstaller их не видит
hiddenimports += collect_submodules("uvicorn")
# провайдеры таймингов ищутся динамически; дублируем список на случай сборки
hiddenimports += [f"streamblur.timings.{m}" for m in (
    "generic_http", "imdb_provider", "link_auto", "local_file", "opensubtitles", "tmdb_provider",
)]
hiddenimports += [
    "streamblur.clock.dom_clock",
    "streamblur.clock.manual",
    "streamblur.mock_obs_server",
    "streamblur.demo",
    "streamblur.dom_bridge",
    # onnxruntime тянет провайдеры выполнения по имени
    "onnxruntime.capi.onnxruntime_pybind11_state",
]

# ---------------------------------------------------------------- сборка
a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # tkinter/тесты/документация в EXE не нужны — минус десятки мегабайт
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "pdb",
        "matplotlib", "scipy", "pandas", "IPython", "notebook",
        "pytest", "setuptools", "pip", "wheel",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name=APP_NAME,
        debug=False,
        strip=False,
        upx=False,                 # UPX ломает onnxruntime — не включаем
        console=False,             # без чёрного окна: программа открывается в браузере
        icon=str(PKG / "assets" / "icon.ico") if (PKG / "assets" / "icon.ico").exists() else None,
        version=str(ROOT / "packaging" / "version_info.txt")
        if (ROOT / "packaging" / "version_info.txt").exists() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=str(PKG / "assets" / "icon.ico") if (PKG / "assets" / "icon.ico").exists() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
