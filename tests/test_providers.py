"""Тесты провайдеров таймингов: реестр, локальный файл, generic HTTP/JSON/HTML."""
from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

from streamblur.timings import get_provider, list_providers
from streamblur.timings.generic_http import (
    _dig,
    _select,
    segments_from_html,
    segments_from_json,
)
from streamblur.timings.local_file import load_segments_from_text

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def test_registry_has_core_providers():
    ids = {info.id for info in list_providers()}
    assert {"local-file", "generic-http", "opensubtitles", "tmdb", "imdb"} <= ids


def test_get_provider_unknown_raises():
    with pytest.raises(KeyError):
        get_provider("нет-такого")


def test_provider_info_exposes_ui_fields():
    info = get_provider("generic-http", {}).info()
    keys = {f.key for f in info.fields}
    assert {"url", "format", "json_segments_path", "selector"} <= keys
    assert all(f.label for f in info.fields)


# ---------------------------------------------------------------- local-file
def test_local_file_json_segments():
    text = json.dumps(
        {
            "segments": [
                {"start": "0:07:12", "end": "0:08:40", "reason": "драка"},
                {"start": 100, "end": 130.5},
                {"start": 500, "end": 400},  # мусор — отбрасывается
            ]
        }
    )
    segments = load_segments_from_text(text)
    assert len(segments) == 2
    # отсортировано по возрастанию начала
    assert [s["start"] for s in segments] == [100.0, 432.0]
    assert segments[1]["end"] == 520.0
    assert segments[1]["reason"] == "драка"


def test_local_file_srt_content():
    content = (DATA_DIR / "sample_timings.srt").read_text(encoding="utf-8")
    segments = load_segments_from_text(content, language="ru")
    assert segments
    assert segments[0]["start"] == 7 * 60 + 12


def test_local_file_provider_reads_repo_sample():
    provider = get_provider("local-file", {"path": str(DATA_DIR / "sample_timings.json")})
    result = provider.fetch({"title": "x"})
    assert len(result.segments) == 5
    assert result.segments[0]["start"] == 7 * 60 + 12


def test_local_file_provider_missing_file():
    provider = get_provider("local-file", {"path": "/нет/такого/файла.json"})
    with pytest.raises(FileNotFoundError):
        provider.fetch({"title": "x"})


def test_local_file_provider_without_path():
    provider = get_provider("local-file", {})
    with pytest.raises(ValueError):
        provider.fetch({"title": "x"})


# -------------------------------------------------------------- generic-http
def test_dig_json_path():
    payload = {"data": {"moments": [{"start": 1}, {"start": 2}]}}
    assert _dig(payload, "data.moments.1.start") == 2
    assert _dig(payload, "data.nope") is None
    assert _dig(payload, "") == payload


def test_segments_from_json_custom_fields():
    payload = {"data": {"scenes": [{"t0": "0:01:00", "t1": "0:01:30", "why": "кровь"}]}}
    segments = segments_from_json(
        payload, list_path="data.scenes", field_start="t0", field_end="t1", field_reason="why"
    )
    assert len(segments) == 1
    assert segments[0]["start"] == 60.0
    assert segments[0]["reason"] == "кровь"


def test_select_css_like():
    html = '<div class="scene-time" data-start="0:01:00" data-end="0:01:30"></div>'
    assert len(_select(html, ".scene-time")) == 1
    assert len(_select(html, "[data-start]")) == 1
    assert len(_select(html, ".other")) == 0


def test_segments_from_html_attributes():
    html = """
    <div class="scene" data-start="0:07:12" data-end="0:08:40">бой</div>
    <div class="scene" data-start="0:23:05" data-end="0:24:12">ню</div>
    """
    segments = segments_from_html(html, ".scene")
    assert len(segments) == 2
    assert segments[1]["start"] == 23 * 60 + 5


def test_segments_from_html_text_range():
    html = '<span class="t">00:12:04 - 00:14:31</span>'
    segments = segments_from_html(html, ".t")
    assert len(segments) == 1
    assert segments[0]["start"] == 724.0
    assert segments[0]["end"] == 871.0


def test_segments_from_html_ignores_html_entities_in_text():
    """«00:12:04» не должно превращаться в «00:12:04» html.unescape'ом."""
    html = '<div class="s">сцена 00:07:12 - 00:08:40</div>'
    segments = segments_from_html(html, ".s")
    assert len(segments) == 1
    assert segments[0]["start"] == 432.0


