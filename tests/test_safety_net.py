"""Тесты нейросетевой страховки.

Реальные веса модели сюда не входят (их нельзя распространять с кодом), поэтому
проверяется всё, что можно проверить без них:
  * профили платформ и честный отчёт «что прикрыто, а что нет»;
  * чистые функции предобработки (letterbox, NMS, пересчёт координат);
  * разбор выхода YOLO обеих раскладок;
  * логика вердикта и главное правило: страховка НЕ имеет права снять блюр.
"""
from __future__ import annotations

import numpy as np
import pytest

from streamblur.config import Settings
_PNG_1PX = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'


from streamblur.safety import (
    DETECTABLE_CATEGORIES,
    Detection,
    SafetyNet,
    SafetySettings,
    get_profile,
    list_profiles,
    profile_ids,
    profile_status,
)
from streamblur.safety.detector_base import (
    boxes_to_original,
    iou,
    letterbox,
    non_max_suppression,
)
from streamblur.safety.yolo_onnx import YoloOnnxDetector, parse_yolo_output


# --------------------------------------------------------------- профили
def test_profiles_present_and_off_is_really_off():
    ids = profile_ids()
    for expected in ("twitch", "youtube", "kick", "paranoid", "off"):
        assert expected in ids
    assert get_profile("off").enabled_categories() == []
    # порог «выключенного» профиля выше единицы — ничего не пройдёт
    assert get_profile("off").blur_threshold > 1.0


def test_unknown_profile_falls_back_to_default():
    assert get_profile("несуществующий").id == "twitch"


def test_profile_status_is_honest_about_coverage():
    """Профиль обещает больше, чем модели умеют, и это не скрывается."""
    status = profile_status("twitch")
    covered = {c["id"] for c in status["covered"]}
    not_covered = {c["id"] for c in status["not_covered"]}
    assert covered <= set(DETECTABLE_CATEGORIES)
    # нагота закрыта классификатором sfw/nsfw, оружие — детектором COCO
    assert {"nudity", "weapon"} <= covered
    # а вот крови в COCO нет: профиль её обещает, и программа обязана это признать
    assert "blood" in not_covered
    assert status["warning"], "предупреждение о неприкрытых категориях обязательно"


def test_every_profile_category_is_known():
    from streamblur.safety import category_labels

    labels = category_labels()
    for profile in list_profiles():
        for category in profile.categories:
            assert category in labels, f"категория {category} не описана в profiles.json"


# --------------------------------------------------- чистые функции геометрии
def test_letterbox_keeps_aspect_and_pads():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    canvas, scale, pad_x, pad_y = letterbox(image, 640)
    assert canvas.size == (640, 640)
    assert scale == pytest.approx(1.0)
    assert pad_x == 0
    assert pad_y == 80  # (640 - 480) / 2


def test_boxes_round_trip_to_original_coordinates():
    """Координаты из letterbox-квадрата возвращаются в доли исходного кадра."""
    boxes = [(0 + 0, 80 + 0, 640, 80 + 480)]  # весь полезный квадрат
    result = boxes_to_original(boxes, scale=1.0, pad_x=0, pad_y=80, src_w=640, src_h=480)
    x1, y1, x2, y2 = result[0]
    assert (x1, y1, x2, y2) == pytest.approx((0.0, 0.0, 1.0, 1.0), abs=1e-3)


def test_boxes_are_clamped_to_frame():
    result = boxes_to_original([(-1000, -1000, 5000, 5000)], 1.0, 0, 0, 640, 480)
    assert result[0] == (0.0, 0.0, 1.0, 1.0)


def test_iou():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    half = iou((0, 0, 10, 10), (5, 0, 15, 10))
    assert 0.3 < half < 0.4


def test_non_max_suppression_removes_duplicates():
    items = [
        (0.9, (0, 0, 10, 10), "weapon", "knife"),
        (0.8, (1, 1, 11, 11), "weapon", "knife"),   # дубль
        (0.7, (200, 200, 260, 260), "weapon", "knife"),  # другое место
    ]
    kept = non_max_suppression(items)
    assert len(kept) == 2
    assert kept[0][0] == 0.9  # оставили самого уверенного


