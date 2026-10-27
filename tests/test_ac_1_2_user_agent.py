"""AC 1.2: NWS fetchers call point, hourly and alerts endpoints, each with NWS_USER_AGENT."""

import config
from data import sources
from data.location import resolve_zip
from tests.conftest import TEST_UA, mock_live


def test_ac_1_2_every_nws_request_carries_user_agent(rsps):
    loc = resolve_zip("08807")
    mock_live(rsps, loc)
    point = sources.nws_point(loc)
    sources.nws_hourly(loc, point)
    sources.nws_gridpoint_qpf(loc, point)
    sources.nws_alerts(loc.lat, loc.lon, loc.zip)

    nws_calls = [c for c in rsps.calls if c.request.url.startswith(config.NWS_BASE)]
    urls = [c.request.url for c in nws_calls]
    assert any("/points/40.5904,-74.6267" in u for u in urls)
    assert any(u.endswith("/gridpoints/PHI/64,110/forecast/hourly") for u in urls)
    assert any(u.endswith("/gridpoints/PHI/64,110") for u in urls)
    assert any("/alerts/active?point=40.5904,-74.6267" in u for u in urls)
    assert len(nws_calls) == 4
    for c in nws_calls:
        assert c.request.headers["User-Agent"] == TEST_UA


def test_ac_1_2_hourly_without_point_fetches_point_first(rsps):
    loc = resolve_zip("08807")
    mock_live(rsps, loc)
    hourly = sources.nws_hourly(loc)
    assert hourly is not None and not hourly.stale
    assert all(c.request.headers["User-Agent"] == TEST_UA for c in rsps.calls)


def test_ac_1_2_user_agent_prefers_streamlit_secrets(monkeypatch):
    monkeypatch.setattr(config, "_from_st_secrets", lambda name: "FromSecrets (a@b.c)")
    assert config.nws_user_agent() == "FromSecrets (a@b.c)"


def test_ac_1_2_user_agent_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", "FromEnv (x@y.z)")
    assert config.nws_user_agent() == "FromEnv (x@y.z)"


def test_ac_1_2_qpf_series_is_parsed(rsps):
    loc = resolve_zip("08807")
    mock_live(rsps, loc)
    grid = sources.nws_gridpoint_qpf(loc)
    values = sources.qpf_values(grid)
    assert values and "validTime" in values[0] and "value" in values[0]
    assert grid.data["properties"]["quantitativePrecipitation"]["uom"] == "wmoUnit:mm"


def test_ac_1_2_user_agent_unset_everywhere_uses_fallback(monkeypatch, caplog):
    monkeypatch.delenv("NWS_USER_AGENT", raising=False)
    with caplog.at_level("WARNING"):
        ua = config.nws_user_agent()
    assert ua == config._FALLBACK_USER_AGENT and ua.startswith("FloodSense")
    assert "NWS_USER_AGENT is not set" in caplog.text


def test_ac_1_2_real_st_secrets_lookup_reads_mapping(monkeypatch):
    import streamlit

    from tests.conftest import REAL_FROM_ST_SECRETS

    monkeypatch.setattr(streamlit, "secrets", {"NWS_USER_AGENT": "Mapped (m@n.o)"})
    assert REAL_FROM_ST_SECRETS("NWS_USER_AGENT") == "Mapped (m@n.o)"
    assert REAL_FROM_ST_SECRETS("MISSING") is None
    monkeypatch.setattr(config, "_from_st_secrets", REAL_FROM_ST_SECRETS)
    monkeypatch.delenv("NWS_USER_AGENT", raising=False)
    assert config.nws_user_agent() == "Mapped (m@n.o)"