def test_generic_provider_builds_url_with_placeholders():
    provider = get_provider("generic-http", {"url": "https://x.test/api?t={title}&y={year}&l={language}"})
    url = provider.build_url({"title": "Ночной патруль", "year": 2023}, "ru")
    assert "t=" in url
    assert "y=2023" in url
    assert "l=ru" in url
    # кириллица в URL обязана быть percent-encoded, иначе requests может сломаться
    assert "%" in url.split("t=", 1)[1].split("&")[0]


def test_generic_provider_unknown_placeholder():
    provider = get_provider("generic-http", {"url": "https://x.test/{nope}"})
    with pytest.raises(ValueError):
        provider.build_url({"title": "x"}, "ru")


def test_provider_module_list_matches_files_on_disk():
    """Список модулей для сборки EXE не должен разъезжаться с реальными файлами.

    В заморозке PyInstaller ``pkgutil.iter_modules`` не работает, поэтому список
    продублирован в ``_PROVIDER_MODULES``. Если он разойдётся с диском, в EXE
    молча пропадут провайдеры — тест ловит это заранее.
    """
    import pkgutil

    from streamblur import timings

    on_disk = {
        mod.name
        for mod in pkgutil.iter_modules(timings.__path__)
        if not mod.name.startswith("_")
        and mod.name not in {"base", "subtitles"}  # subtitles — утилиты, не провайдер
    }
    assert set(timings._PROVIDER_MODULES) == on_disk, (
        f"разъезд: в списке {sorted(set(timings._PROVIDER_MODULES) - on_disk)} лишние, "
        f"{sorted(on_disk - set(timings._PROVIDER_MODULES))} забыты"
    )


def test_pyinstaller_spec_bundles_every_data_file():
    """Все нефайловые ресурсы пакета должны быть прописаны в spec.

    Реальный случай: ``safety/profiles.json`` не попал в сборку, и в EXE профили
    платформ оказались пустыми — при том что все тесты проходили, потому что они
    читают файл из исходников. Тест ловит это до сборки.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = (root / "packaging" / "streamblur.spec").read_text(encoding="utf-8")
    package = root / "streamblur"

    data_files = [
        p.relative_to(package).as_posix()
        for p in package.rglob("*")
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
    ]
    assert data_files, "в пакете вообще нет ресурсов — тест устарел"
    for relative in data_files:
        name = pathlib.Path(relative).name
        assert name in spec, (
            f"streamblur/{relative} не упомянут в packaging/streamblur.spec — "
            "в собранном EXE этого файла не будет"
        )


def test_spec_keeps_console_enabled():
    """EXE обязан собираться с console=True.

    С console=False PyInstaller берёт бутлоадер runw.exe, у процесса нет stdout,
    и doctor / safety-check / safety-models молча не печатают ничего. Именно так
    программа вела себя в Windows-сборке: смоук-тест падал без единой строчки лога.
    """
    import pathlib

    spec = pathlib.Path(__file__).resolve().parent.parent / "packaging" / "streamblur.spec"
    text = spec.read_text(encoding="utf-8")
    assert "console=True" in text, "в spec должен быть console=True"
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert "console=False" not in code, "console=False возвращает нерабочий CLI"


def test_hide_console_for_ui_is_safe_off_windows():
    """На Linux/macOS функция обязана просто ничего не делать и не падать."""
    from streamblur.__main__ import _hide_console_for_ui

    _hide_console_for_ui()          # не бросает исключение


def test_output_survives_ascii_only_encoding():
    """Вывод не должен падать, когда поток не понимает UTF-8.

    На Windows при перенаправлении в файл Python берёт локальную кодировку
    (cp1252). Первый же print с кириллицей или «→» бросал UnicodeEncodeError,
    а traceback с кириллицей не мог напечататься в stderr — в логе не оставалось
    вообще ничего, только код возврата 1. Так программа вела себя в CI.
    """
    import os
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "streamblur", "selftest"],
        cwd=root, env=env, capture_output=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"selftest упал при ASCII-кодировке: {result.stderr.decode(errors='replace')[:400]}"
    )
    assert "selftest завершён" in result.stdout.decode(errors="replace"), (
        "selftest не дошёл до конца — вывод обрывается на не-ASCII символе"
    )