# -------------------------------------------------------- разбор выхода YOLO
def _yolo_output_v8_layout(*, class_id: int, confidence: float, n: int = 8400):
    """Выход (1, 84, 8400) — YOLOv8/YOLO11: классы по строкам."""
    array = np.zeros((1, 84, n), dtype=np.float32)
    array[0, 0, 0] = 320.0    # cx
    array[0, 1, 0] = 240.0    # cy
    array[0, 2, 0] = 100.0    # w
    array[0, 3, 0] = 80.0     # h
    array[0, 4 + class_id, 0] = confidence
    return array


def test_parse_yolo_output_picks_only_mapped_classes():
    output = _yolo_output_v8_layout(class_id=24, confidence=0.9)   # 24 = knife
    items = parse_yolo_output(output, confidence_threshold=0.4, allowed_categories={"weapon"})
    assert len(items) == 1
    confidence, box, category, label = items[0]
    assert category == "weapon" and label == "knife"
    assert confidence == pytest.approx(0.9)
    assert box == pytest.approx((270.0, 200.0, 370.0, 280.0))  # cx,cy,w,h → x1y1x2y2


def test_parse_yolo_output_ignores_uninteresting_class():
    output = _yolo_output_v8_layout(class_id=0, confidence=0.99)   # 0 = person
    items = parse_yolo_output(output, confidence_threshold=0.4, allowed_categories={"weapon"})
    assert items == []


def test_parse_yolo_output_respects_threshold():
    output = _yolo_output_v8_layout(class_id=24, confidence=0.30)
    assert parse_yolo_output(output, confidence_threshold=0.4, allowed_categories={"weapon"}) == []


def test_parse_yolo_output_handles_transposed_layout():
    source = _yolo_output_v8_layout(class_id=24, confidence=0.85)
    transposed = np.transpose(source, (0, 2, 1))   # (1, 8400, 84)
    items = parse_yolo_output(transposed, confidence_threshold=0.4, allowed_categories={"weapon"})
    assert len(items) == 1
    assert items[0][2] == "weapon"


def test_parse_yolo_output_survives_garbage():
    assert parse_yolo_output(np.zeros((3,)), confidence_threshold=0.4, allowed_categories=set()) == []
    assert parse_yolo_output(np.zeros((1, 2, 2)), confidence_threshold=0.4, allowed_categories=set()) == []


# ------------------------------------------------------------------ вердикт
def _net(**overrides) -> SafetyNet:
    kwargs = {"platform": "twitch", **overrides}
    return SafetyNet(SafetySettings(**kwargs))


def test_evaluate_blurs_above_threshold():
    net = _net(blur_threshold=0.5)
    verdict = net.evaluate([Detection("weapon", "knife", 0.8)])
    assert verdict.blur is True
    assert verdict.category == "weapon"
    assert verdict.confidence == pytest.approx(0.8)


def test_evaluate_does_not_blur_below_threshold():
    net = _net(blur_threshold=0.5)
    assert net.evaluate([Detection("weapon", "knife", 0.3)]).blur is False


def test_evaluate_picks_the_most_confident_detection():
    net = _net(blur_threshold=0.5)
    verdict = net.evaluate([
        Detection("weapon", "knife", 0.55),
        Detection("weapon", "knife", 0.9),
    ])
    assert verdict.confidence == pytest.approx(0.9)


def test_evaluate_ignores_categories_disabled_in_profile():
    net = _net(platform="off")
    assert net.evaluate([Detection("weapon", "knife", 0.99)]).blur is False


def test_evaluate_ignores_undetectable_categories():
    """Категория есть в профиле, но модели под неё нет → блюра быть не должно."""
    net = _net(platform="twitch", blur_threshold=0.1)
    assert net.evaluate([Detection("blood", "gore", 0.99)]).blur is False
    assert net.evaluate([Detection("drugs", "drugs", 0.99)]).blur is False


def test_evaluate_blurs_on_nudity_from_classifier():
    """Нагота теперь детектируется классификатором sfw/nsfw."""
    net = _net(platform="twitch", blur_threshold=0.55)
    verdict = net.evaluate([Detection("nudity", "nsfw", 0.958)])
    assert verdict.blur is True
    assert verdict.category == "nudity"


