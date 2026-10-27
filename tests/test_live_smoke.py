"""Live smoke tests against the real APIs. Skipped by default; run with -m live."""

import pytest

pytestmark = pytest.mark.live


@pytest.fixture
def real_pgeocode(offline_pgeocode, monkeypatch):
    import os
    import pgeocode

    from data import location

    monkeypatch.setattr(pgeocode, "STORAGE_DIR", os.environ.get(
        "PGEOCODE_DATA_DIR", os.path.join(os.path.expanduser("~"), ".cache", "pgeocode")))
    location._nominatim.cache_clear()
    yield
    location._nominatim.cache_clear()


@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_live_nws_and_usgs(zip_code, real_pgeocode, monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", "FloodSense live test (github.com/uscongressionalapp-floodsense)")
    from data import sources
    from data.location import resolve_zip

    loc = resolve_zip(zip_code)
    point = sources.nws_point(loc)
    assert point and not point.stale
    hourly = sources.nws_hourly(loc, point)
    assert len(sources.hourly_periods(hourly)) == 12
    assert sources.nws_alerts(loc.lat, loc.lon, loc.zip) is not None
    assert sources.qpf_values(sources.nws_gridpoint_qpf(loc, point))
    levels = sources.usgs_levels()
    assert len(levels) == 6 and any(g.has_reading for g in levels)


@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_live_gauges_near(zip_code, real_pgeocode):
    import time

    from data import sources
    from data.location import resolve_zip

    start = time.monotonic()
    levels = sources.gauges_near(resolve_zip(zip_code))
    assert time.monotonic() - start < 10
    assert levels and len(levels) == 6
    dists = [g.distance_mi for g in levels]
    assert dists == sorted(dists) and dists[0] < 35
    assert any(g.has_reading and not g.stale for g in levels)
