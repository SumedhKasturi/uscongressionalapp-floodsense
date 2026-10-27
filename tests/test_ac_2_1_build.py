"""AC 2.1: a first build for 08807 has all 8 sections, is upserted as today's version and copied locally."""

import json
import threading
from urllib.parse import parse_qs, urlparse

import requests
import responses

import config
from data import places
from data.location import resolve_zip
from tests.conftest import (
    PLACES_NOW,
    PLACES_TODAY,
    SUPABASE_TEST_KEY,
    SUPABASE_TEST_URL,
    TEST_UA,
    is_source_call,
    mock_live,
    mock_places_sources,
)


def test_ac_2_1_first_build_has_all_sections_saved_and_copied(rsps, supabase, cache_dir):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    record = places.get_places(loc, now=PLACES_NOW)

    for section in places.SECTIONS:
        assert section in record, section
    assert record["zip"] == "08807" and record["stale"] is False
    assert record["version"] == PLACES_TODAY
    assert all(record["status"][s] == "ok" for s in places.SECTIONS)
    assert record["gauges"] and record["flood_spots"] and record["waterways"]
    assert record["history"]["warnings"]["count"] > 0
    assert record["history"]["disasters"]["count"] == 30
    assert record["history"]["flood_claims"]["count"] == 258
    assert record["shelters"] == []

    # Upserted once to Supabase as today's version, with the anon key headers.
    assert supabase.versions("08807") == [PLACES_TODAY]
    post = [r for r in supabase.requests if r.method == "POST"]
    assert len(post) == 1
    assert post[0].headers["apikey"] == SUPABASE_TEST_KEY
    assert post[0].headers["Authorization"] == f"Bearer {SUPABASE_TEST_KEY}"
    row = json.loads(post[0].body)
    assert set(row) == {"zip", "version", "built_at", "status", "data"}
    assert row["data"]["zip"] == "08807" and row["status"] == record["status"]

    # Local copy written atomically to data/cache/places/08807.json.
    local = cache_dir / "places" / "08807.json"
    assert json.loads(local.read_text())["version"] == record["version"]
    assert not [p for p in local.parent.iterdir() if p.suffix == ".tmp"]


def test_ac_2_1_area_section(rsps, supabase):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    area = places.build_places(loc)["area"]
    assert area == {
        "zip": "08807", "place": "Bridgewater, NJ", "lat": 40.5904, "lon": -74.6267,
        "county_ugc": "NJC035", "zone": "NJZ010", "wfo": "PHI",
    }


def test_ac_2_1_request_parameters(rsps):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    places.build_places(loc)
    by_path = {urlparse(c.request.url).path: parse_qs(urlparse(c.request.url).query) for c in rsps.calls}
    lsr = by_path["/api/1/nws/lsrs_by_point.json"]
    assert lsr["radius_miles"] == ["6"] and lsr["begints"] == ["2005-01-01T00:00Z"]
    assert lsr["lat"] == ["40.5904"] and lsr["lon"] == ["-74.6267"]
    vtec = by_path["/json/vtec_events_byugc.py"]
    assert vtec["ugc"] == ["NJC035"] and vtec["sdate"] == ["2005-01-01"]
    dis = by_path["/api/open/v2/DisasterDeclarationsSummaries"]
    assert dis["$filter"] == ["fipsStateCode eq '34' and fipsCountyCode eq '035'"]
    nfip = by_path["/api/open/v3/NfipClaims"]
    assert nfip["$filter"] == ["reportedZipCode eq '08807'"]
    shel = by_path["/arcgis/rest/services/NSS/OpenShelters/MapServer/0/query"]
    assert shel["distance"] == ["25"] and shel["outSR"] == ["4326"] and shel["geometry"] == ["-74.6267,40.5904"]
    assert "/offices/PHI" in by_path and "/nwps/v1/gauges/BDBN4" in by_path