def test_threshold_prefers_profile_when_not_set():
    assert _net(blur_threshold=0).threshold() == pytest.approx(0.55)
    assert _net(blur_threshold=0.7).threshold() == pytest.approx(0.7)


def test_settings_are_read_from_app_settings():
    settings = Settings()
    settings.safety.platform = "youtube"
    settings.safety.blur_threshold = 0.42
    safety_settings = SafetySettings.from_settings(settings)
    assert safety_settings.platform == "youtube"
    assert safety_settings.blur_threshold == pytest.approx(0.42)
    assert safety_settings.enabled is False, "по умолчанию страховка выключена"
    assert safety_settings.test_mode is True, "и в режиме проверки"


# ------------------------------------------------- жизненный цикл страховки
class StubDetector:
    """Детектор-заглушка: возвращает заранее заданные срабатывания."""

    id = "stub"

    def __init__(self, detections):
        self.detections = detections
        self.started = False
        self.stopped = False
        self.model_path = None

    def start(self):
        self.started = True
        return True, "заглушка"

    def stop(self):
        self.stopped = True

    def detect(self, frame):
        return list(self.detections)

    @property
    def available(self):
        return self.started


def test_disabled_net_checks_nothing():
    net = SafetyNet(SafetySettings(enabled=False), detector=StubDetector([]))
    verdict = net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    assert verdict.skipped
    assert net.status.frames_checked == 0


def test_test_mode_never_blurs_but_still_logs():
    """Режим проверки: пишем, что увидели, но блюр не трогаем."""
    net = SafetyNet(
        SafetySettings(enabled=True, test_mode=True, blur_threshold=0.5, hold_seconds=5),
        detector=StubDetector([Detection("weapon", "knife", 0.9)]),
    )
    net.start()
    verdict = net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    assert verdict.blur is True                 # вердикт «надо блюрить»
    assert net.is_active() is False             # но реально не блюрим
    assert net.status.blurs_triggered == 0
    assert net.recent()[0]["test_mode"] is True
    net.stop()


def test_live_mode_blurs_and_holds():
    net = SafetyNet(
        SafetySettings(enabled=True, test_mode=False, blur_threshold=0.5, hold_seconds=5),
        detector=StubDetector([Detection("weapon", "knife", 0.9)]),
    )
    net.start()
    net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    assert net.is_active() is True
    assert net.status.blurs_triggered == 1
    net.stop()
    assert net.is_active() is False


def test_missing_model_does_not_break_stream():
    """Нет весов — страховка честно говорит об этом и не мешает стриму.

    Детектор подсовываем свой: на машине разработчика веса могут лежать,
    и тогда тест проверял бы не то, что нужно.
    """
    from streamblur.safety.yolo_onnx import YoloOnnxDetector

    net = SafetyNet(
        SafetySettings(enabled=True, model_path="/нет/такой/модели.onnx"),
        detector_factory=lambda: YoloOnnxDetector("/нет/такой/модели.onnx"),
    )
    ok, message = net.start()
    assert ok is False
    assert net.status.running is False
    assert "не найдена" in net.status.error
    net.stop()


def test_detector_crash_is_reported_not_raised():
    class Broken(StubDetector):
        def detect(self, frame):
            raise RuntimeError("модель отвалилась")

    net = SafetyNet(SafetySettings(enabled=True), detector=Broken([]))
    net.start()
    verdict = net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    assert "модель отвалилась" in verdict.skipped
    assert "модель отвалилась" in net.status.error
    net.stop()


def test_yolo_detector_without_weights_reports_error():
    detector = YoloOnnxDetector("/нет/такой/папки")
    ok, message = detector.start()
    assert ok is False
    assert "safety-models" in message
    assert detector.detect(np.zeros((10, 10, 3), dtype=np.uint8)) == []


