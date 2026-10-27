"""AC 1.4: successful responses are cached as JSON with fetched_at; failures return stale cache."""

import json
from datetime import datetime, timezone

import pytest
import requests

import config
from data import cache
from data.cache import get_json

URL = "https://api.weather.gov/points/40.5904,-74.6267"


def test_ac_1_4_success_writes_cache_envelope(rsps, cache_dir):
    rsps.get(URL, json={"properties": {"x": 1}})
    result = get_json("nws_point_08807", URL, {"User-Agent": "t"})
    assert result.stale is False and result.data == {"properties": {"x": 1}}

    path = cache_dir / "nws_point_08807.json"
    env = json.loads(path.read_text())
    assert set(env) == {"fetched_at", "source_url", "data"}
    assert env["source_url"] == URL and env["data"] == {"properties": {"x": 1}}
    fetched = datetime.fromisoformat(env["fetched_at"])
    assert fetched.utcoffset() == timezone.utc.utcoffset(None)
    assert abs((datetime.now(timezone.utc) - fetched).total_seconds()) < 60
    assert [p.name for p in cache_dir.iterdir()] == ["nws_point_08807.json"]  # no temp files left


@pytest.mark.parametrize(
    "failure",
    [
        {"body": requests.exceptions.ConnectionError("down")},
        {"body": requests.exceptions.Timeout("slow")},
        {"status": 500},
        {"status": 503},
        {"body": "not json", "status": 200},
    ],
    ids=["connection", "timeout", "500", "503", "bad-json"],
)
def test_ac_1_4_failure_returns_cached_copy_marked_stale(rsps, failure, caplog):
    rsps.get(URL, json={"v": "good"})
    first = get_json("k", URL)
    rsps.replace("GET", URL, **failure)
    with caplog.at_level("WARNING"):
        second = get_json("k", URL)
    assert second.stale is True
    assert second.data == {"v": "good"}
    assert second.fetched_at == first.fetched_at
    assert "using cache" in caplog.text


def test_ac_1_4_failure_without_cache_returns_none(rsps):
    rsps.get(URL, body=requests.exceptions.ConnectionError("down"))
    assert get_json("missing", URL) is None


def test_ac_1_4_every_call_uses_10s_timeout(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return Resp()

    monkeypatch.setattr(cache.requests, "get", fake_get)
    get_json("t", URL)
    assert config.HTTP_TIMEOUT == 10 and seen["timeout"] == 10


def test_ac_1_4_new_success_overwrites_cache(rsps, cache_dir):
    rsps.get(URL, json={"v": 1})
    get_json("k", URL)
    rsps.replace("GET", URL, json={"v": 2})
    get_json("k", URL)
    assert json.loads((cache_dir / "k.json").read_text())["data"] == {"v": 2}


def test_ac_1_4_cache_dir_default_is_data_cache(monkeypatch):
    import importlib.util

    monkeypatch.delenv("FLOODSENSE_CACHE_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("_cfg_copy", config.APP_DIR / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.CACHE_DIR == config.APP_DIR / "data" / "cache"


@pytest.mark.parametrize("corrupt", ["{not json", "{}"], ids=["not-json", "empty-object"])
def test_ac_1_4_corrupt_cache_and_network_down_returns_none(rsps, cache_dir, corrupt):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "k.json").write_text(corrupt)
    rsps.get(URL, body=requests.exceptions.ConnectionError("down"))
    assert get_json("k", URL) is None


def test_ac_1_4_cache_write_error_still_returns_fresh_result(rsps, monkeypatch):
    def boom(path, envelope):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "_write_atomic", boom)
    rsps.get(URL, json={"v": 1})
    result = get_json("k", URL)
    assert result is not None and result.stale is False and result.data == {"v": 1}
