"""AC 1.3: usgs_levels returns latest height and time per gauge, or "no reading"."""

import responses

import config
from data import sources
from tests.conftest import load_fixture, usgs_callback

EXPECTED = {  # latest values in the trimmed real fixture (Oct 24, 2026)
    "01403200": (4.06, "2026-10-24T20:30:00.000-04:00"),
    "01403150": (2.03, "2026-10-24T21:10:00.000-04:00"),
    "01400500": (4.21, "2026-10-24T20:15:00.000-04:00"),
    "01400000": (2.77, "2026-10-24T21:00:00.000-04:00"),
    "01403060": (16.97, "2026-10-24T20:15:00.000-04:00"),
}


def test_ac_1_3_gauge_list_matches_stack():
    assert list(config.GAUGES) == ["01403200", "01403190", "01403150", "01400500", "01400000", "01403060"]


def test_ac_1_3_latest_height_and_time_per_gauge(rsps):
    rsps.add_callback(responses.GET, config.USGS_IV_URL, callback=usgs_callback())
    levels = sources.usgs_levels()
    assert [g.site for g in levels] == list(config.GAUGES)
    by_site = {g.site: g for g in levels}
    for site, (height, when) in EXPECTED.items():
        assert by_site[site].height_ft == height
        assert by_site[site].time == when
        assert by_site[site].display == f"{height:.2f} ft"
    # 01403190 is absent from the real iv response: no reading, others still shown.
    assert by_site["01403190"].height_ft is None
    assert by_site["01403190"].display == "no reading"
    assert by_site["01403190"].name == config.GAUGES["01403190"]
    # One cached request per site, each with the gauge-height parameter.
    assert len(rsps.calls) == 6
    assert all("parameterCd=00065" in c.request.url for c in rsps.calls)


def test_ac_1_3_empty_series_shows_no_reading_others_still_shown(rsps):
    rsps.add_callback(responses.GET, config.USGS_IV_URL, callback=usgs_callback(empty_sites=("01403200",)))
    by_site = {g.site: g for g in sources.usgs_levels()}
    assert by_site["01403200"].display == "no reading"
    assert by_site["01403200"].name == "Middle Brook at Bound Brook NJ"
    assert by_site["01403060"].height_ft == 16.97


def test_ac_1_3_no_data_sentinel_is_no_reading(rsps):
    iv = load_fixture("usgs_iv.json")
    for ts in iv["value"]["timeSeries"]:
        ts["values"][0]["value"] = [{"value": "-999999", "dateTime": "2026-10-24T20:00:00.000-04:00"}]
    rsps.add_callback(responses.GET, config.USGS_IV_URL, callback=usgs_callback(iv=iv))
    assert all(g.display == "no reading" for g in sources.usgs_levels())


def test_ac_1_3_network_down_no_cache_is_no_reading_without_crash(rsps):
    rsps.get(config.USGS_IV_URL, status=503)
    levels = sources.usgs_levels()
    assert len(levels) == 6 and all(g.display == "no reading" for g in levels)


from datetime import datetime, timedelta, timezone  # noqa: E402

FIXTURE_NOW = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)  # ~1 h after the fixture readings


def test_ac_1_3_later_series_with_valid_value_is_used():
    data = {"value": {"timeSeries": [
        {"sourceInfo": {"siteName": "A", "siteCode": [{"value": "01403200"}]},
         "values": [{"value": [{"value": "-999999", "dateTime": "2026-10-24T20:00:00-04:00"}]}]},
        {"sourceInfo": {"siteName": "A", "siteCode": [{"value": "01403200"}]},
         "values": [{"value": []}, {"value": [{"value": "5.5", "dateTime": "2026-10-24T20:15:00-04:00"}]}]},
    ]}}
    assert sources._latest_reading(data, "01403200") == ("A", 5.5, "2026-10-24T20:15:00-04:00")


def test_ac_1_3_status_live_cached_old_unavailable_no_reading(rsps):
    rsps.add_callback(responses.GET, config.USGS_IV_URL, callback=usgs_callback())
    sources.usgs_levels()  # warm cache
    live = {g.site: g.status(now=FIXTURE_NOW) for g in sources.usgs_levels()}
    assert live["01403200"] == "live"
    assert live["01403190"] == "no reading"

    rsps.reset()
    rsps.get(config.USGS_IV_URL, status=503)
    cached = {g.site: g.status(now=FIXTURE_NOW) for g in sources.usgs_levels()}
    assert cached["01403200"] == "cached"
    later = FIXTURE_NOW + timedelta(hours=3)
    assert {g.status(now=later) for g in sources.usgs_levels() if g.has_reading} == {"old reading"}


def test_ac_1_3_fetch_failure_without_cache_is_unavailable_not_no_reading(rsps):
    rsps.get(config.USGS_IV_URL, status=503)
    levels = sources.usgs_levels()
    assert all(g.fetch_failed and g.status() == "unavailable" for g in levels)
