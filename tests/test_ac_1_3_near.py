"""AC 1.3 (user change, Oct 25): gauges are the NOAA NWPS gauges nearest the entered ZIP."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from data import sources
from data.location import resolve_zip
from tests.conftest import NWPS_GAUGES_URL, load_fixture, near_callback


def box_width(url):
    q = parse_qs(urlparse(url).query)
    return round(float(q["bbox.xmax"][0]) - float(q["bbox.xmin"][0]), 4)


def test_ac_1_3_near_08807_nearest_first_with_distance_and_n_limit(rsps):
    loc = resolve_zip("08807")
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback("08807", shift=False))
    levels = sources.gauges_near(loc)
    # Seven gauges in the fixture; the farthest (SPTN4, 18.6 mi) is cut by n=6.
    assert [g.site for g in levels] == ["SMVN4", "MRTN4", "MNVN4", "BDBN4", "BDKN4", "MILN4"]
    assert [round(g.distance_mi, 1) for g in levels] == [1.2, 2.4, 3.3, 4.6, 4.9, 8.0]
    first = levels[0]
    assert first.name == "Peters Brook at Somerville"
    assert first.height_ft == 1.79 and first.unit == "ft" and first.time == "2026-10-25T11:00:00Z"
    # Exactly one request with the documented query.
    assert len(rsps.calls) == 1
    q = parse_qs(urlparse(rsps.calls[0].request.url).query)
    assert q == {
        "bbox.xmin": ["-74.8767"], "bbox.ymin": ["40.3404"],
        "bbox.xmax": ["-74.3767"], "bbox.ymax": ["40.8404"], "srid": ["EPSG_4326"],
    }


def test_ac_1_3_near_n_limit(rsps):
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback("08807", shift=False))
    assert [g.site for g in sources.gauges_near(resolve_zip("08807"), n=3)] == ["SMVN4", "MRTN4", "MNVN4"]


def test_ac_1_3_near_minus_999_is_no_reading_and_others_still_shown(rsps):
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback("10001", shift=False))
    levels = sources.gauges_near(resolve_zip("10001"))
    by_site = {g.site: g for g in levels}
    assert levels[0].site == "BATN6"
    assert by_site["BGNN6"].display == "no reading" and by_site["BGNN6"].time is None
    assert by_site["HAMN6"].display == "no reading"
    assert by_site["VRON4"].height_ft == -0.53  # negative stage is a valid value
    assert by_site["BATN6"].display == "5.81 ft"


def test_ac_1_3_near_flood_category(rsps):
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback("08807", shift=False))
    by_site = {g.site: g for g in sources.gauges_near(resolve_zip("08807"), n=10)}
    assert by_site["MNVN4"].flood_category == "no_flooding"
    assert by_site["MILN4"].flood_category == "minor"
    assert by_site["SMVN4"].flood_category is None  # "not_defined" is not a flood category
    assert by_site["BDKN4"].flood_category is None  # no reading


def test_ac_1_3_near_non_ft_unit_is_shown(rsps):
    body = load_fixture("nwps_near_08807.json")
    body["gauges"][0]["status"]["observed"]["primaryUnit"] = "m"
    rsps.get(NWPS_GAUGES_URL, json=body)
    by_site = {g.site: g for g in sources.gauges_near(resolve_zip("08807"))}
    assert by_site[body["gauges"][0]["lid"]].display.endswith(" m")


def test_ac_1_3_haversine_distance():
    assert sources.haversine_mi(40.5904, -74.6267, 40.575, -74.6158333) == pytest.approx(1.2, abs=0.05)
    assert sources.haversine_mi(40.0, -74.0, 40.0, -74.0) == 0


def test_ac_1_3_near_widens_once_when_first_box_is_empty(rsps, cache_dir):
    wide_body = load_fixture("nwps_near_08807.json")
    seen = []

    def cb(request):
        seen.append(box_width(request.url))
        body = {"gauges": []} if len(seen) == 1 else wide_body
        return (200, {}, json.dumps(body))

    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=cb)
    levels = sources.gauges_near(resolve_zip("08807"))
    assert seen == [0.5, 1.0]  # +/-0.25 then +/-0.5 degrees
    assert len(levels) == 6
    assert {p.name for p in cache_dir.iterdir()} == {"nwps_near_08807.json", "nwps_near_08807_wide.json"}


def test_ac_1_3_near_none_found_returns_empty_list(rsps):
    rsps.get(NWPS_GAUGES_URL, json={"gauges": []})
    assert sources.gauges_near(resolve_zip("08807")) == []
    assert len(rsps.calls) == 2  # widened exactly once


def test_ac_1_3_near_fetch_failed_without_cache_returns_none(rsps):
    rsps.get(NWPS_GAUGES_URL, body=requests.exceptions.ConnectionError("down"))
    assert sources.gauges_near(resolve_zip("08807")) is None


def test_ac_1_3_near_stale_cache(rsps):
    loc = resolve_zip("08807")
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback("08807", shift=False))
    fresh = sources.gauges_near(loc)
    rsps.reset()
    rsps.get(NWPS_GAUGES_URL, status=503)
    stale = sources.gauges_near(loc)
    assert [g.site for g in stale] == [g.site for g in fresh]
    assert all(g.stale and g.fetched_at == fresh[0].fetched_at for g in stale)
    assert {g.status() for g in stale} <= {"cached", "old reading", "no reading"}