# --------------------------------------------- интеграция: движок + страховка
class _Blur:
    def __init__(self):
        self.enabled = None

    @property
    def status(self):
        from streamblur.obs.blur import BlurState, BlurStatus

        state = BlurState.ON if self.enabled else BlurState.OFF
        return BlurStatus(state=state, connected=True)

    def set_blur(self, enabled, reason=""):
        self.enabled = enabled
        return self.status


def _engine(segments, safety):
    from streamblur.clock import ClockTracker
    from streamblur.database import Database
    from streamblur.engine import BlurEngine

    clock = ClockTracker(settings=Settings())
    clock.running = True
    blur = _Blur()
    engine = BlurEngine(Database(":memory:"), clock, blur, lambda: Settings(), safety=safety)
    engine.timeline.reload(segments)
    return engine, blur, clock


def test_engine_blurs_on_safety_net_alone():
    """Таймингов нет вообще, но нейросеть увидела нож → блюр включён."""
    net = SafetyNet(
        SafetySettings(enabled=True, test_mode=False, blur_threshold=0.5, hold_seconds=5),
        detector=StubDetector([Detection("weapon", "knife", 0.9)]),
    )
    net.start()
    net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    engine, blur, clock = _engine([], net)
    clock.sync_to(100.0)
    engine.tick()
    assert blur.enabled is True
    assert "страховка" in (engine.events[-1].message or "")
    net.stop()


def test_safety_net_never_turns_blur_off():
    """Главное правило: тайминги включили блюр, нейросеть молчит → блюр остаётся."""
    net = SafetyNet(
        SafetySettings(enabled=True, test_mode=False, blur_threshold=0.5),
        detector=StubDetector([]),   # ничего не нашла
    )
    net.start()
    net.check_once(np.zeros((10, 10, 3), dtype=np.uint8))
    engine, blur, clock = _engine([{"start": 0.0, "end": 600.0, "reason": "сцена"}], net)
    clock.sync_to(100.0)
    engine.tick()
    assert blur.enabled is True, "страховка сняла блюр, поставленный таймингами"
    net.stop()


def test_disabled_safety_net_is_invisible_to_engine():
    net = SafetyNet(SafetySettings(enabled=False), detector=StubDetector([]))
    engine, blur, clock = _engine([], net)
    clock.sync_to(100.0)
    engine.tick()
    # выключенная страховка не должна вообще ни на что влиять
    assert blur.enabled is not True
    assert net.status.running is False


def test_engine_snapshot_exposes_safety():
    net = SafetyNet(SafetySettings(enabled=False))
    engine, _, _ = _engine([], net)
    data = engine.snapshot().as_dict()
    assert isinstance(data["safety"], dict)
    assert data["safety"]["enabled"] is False


def test_snapshot_reports_coverage_even_when_disabled():
    """Список «что прикрыто» виден всегда — иначе UI молчит до первого запуска."""
    net = SafetyNet(SafetySettings(enabled=False, platform="twitch"))
    data = net.snapshot()
    assert data["enabled"] is False
    assert data["covered"], "профиль Twitch что-то да прикрывает"
    assert data["not_covered"], "и честно говорит, чего не прикрывает"
    assert data["settings"]["platform"] == "twitch"
    assert data["profile"]["blur_threshold"] > 0


def test_snapshot_follows_selected_platform():
    twitch = SafetyNet(SafetySettings(enabled=False, platform="twitch")).snapshot()
    off = SafetyNet(SafetySettings(enabled=False, platform="off")).snapshot()
    assert twitch["covered"] and not off["covered"]
    assert off["profile"]["label"] == "Страховка выключена"


# ------------------------------------------------------ каталог моделей
def test_known_models_have_checksum_and_license():
    from streamblur.safety.models import KNOWN_MODELS

    assert KNOWN_MODELS, "каталог моделей пуст"
    for model in KNOWN_MODELS:
        assert len(model.sha256) == 64, f"у {model.filename} нет полной sha256"
        assert model.license, f"у {model.filename} не указана лицензия"
        assert model.url.startswith("https://"), f"у {model.filename} небезопасный URL"


def test_verify_rejects_corrupted_file(tmp_path):
    """Подменённый или побитый файл не должен приниматься за модель."""
    from streamblur.safety.models import KNOWN_MODELS, verify

    bad = tmp_path / "model.onnx"
    bad.write_bytes(b"not a model at all")
    ok, actual = verify(bad, KNOWN_MODELS[0])
    assert ok is False
    assert len(actual) == 64


