"""Тест OCR-цепочки на синтетическом кадре: рисуем таймер → распознаём → парсим.

Здесь проверяется не «заглушка», а настоящий ``streamblur.clock.ocr_clock``:
``preprocess`` → OCR-движок → ``parse_ocr_time``. Захват экрана подменяется
фейковым граббером, потому что в CI монитора нет.

Важно про шрифт: OCR-модель обучена на нормальном кегле. Если рисовать таймер
дефолтным битмап-шрифтом PIL (≈11 px), она честно прочитает «L» вместо «1» —
поэтому кадр рисуем крупным моноширинным шрифтом, как его рисует плеер.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from streamblur.clock.base import ClockReading
from streamblur.clock.ocr_clock import GrabbedFrame, OcrClock, preprocess
from streamblur.config import OcrSettings

pytest.importorskip("rapidocr_onnxruntime", reason="OCR-движок не установлен")

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
)
FONT = next((p for p in FONT_CANDIDATES if __import__("os").path.exists(p)), None)
FONT_SIZE = 40
FRAME_W, FRAME_H = 420, 140


def make_timer_image(text: str) -> np.ndarray:
    """Тёмный кадр с крупными светлыми цифрами — так рисует время плеер."""
    img = Image.new("RGB", (FRAME_W, FRAME_H), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, FONT_SIZE) if FONT else ImageFont.load_default()
    draw.text((20, FRAME_H // 2 - FONT_SIZE // 2), text, fill=(250, 250, 250), font=font)
    return np.asarray(img)


class FakeGrabber:
    """Замена ScreenGrabber: отдаёт заранее нарисованный кадр."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.settings = OcrSettings()
        self.stopped = False

    def start(self):
        return True, "fake"

    def region(self):
        return {"left": 0, "top": 0, "width": FRAME_W, "height": FRAME_H}

    def grab(self):
        data = make_timer_image(self.text)
        return GrabbedFrame(width=data.shape[1], height=data.shape[0], data=data)

    def stop(self):
        self.stopped = True


@pytest.mark.parametrize("text", ["0:12:34", "1:02:03", "00:41:09"])
def test_preprocess_keeps_image_valid(text):
    frame = GrabbedFrame(width=FRAME_W, height=FRAME_H, data=make_timer_image(text))
    image = preprocess(frame, upscale=3)
    assert image.width == FRAME_W * 3
    assert image.mode in ("1", "L")  # после пороговой обработки — ч/б


@pytest.mark.slow
@pytest.mark.skipif(FONT is None, reason="нет моноширинного TTF для отрисовки таймера")
@pytest.mark.parametrize(
    "text,expected",
    [("0:12:34", 754.0), ("1:02:03", 3723.0), ("00:41:09", 2469.0)],
)
def test_ocr_clock_reads_timer_from_frame(text, expected):
    """Полный круг: кадр → OCR → секунды фильма."""
    clock = OcrClock(OcrSettings(), grabber=FakeGrabber(text))
    ok, message = clock.start()
    assert ok, message
    try:
        reading = clock.read()
        assert reading is not None, "OCR ничего не распознал"
        assert isinstance(reading, ClockReading)
        assert reading.raw_text
        assert reading.position == pytest.approx(expected, abs=1.0), reading.raw_text
    finally:
        clock.stop()
    assert clock._grabber.stopped is True


@pytest.mark.slow
@pytest.mark.skipif(FONT is None, reason="нет моноширинного TTF для отрисовки таймера")
def test_ocr_probe_returns_preview():
    clock = OcrClock(OcrSettings(), grabber=FakeGrabber("0:07:12"))
    ok, message = clock.start()
    assert ok, message
    try:
        result = clock.probe()
        assert result["ok"] is True
        assert result["text"]
        assert clock.probe_image() is not None
    finally:
        clock.stop()


def test_ocr_clock_without_grabber_frame():
    class NoFrames(FakeGrabber):
        def grab(self):
            return None

    clock = OcrClock(OcrSettings(), grabber=NoFrames("0:00:01"), engine=object())
    assert clock.read() is None


def test_ocr_clock_reports_missing_engine(monkeypatch):
    """Если OCR-движка нет — честно говорим об этом, а не падаем."""
    import streamblur.clock.ocr_clock as mod

    monkeypatch.setattr(mod, "_OCR_OK", False)
    monkeypatch.setattr(mod, "RapidOCR", None)
    clock = OcrClock(OcrSettings(), grabber=FakeGrabber("0:12:34"))
    assert clock.read() is None
    assert clock._engine_error
