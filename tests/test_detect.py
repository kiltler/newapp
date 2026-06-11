"""Тесты автоопределения типа API."""
import httpx

from app.drivers import detect_api_type
from app.models import ApiType
from mock.server import make_mock_app
from mock.state import MockNVR
from tests.conftest import make_http


async def _detect(profile: str):
    nvr = MockNVR.default(channels=2)
    app = make_mock_app(nvr, profile)
    http = make_http(app, nvr.username, nvr.password)
    return await detect_api_type(
        "testserver", 80, nvr.username, nvr.password, client=http
    )


async def test_detect_hikvision():
    res = await _detect("hikvision")
    assert res.api_type == ApiType.HIKVISION
    assert res.info and res.info.model


async def test_detect_dahua():
    res = await _detect("dahua")
    assert res.api_type == ApiType.DAHUA
    assert res.info and res.info.serial


async def test_detect_unknown():
    # пустое приложение — ни ISAPI, ни CGI
    empty = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_mock_app(MockNVR.default(), profile="none")),
        base_url="http://testserver",
    )
    res = await detect_api_type("testserver", 80, "admin", "x", client=empty)
    assert res.api_type == ApiType.UNKNOWN