def test_status_reports_installed_and_verified():
    from streamblur.safety.models import status
    from streamblur.safety.nsfw_onnx import find_nsfw_model

    data = status()
    assert data["models_dir"]
    assert data["models"]
    installed = [m for m in data["models"] if m["installed"]]
    for item in installed:
        assert item["verified"] is True, "установленная модель обязана пройти сверку"
    # если весов нет, тест не падает: он лишь проверяет форму ответа
    assert find_nsfw_model() is not None or not installed


def test_nsfw_model_is_not_picked_up_by_object_detector():
    """Детектор предметов не должен хватать модель наготы как свою.

    Реальный баг: fallback «первый *.onnx в папке» загружал ViT вместо YOLO,
    onnxruntime падал на каждом кадре, а статус врал, что оружие детектируется.
    """
    from streamblur.safety.models import KNOWN_MODELS
    from streamblur.safety.yolo_onnx import YoloOnnxDetector

    foreign = {m.filename for m in KNOWN_MODELS if m.detector != "yolo"}
    detector = YoloOnnxDetector()
    found = detector._find_model()
    if found is not None:
        assert found.name not in foreign, (
            f"детектор предметов взял чужую модель {found.name}"
        )


@pytest.mark.slow
def test_real_nsfw_model_classifies_images():
    """Прогон настоящей модели. Пропускается, если веса не скачаны.

    Проверено 2026-10-10 на int8/CPU:
        фото 1920-х, топлесс → nsfw 0.958
        кошка                → sfw  0.005
    """
    from streamblur.safety.nsfw_onnx import NsfwOnnxClassifier, find_nsfw_model

    path = find_nsfw_model()
    if path is None:
        pytest.skip("веса модели не скачаны: python -m streamblur safety-models --download")

    classifier = NsfwOnnxClassifier()
    ok, message = classifier.start()
    assert ok, message

    # синтетический «нейтральный» кадр: однородный серый — наготы там нет
    neutral = np.full((256, 256, 3), 128, dtype=np.uint8)
    label, probability = classifier.classify(neutral)
    assert label in ("sfw", "nsfw")
    assert 0.0 <= probability <= 1.0
    assert label == "sfw", "однородный серый кадр признан наготой — предобработка сломана"
    classifier.stop()


def test_composite_starts_only_needed_models():
    """Модель выключенной категории не должна грузиться вообще."""
    from streamblur.safety.composite import build_detector

    settings = SafetySettings(platform="twitch", model_path="/нет.onnx",
                              nsfw_model_path="/нет-тоже.onnx")
    detector = build_detector(settings, {"nudity"})
    categories = [category for category, _ in detector.parts]
    assert categories == ["nudity"], "для профиля без оружия детектор предметов не нужен"

    detector = build_detector(settings, {"weapon"})
    assert [c for c, _ in detector.parts] == ["weapon"]


def test_safety_check_images_cli(tmp_path, capsys):
    """`safety-check --image` — главный инструмент личной проверки.

    Тест не зависит от весов: детектор подсовываем свой.
    """
    import argparse

    from streamblur.__main__ import _safety_check_images

    good = tmp_path / "a.jpg"
    good.write_bytes(_PNG_1PX)
    missing = tmp_path / "нет.jpg"

    class StubDetector:
        id = "stub"
        def start(self): return True, "ok"
        def stop(self): pass
        def detect(self, frame):
            return [Detection("nudity", "nsfw", 0.9)]

    net = SafetyNet(
        SafetySettings(enabled=True, platform="twitch", test_mode=True),
        detector=StubDetector(),
    )
    code = _safety_check_images(net, [str(good), str(missing)])
    out = capsys.readouterr().out
    assert code == 1, "пропавший файл должен давать ненулевой код возврата"
    assert "БЛЮР" in out and "0.900" in out
    assert "НЕТ ТАКОГО ФАЙЛА" in out
    assert net.status.frames_checked == 1, "пропавший файл не считается кадром"
