"""AC 2.4 and the I/O matrix: freshness, Supabase and source-failure behavior of get_places."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests

from data import places, supabase_rest
from data.location import resolve_zip
from tests.conftest import (
    PLACES_NOW,
    PLACES_SOURCES,
    PLACES_TODAY,
    SUPABASE_TEST_URL,
    is_source_call,
    mock_places_sources,
)

NOW = PLACES_NOW
LATER = NOW + timedelta(minutes=61)  # past the retry window, same UTC day
TODAY = PLACES_TODAY


def days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).date().isoformat()


def stored(loc, version: str, marker: str = "stored") -> dict:
    rec = places.fallback_record(loc)
    rec.update(version=version, built_at=f"{version}T12:00:00+00:00", all_sources_failed=False)
    rec["status"] = {k: "ok" for k in rec["status"]}  # a complete stored version
    rec["area"]["place"] = marker
    return rec


def source_calls(rsps):
    return [c for c in rsps.calls if is_source_call(c)]


@pytest.fixture
def loc():
    return resolve_zip("08807")


# --- freshness ---------------------------------------------------------------

@pytest.mark.parametrize("age", [0, 6])
def test_ac_2_4_fresh_version_returned_with_zero_source_calls(rsps, supabase, loc, age):
    supabase.add(stored(loc, days_ago(age)))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["version"] == days_ago(age) and rec["area"]["place"] == "stored"
    assert rec["stale"] is False
    assert source_calls(rsps) == []
    assert [r.method for r in supabase.requests] == ["GET"]  # nothing saved


def test_ac_2_4_fresh_supabase_version_refreshes_local_copy(rsps, supabase, loc, cache_dir):
    supabase.add(stored(loc, days_ago(2)))
    places.get_places(loc, now=NOW)
    assert json.loads((cache_dir / "places" / "08807.json").read_text())["version"] == days_ago(2)


@pytest.mark.parametrize("age", [7, 30])
def test_ac_2_4_old_version_rebuilt_and_old_kept(rsps, supabase, loc, age):
    supabase.add(stored(loc, days_ago(age)))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY and rec["area"]["place"] == "Bridgewater, NJ"
    assert source_calls(rsps)
    assert supabase.versions("08807") == [days_ago(age), TODAY]  # old one stays until cleanup


def test_ac_2_4_same_day_forced_rebuild_replaces_todays_row(rsps, supabase, loc):
    supabase.add(stored(loc, TODAY, marker="morning build"))
    mock_places_sources(rsps, loc)
    assert places.get_places(loc, now=NOW)["area"]["place"] == "morning build"  # fresh, not rebuilt
    rec = places.get_places(loc, force=True, now=NOW)
    assert rec["area"]["place"] == "Bridgewater, NJ"
    assert supabase.versions("08807") == [TODAY]
    assert supabase.rows[("08807", TODAY)]["data"]["area"]["place"] == "Bridgewater, NJ"


def test_ac_2_4_staleness_compares_version_with_utc_today(loc):
    rec = stored(loc, "2026-10-18")
    assert places.age_days(rec, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)) == 7
    assert not places.is_fresh(rec, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
    assert places.is_fresh(rec, datetime(2026, 10, 24, 23, 59, tzinfo=timezone.utc))


# --- source failures -------------------------------------------------------------

def test_ac_2_4_every_source_down_with_version_returns_it_stale(rsps, supabase, loc, caplog):
    supabase.add(stored(loc, days_ago(9)))
    mock_places_sources(rsps, loc, fail=PLACES_SOURCES)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["version"] == days_ago(9) and rec["stale"] is True
    assert supabase.versions("08807") == [days_ago(9)]  # nothing saved
    assert "Every places source failed" in caplog.text


def test_ac_2_4_no_version_and_sources_down_gives_fixed_help_only(rsps, supabase, loc, cache_dir, caplog):
    mock_places_sources(rsps, loc, fail=PLACES_SOURCES)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["area"]["zip"] == "08807" and rec["area"]["lat"] == loc.lat
    assert [h["number"] for h in rec["help"]] == ["911", "211", "1-800-621-3362", "1-800-733-2767"]
    assert rec["precautions"]["flood"]["high"]
    for section in ("waterways", "gauges", "flood_spots", "shelters", "history"):
        assert rec["status"][section] == "failed"
    assert rec["gauges"] == [] and rec["flood_spots"] == [] and rec["shelters"] == []
    assert supabase.versions("08807") == []
    assert not (cache_dir / "places" / "08807.json").exists()
    assert "nothing is stored" in caplog.text


def test_ac_2_4_one_source_down_leaves_that_section_empty(rsps, supabase, loc, caplog):
    mock_places_sources(rsps, loc, fail=("lsr",))
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["flood_spots"] == [] and rec["status"]["flood_spots"] == "failed"
    for section in ("gauges", "waterways", "shelters", "help", "history"):
        assert rec["status"][section] == "ok"
    assert rec["gauges"] and rec["history"]["warnings"]["count"]
    assert supabase.versions("08807") == [TODAY]
    assert "lsrs_by_point" in caplog.text or "fetch_flood_spots" in caplog.text


@pytest.mark.parametrize(
    "failed, section, sub",
    [
        (("vtec",), "history.warnings", "warnings"),
        (("disasters",), "history.disasters", "disasters"),
        (("nfip",), "history.flood_claims", "flood_claims"),
    ],
)
def test_ac_2_4_one_history_source_down(rsps, loc, failed, section, sub):
    mock_places_sources(rsps, loc, fail=failed)
    rec = places.build_places(loc, NOW)
    assert rec["status"][section] == "failed" and rec["history"][sub] == {}
    assert rec["status"]["history"] == "partial"


def test_ac_2_4_shelters_and_office_down(rsps, loc):
    mock_places_sources(rsps, loc, fail=("shelters", "office"))
    rec = places.build_places(loc, NOW)
    assert rec["status"]["shelters"] == "failed" and rec["shelters"] == []
    assert rec["status"]["help"] == "partial" and len(rec["help"]) == 4


def test_ac_2_4_point_down_skips_county_sources(rsps, loc):
    mock_places_sources(rsps, loc, fail=("point",))
    rec = places.build_places(loc, NOW)
    assert rec["status"]["area"] == "partial" and rec["area"]["county_ugc"] is None
    assert rec["status"]["history.warnings"] == "failed" and rec["status"]["history.disasters"] == "failed"
    assert rec["status"]["history.flood_claims"] == "ok" and rec["status"]["gauges"] == "ok"
    assert rec["all_sources_failed"] is False


def test_ac_2_4_gauge_stage_failure_keeps_gauge(rsps, loc):
    mock_places_sources(rsps, loc, fail=("gauge",))
    rec = places.build_places(loc, NOW)
    assert rec["status"]["gauges"] == "partial"
    assert rec["gauges"] and all(g["stages"] == {} for g in rec["gauges"])
    places.validate_places(rec)


# --- Supabase down or not configured ---------------------------------------------

def test_ac_2_4_supabase_down_uses_local_copy(rsps, supabase, loc, caplog):
    places.write_local(stored(loc, days_ago(1), marker="local copy"))
    supabase.down = True
    mock_places_sources(rsps, loc)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "local copy" and source_calls(rsps) == []
    assert "Supabase unavailable" in caplog.text


def test_ac_2_4_supabase_timeout_builds_and_saves_locally_only(rsps, monkeypatch, loc, cache_dir):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_KEY", "k")
    rsps.get(f"{SUPABASE_TEST_URL}/rest/v1/places_versions", body=requests.exceptions.ReadTimeout("slow"))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY and rec["status"]["gauges"] == "ok"
    assert json.loads((cache_dir / "places" / "08807.json").read_text())["version"] == TODAY
    assert not [c for c in rsps.calls if c.request.method == "POST"]  # no save attempted


def test_ac_2_4_supabase_save_failure_still_writes_local(rsps, supabase, loc, cache_dir, caplog):
    supabase.fail_status = {"POST": 500}
    mock_places_sources(rsps, loc)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY and supabase.rows == {}
    assert (cache_dir / "places" / "08807.json").exists()
    assert "Could not save places version" in caplog.text


def test_ac_2_4_supabase_not_configured_warns_once_and_uses_local(rsps, loc, cache_dir, caplog):
    mock_places_sources(rsps, loc)
    with caplog.at_level(logging.WARNING):
        first = places.get_places(loc, now=NOW)  # built, saved locally only
        rsps.calls.reset()
        second = places.get_places(loc, now=NOW)  # the local copy is now the latest version
    assert first["version"] == second["version"] == TODAY
    assert source_calls(rsps) == []
    assert caplog.text.count("Supabase is not configured") == 1
    assert "Supabase unavailable" not in caplog.text
    assert not [c for c in rsps.calls if c.request.url.startswith(SUPABASE_TEST_URL)]


def test_ac_2_4_supabase_down_old_local_copy_is_rebuilt_locally(rsps, supabase, loc, cache_dir):
    places.write_local(stored(loc, days_ago(8), marker="old local"))
    supabase.down = True
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "Bridgewater, NJ" and supabase.rows == {}
    assert json.loads((cache_dir / "places" / "08807.json").read_text())["version"] == TODAY


def test_ac_2_4_supabase_down_no_local_every_source_down(rsps, supabase, loc):
    supabase.down = True
    mock_places_sources(rsps, loc, fail=PLACES_SOURCES)
    rec = places.get_places(loc, now=NOW)
    assert rec["all_sources_failed"] and rec["status"]["gauges"] == "failed"


def test_ac_2_4_supabase_rest_raises_unavailable(rsps, monkeypatch):
    with pytest.raises(supabase_rest.SupabaseUnavailable):
        supabase_rest.latest_version("08807")  # not configured
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_KEY", "k")
    rsps.get(f"{SUPABASE_TEST_URL}/rest/v1/places_versions", status=502)
    with pytest.raises(supabase_rest.SupabaseUnavailable):
        supabase_rest.latest_version("08807")
    rsps.post(f"{SUPABASE_TEST_URL}/rest/v1/places_versions", body=requests.exceptions.ConnectTimeout("x"))
    with pytest.raises(supabase_rest.SupabaseUnavailable):
        supabase_rest.upsert_version({"zip": "08807"})


def test_ac_2_4_invalid_stored_version_is_ignored(rsps, supabase, loc):
    bad = stored(loc, days_ago(1))
    bad["help"] = []
    supabase.add(bad)
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY and source_calls(rsps)


# --- Supabase 4xx (SupabaseError) ----------------------------------------------------

@pytest.mark.parametrize(
    "fail_status, message",
    [({"GET": 404}, "Supabase unavailable"), ({"POST": 401}, "Could not save places version")],
)
def test_ac_2_4_supabase_4xx_still_returns_record_and_writes_local(
    rsps, supabase, loc, cache_dir, caplog, fail_status, message
):
    supabase.fail_status = fail_status
    mock_places_sources(rsps, loc)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY and rec["status"]["gauges"] == "ok"
    assert json.loads((cache_dir / "places" / "08807.json").read_text())["version"] == TODAY
    assert message in caplog.text
    assert supabase.rows == {}


# --- versions dated in the future ---------------------------------------------------

def test_ac_2_4_future_supabase_version_is_rejected(rsps, supabase, loc, caplog):
    supabase.add(stored(loc, "2026-11-30", marker="future"))
    mock_places_sources(rsps, loc)
    with caplog.at_level(logging.WARNING):
        rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "Bridgewater, NJ" and source_calls(rsps)
    assert "dated in the future" in caplog.text


def test_ac_2_4_future_local_copy_is_rejected(rsps, loc, caplog):
    places.write_local(stored(loc, "2026-10-26", marker="future local"))
    with caplog.at_level(logging.WARNING):
        assert places.read_local("08807", NOW) is None
    assert "dated in the future" in caplog.text
    mock_places_sources(rsps, loc)
    assert places.get_places(loc, now=NOW)["area"]["place"] == "Bridgewater, NJ"


def test_ac_2_4_is_fresh_rejects_future_age(loc):
    assert not places.is_fresh(stored(loc, "2026-10-26"), NOW)
    assert places.is_fresh(stored(loc, TODAY), NOW)


# --- newer of Supabase and the local copy wins -------------------------------------------

def test_ac_2_4_newer_local_copy_is_served_and_uploaded(rsps, supabase, loc):
    supabase.add(stored(loc, days_ago(3), marker="supabase"))
    places.write_local(stored(loc, days_ago(1), marker="local outage build"))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "local outage build" and source_calls(rsps) == []
    assert supabase.versions("08807") == [days_ago(3), days_ago(1)]
    assert supabase.rows[("08807", days_ago(1))]["data"]["area"]["place"] == "local outage build"


def test_ac_2_4_supabase_empty_fresh_local_served_without_source_calls(rsps, supabase, loc):
    places.write_local(stored(loc, days_ago(2), marker="local"))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "local" and source_calls(rsps) == []
    assert supabase.versions("08807") == [days_ago(2)]


def test_ac_2_4_newer_supabase_version_wins_over_local(rsps, supabase, loc, cache_dir):
    places.write_local(stored(loc, days_ago(4), marker="local"))
    supabase.add(stored(loc, days_ago(1), marker="supabase"))
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "supabase"
    assert json.loads((cache_dir / "places" / "08807.json").read_text())["version"] == days_ago(1)


# --- failed sections are carried from the previous version ---------------------------------

def test_ac_2_4_failed_section_carried_from_previous_and_retried(rsps, supabase, loc):
    mock_places_sources(rsps, loc)
    complete = places.get_places(loc, now=NOW - timedelta(days=8))  # a complete older version
    assert complete["flood_spots"] and places.is_fresh(complete, NOW - timedelta(days=8))

    rsps.reset()
    supabase.register(rsps)
    mock_places_sources(rsps, loc, fail=("lsr",))
    rec = places.get_places(loc, now=NOW)
    assert rec["version"] == TODAY
    assert rec["status"]["flood_spots"] == "carried"
    assert [s["id"] for s in rec["flood_spots"]] == [s["id"] for s in complete["flood_spots"]]
    assert rec["status"]["gauges"] == "ok"
    places.validate_places(rec)
    assert places.is_fresh(rec, NOW) and not places.is_fresh(rec, LATER)
    assert supabase.rows[("08807", TODAY)]["status"]["flood_spots"] == "carried"  # saved: no worse than before

    rsps.reset()
    supabase.register(rsps)
    mock_places_sources(rsps, loc)
    again = places.get_places(loc, now=LATER)  # past the retry window, so it rebuilds with sources back up
    assert source_calls(rsps)
    assert again["status"]["flood_spots"] == "ok" and places.is_fresh(again, LATER)
    assert supabase.rows[("08807", TODAY)]["status"]["flood_spots"] == "ok"


def test_ac_2_4_carried_history_part_and_gauges(rsps, supabase, loc, cache_dir):
    mock_places_sources(rsps, loc)
    complete = places.get_places(loc, now=NOW - timedelta(days=8))
    (cache_dir / "nwps_near_08807.json").unlink()  # else the gauge search falls back to its live cache
    rsps.reset()
    supabase.register(rsps)
    mock_places_sources(rsps, loc, fail=("vtec", "near"))
    rec = places.get_places(loc, now=NOW)
    assert rec["status"]["history.warnings"] == "carried" and rec["status"]["history"] == "carried"
    assert rec["history"]["warnings"] == complete["history"]["warnings"]
    assert rec["status"]["gauges"] == rec["status"]["waterways"] == "carried"
    assert rec["gauges"] == complete["gauges"]
    places.validate_places(rec)


def test_ac_2_4_section_failed_in_both_stays_failed(rsps, loc):
    mock_places_sources(rsps, loc, fail=("lsr",))
    first = places.get_places(loc, now=NOW - timedelta(days=8))
    rsps.reset()
    mock_places_sources(rsps, loc, fail=("lsr",))
    rec = places.get_places(loc, now=NOW)
    assert first["status"]["flood_spots"] == rec["status"]["flood_spots"] == "failed"
    assert places.is_fresh(rec, NOW) and not places.is_fresh(rec, LATER)


def test_ac_2_4_validate_rejects_unknown_status(loc):
    rec = stored(loc, TODAY)
    rec["status"]["gauges"] = "carried"
    places.validate_places(rec)
    rec["status"]["gauges"] = "borrowed"
    with pytest.raises(places.PlacesError, match="status"):
        places.validate_places(rec)


# --- retry window for records with failed or carried sections ------------------------------

def partial(loc, built_at):
    rec = stored(loc, TODAY, marker="partial")
    rec["status"]["flood_spots"] = "failed"
    rec["built_at"] = built_at.isoformat(timespec="seconds")
    return rec


def test_ac_2_4_partial_record_built_30_min_ago_is_fresh(rsps, supabase, loc):
    supabase.add(partial(loc, NOW - timedelta(minutes=30)))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "partial" and source_calls(rsps) == []


def test_ac_2_4_partial_record_built_61_min_ago_is_rebuilt(rsps, supabase, loc):
    supabase.add(partial(loc, NOW - timedelta(minutes=61)))
    mock_places_sources(rsps, loc)
    rec = places.get_places(loc, now=NOW)
    assert rec["area"]["place"] == "Bridgewater, NJ" and source_calls(rsps)
    assert rec["status"]["flood_spots"] == "ok"


def test_ac_2_4_complete_record_keeps_7_day_rule(loc):
    rec = stored(loc, days_ago(6))
    assert places.is_fresh(rec, NOW)  # built days ago, but complete
