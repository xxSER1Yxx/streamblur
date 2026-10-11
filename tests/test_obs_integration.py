"""Интеграционный тест: реальный obs-websocket протокол между клиентом и мок-OBS.

Здесь проверяется самая важная для стримера вещь — что команда «включить размытие»
действительно доходит до OBS и меняет состояние фильтра.
"""
from __future__ import annotations

import pytest

from streamblur.config import ObsSettings
from streamblur.mock_obs_server import MockOBS
from streamblur.obs.blur import BlurController, BlurState
from streamblur.obs.client import OBSClient, OBSClientError, make_auth


@pytest.fixture()
def server():
    mock = MockOBS("127.0.0.1", 0)
    mock.start()
    yield mock
    mock.stop()


def _client(server: MockOBS, password: str = "") -> OBSClient:
    return OBSClient("127.0.0.1", server.port, password, timeout=5.0)


def test_connect_and_version(server: MockOBS):
    client = _client(server)
    info = client.connect()
    assert info["negotiatedRpcVersion"] == 1
    version = client.version()
    assert version["obsVersion"].endswith("mock")
    client.close()


def test_password_required_and_verified():
    server = MockOBS("127.0.0.1", 0, password="secret")
    server.start()
    try:
        with pytest.raises(OBSClientError):
            _client(server).connect()  # без пароля
        good = _client(server, "secret")
        good.connect()
        good.close()
        bad = _client(server, "неверный")
        with pytest.raises(OBSClientError):
            bad.connect()
    finally:
        server.stop()


def test_make_auth_matches_server():
    salt, challenge = "salt123", "chal456"
    token = make_auth("pw", salt, challenge)
    import base64
    import hashlib

    secret = base64.b64encode(hashlib.sha256(b"pw" + salt.encode()).digest()).decode()
    expected = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()
    assert token == expected


def test_scene_and_source_discovery(server: MockOBS):
    client = _client(server)
    client.connect()
    assert "Main" in client.scenes()
    assert client.current_scene() == "Main"
    assert "Game Capture" in client.inputs_of_scene("Main")
    client.close()


def test_blur_controller_creates_filter_and_toggles_it(server: MockOBS):
    settings = ObsSettings(host="127.0.0.1", port=server.port, backend="streamfx",
                           source="Game Capture", scene="Main")
    controller = BlurController(settings, client=_client(server))
    status = controller.connect()
    assert status.connected is True, status.last_error
    assert status.state == BlurState.OFF
    # фильтр создан и выключен
    created = server.filters["Game Capture"]["StreamBlur Blur"]
    assert created["filterEnabled"] is False
    assert created["filterSettings"]["Blur.Size"] == pytest.approx(64.0)

    controller.set_blur(True, reason="тест")
    assert controller.status.state == BlurState.ON
    assert server.filters["Game Capture"]["StreamBlur Blur"]["filterEnabled"] is True
    assert server.blur_log[-1]["enabled"] is True

    controller.set_blur(False, reason="тест")
    assert controller.status.state == BlurState.OFF
    assert server.filters["Game Capture"]["StreamBlur Blur"]["filterEnabled"] is False
    controller.disconnect()


def test_blur_controller_source_toggle_backend(server: MockOBS):
    settings = ObsSettings(host="127.0.0.1", port=server.port, backend="source-toggle",
                           source="Game Capture", scene="Main", toggle_source="BRB Screen")
    controller = BlurController(settings, client=_client(server))
    assert controller.connect().connected
    controller.set_blur(True)
    assert server.scene_items_enabled[("Main", "BRB Screen")] is True
    controller.set_blur(False)
    assert server.scene_items_enabled[("Main", "BRB Screen")] is False
    controller.disconnect()


def test_blur_controller_autodetects_source(server: MockOBS):
    settings = ObsSettings(host="127.0.0.1", port=server.port, backend="streamfx", source="")
    controller = BlurController(settings, client=_client(server))
    controller.auto_reconnect = False
    assert controller.connect().connected
    assert controller.settings.source == "Game Capture"
    controller.disconnect()


def test_diagnose_reports_sources_and_filters(server: MockOBS):
    settings = ObsSettings(host="127.0.0.1", port=server.port, source="Game Capture")
    controller = BlurController(settings, client=_client(server))
    report = controller.diagnose()
    assert report["ok"] is True
    assert "Game Capture" in report["sources"]
    controller.disconnect()


def test_diagnose_failure_is_reported_not_raised():
    settings = ObsSettings(host="127.0.0.1", port=1, source="X")
    controller = BlurController(settings, client=OBSClient("127.0.0.1", 1, timeout=1.0))
    controller.auto_reconnect = False
    report = controller.diagnose()
    assert report["ok"] is False
    assert report["error"]


def test_missing_filter_is_recreated_instead_of_failing(server: MockOBS):
    """Если пользователь удалил фильтр — StreamBlur обязан создать его заново.

    Это безопаснее, чем упасть в ERROR: стрим не должен остаться без защиты.
    """
    settings = ObsSettings(host="127.0.0.1", port=server.port, source="Game Capture",
                           filter_name="StreamBlur Blur")
    controller = BlurController(settings, client=_client(server))
    controller.auto_reconnect = False
    controller.connect()
    # имитируем ситуацию «фильтр пропал»
    server.filters["Game Capture"].clear()
    state = controller.set_blur(True)
    assert state.state is BlurState.ON
    assert server.filters["Game Capture"]["StreamBlur Blur"]["filterEnabled"] is True
    controller.disconnect()


def test_mock_backend_needs_no_server():
    settings = ObsSettings(backend="mock", source="X")
    controller = BlurController(settings)
    assert controller.connect().connected
    controller.set_blur(True)
    assert controller.status.state == BlurState.ON
    controller.disconnect()