def test_ac_2_1_every_source_call_sends_user_agent_and_timeout(rsps, monkeypatch):
    seen = []
    real = requests.Session.request

    def spy(self, method, url, **kwargs):
        seen.append((url, kwargs.get("timeout")))
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    places.build_places(loc)
    source_calls = [c for c in rsps.calls if is_source_call(c)]
    assert len(source_calls) >= 14  # point, bbox, 7 gauges, lsr, vtec, disasters, nfip, shelters, office
    assert all(c.request.headers["User-Agent"] == TEST_UA for c in source_calls)
    assert seen and all(t == config.HTTP_TIMEOUT for _, t in seen)


def test_ac_2_1_sources_are_fetched_in_parallel(rsps):
    """The five independent sources must all be in flight at once to get past a barrier.

    Fetched one after another, the first would wait at the barrier alone and fail.
    """
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    registered = list(rsps.registered())
    rsps.reset()
    barrier = threading.Barrier(5, timeout=5)
    gated = ("/points/", "/nwps/v1/gauges?", "lsrs_by_point", "OpenShelters", "NfipClaims")
    for m in registered:
        def respond(request, body=m.body):
            url = request.url if "?" in request.url else request.url + "?"
            if any(g in url for g in gated):
                barrier.wait()  # raises BrokenBarrierError if the others never arrive
            return (200, {}, body)

        rsps.add_callback(responses.GET, m.url, callback=respond, content_type="application/json")
    record = places.build_places(loc)
    assert not barrier.broken
    for section in ("area", "gauges", "flood_spots", "shelters", "history.flood_claims"):
        assert record["status"][section] == "ok", section


def test_ac_2_1_cli_builds_and_prints_summary(rsps, capsys, cache_dir):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    assert places.main(["08807"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["zip"] == "08807" and out["counts"]["gauges"] == 7
    assert (cache_dir / "places" / "08807.json").exists()


def test_ac_2_1_supabase_rest_paths(rsps, supabase):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    places.get_places(loc, now=PLACES_NOW)
    get = [r for r in supabase.requests if r.method == "GET"][0]
    assert get.url.startswith(f"{SUPABASE_TEST_URL}/rest/v1/places_versions?")
    q = parse_qs(urlparse(get.url).query)
    assert q["zip"] == ["eq.08807"] and q["order"] == ["version.desc"] and q["limit"] == ["1"]


def test_ac_2_1_startup_prebuild_runs_in_background(rsps, supabase, monkeypatch, cache_dir):
    monkeypatch.setenv("FLOODSENSE_PREBUILD", "1")
    mock_places_sources(rsps, resolve_zip("08807"))
    thread = places.start_prebuild("08807")
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert len(supabase.versions("08807")) == 1
    assert (cache_dir / "places" / "08807.json").exists()


def test_ac_2_1_startup_prebuild_failure_never_raises(monkeypatch):
    monkeypatch.setenv("FLOODSENSE_PREBUILD", "1")
    monkeypatch.setattr(places, "get_places", lambda loc: (_ for _ in ()).throw(RuntimeError("boom")))
    thread = places.start_prebuild("08807")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_ac_2_1_app_prebuilds_default_zip_once(rsps, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    calls = []
    monkeypatch.setenv("FLOODSENSE_PREBUILD", "1")
    monkeypatch.setattr(places, "start_prebuild", lambda zip_code: calls.append(zip_code))
    st.cache_resource.clear()  # an earlier app test may already have run the prebuild
    mock_live(rsps, resolve_zip("08807"))
    at = AppTest.from_file("../app.py", default_timeout=20)
    at.run()
    at.run()
    assert not at.exception, at.exception
    assert calls == ["08807"]
    st.cache_resource.clear()


def test_ac_2_1_app_prebuild_failure_does_not_break_page(rsps, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    def boom(zip_code):
        raise RuntimeError("prebuild exploded")

    monkeypatch.setattr(places, "start_prebuild", boom)
    st.cache_resource.clear()
    mock_live(rsps, resolve_zip("08807"))
    at = AppTest.from_file("../app.py", default_timeout=20)
    at.run()
    assert not at.exception, at.exception
    assert at.dataframe  # the forecast still renders
    st.cache_resource.clear()
