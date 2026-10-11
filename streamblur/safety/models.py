"""Каталог моделей страховки и их скачивание.

Почему веса не лежат в репозитории
----------------------------------
``model_int8.onnx`` весит 85 МБ — это больше лимита обычного файла GitHub
(100 МБ) и раздувает клон в разы. Поэтому веса скачиваются один раз командой:

    python -m streamblur safety-models --download

Контрольная сумма зашита сюда: если файл по дороге побился или подменился,
программа его не примет.

Проверено на реальных изображениях (2026-10-10, int8, CPU)
---------------------------------------------------------
    фото 1920-х, топлесс          → nsfw  0.958   верно
    дагеротип 1855 г., ню         → sfw   0.106   ПРОМАХ
    кошка                         → sfw   0.005   верно
    Мона Лиза                     → sfw   0.003   верно
    время кадра: ~545 мс на CPU без GPU

Промах не случайность, а свойство модели: она обучена на современных фото и
плохо узнаёт сепию/гравюру/живопись. Для кино это обычно не критично, но
полагаться на страховку как на единственную защиту нельзя.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("streamblur.safety")

MB = 1024 * 1024


@dataclass(frozen=True)
class ModelInfo:
    """Одна известная модель: откуда брать и что с ней делать."""

    key: str
    filename: str
    url: str
    sha256: str
    size_mb: float
    category: str          # какую категорию профиля закрывает
    detector: str          # "classifier" | "yolo"
    labels: tuple[str, ...]
    license: str
    source: str
    note: str = ""


KNOWN_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        key="nsfw-vit-int8",
        filename="nsfw_int8.onnx",
        url=(
            "https://huggingface.co/AdamCodd/vit-base-nsfw-detector/"
            "resolve/main/onnx/model_int8.onnx"
        ),
        sha256="d25aa73fe1eec78459e35ff911e2af98f652ee919b48d9c54316c86d5ff435fa",
        size_mb=85.0,
        category="nudity",
        detector="classifier",
        labels=("sfw", "nsfw"),
        license="Apache-2.0",
        source="https://huggingface.co/AdamCodd/vit-base-nsfw-detector",
        note=(
            "ViT, вход 384×384, нормализация (x/255-0.5)/0.5. "
            "Хорошо ловит современные фото, плохо — сепию и живопись."
        ),
    ),
)


def models_dir() -> Path:
    """Папка с весами: рядом с программой в сборке, иначе в каталоге данных."""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    from ..config import Settings

    return Path(Settings().data_dir) / "models"


def find_model(info: ModelInfo) -> Path | None:
    path = models_dir() / info.filename
    return path if path.exists() else None


def verify(path: Path, info: ModelInfo) -> tuple[bool, str]:
    """Сверить контрольную сумму. Без этого скачанное вслепую доверять нельзя."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(MB), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual == info.sha256:
        return True, actual
    return False, actual


def install(info: ModelInfo, *, progress: bool = True) -> tuple[bool, str]:
    """Скачать модель, если её нет, и проверить контрольную сумму."""
    target = models_dir() / info.filename
    if target.exists():
        ok, actual = verify(target, info)
        if ok:
            return True, f"уже установлена: {target}"
        log.warning("контрольная сумма не совпала, качаю заново: %s != %s", actual, info.sha256)
        target.unlink(missing_ok=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        import urllib.request

        request = urllib.request.Request(
            info.url, headers={"User-Agent": f"StreamBlur/{__package__}"}
        )
        received = 0
        with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as out:
            while True:
                chunk = response.read(MB)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                if progress:
                    print(f"\r  {received / MB:6.1f} / {info.size_mb:.1f} МБ", end="", flush=True)
        if progress:
            print()
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return False, f"не удалось скачать: {type(exc).__name__}: {exc}"

    ok, actual = verify(tmp, info)
    if not ok:
        tmp.unlink(missing_ok=True)
        return False, (
            "контрольная сумма НЕ совпала — файл не сохранён. "
            f"ожидали {info.sha256[:16]}…, получили {actual[:16]}…"
        )
    tmp.replace(target)
    return True, f"установлена: {target} ({target.stat().st_size / MB:.1f} МБ)"


def install_all(*, progress: bool = True) -> list[tuple[ModelInfo, bool, str]]:
    return [(info, *install(info, progress=progress)) for info in KNOWN_MODELS]


def status() -> dict[str, object]:
    """Что установлено, а что нет — для UI и ``doctor``."""
    result = []
    for info in KNOWN_MODELS:
        path = find_model(info)
        entry = {
            "key": info.key,
            "filename": info.filename,
            "category": info.category,
            "size_mb": info.size_mb,
            "license": info.license,
            "source": info.source,
            "installed": path is not None,
            "verified": None,
            "note": info.note,
        }
        if path is not None:
            entry["verified"], entry["sha256"] = verify(path, info)
            entry["actual_size_mb"] = round(path.stat().st_size / MB, 1)
        result.append(entry)
    return {"models_dir": str(models_dir()), "models": result}
