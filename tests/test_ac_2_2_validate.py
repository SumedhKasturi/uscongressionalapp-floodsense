"""AC 2.2: every built record passes validate_places; help always has 911 and 211."""

import copy

import pytest

from data import places
from data.location import resolve_zip
from tests.conftest import PLACES_SOURCES, mock_places_sources


@pytest.fixture
def record(rsps):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    return places.build_places(loc)


def numbers(record):
    return [h["number"] for h in record["help"]]


def test_ac_2_2_built_record_passes_and_has_911_211(record):
    places.validate_places(record)
    assert "911" in numbers(record) and "211" in numbers(record)


def test_ac_2_2_help_lines_match_help_directory(record):
    assert numbers(record) == ["911", "211", "1-800-621-3362", "1-800-733-2767", "609-261-6600"]
    office = record["help"][-1]
    assert office["kind"] == "nws_office" and "Philadelphia/Mt Holly" in office["name"]
    assert all(h.get("verified_on") for h in record["help"][:4])


def test_ac_2_2_every_gauge_and_spot_names_a_waterway(record):
    names = {w["name"] for w in record["waterways"]}
    for g in record["gauges"]:
        assert g["waterway"] in names
        assert all(isinstance(v, float) for v in g["stages"].values())
    linked = [s for s in record["flood_spots"] if s["waterway"]]
    assert linked and all(s["waterway"] in names for s in linked)


@pytest.mark.parametrize("failed", [("lsr",), ("near",), ("point",), PLACES_SOURCES])
def test_ac_2_2_degraded_records_still_pass(rsps, failed):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc, fail=failed)
    rec = places.build_places(loc)
    places.validate_places(rec)
    assert "911" in numbers(rec) and "211" in numbers(rec)


def test_ac_2_2_fallback_record_passes():
    rec = places.fallback_record(resolve_zip("08807"))
    places.validate_places(rec)
    assert numbers(rec) == ["911", "211", "1-800-621-3362", "1-800-733-2767"]


def _broken(record, edit):
    bad = copy.deepcopy(record)
    edit(bad)
    return bad


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda r: r["gauges"][0].update(waterway=None), "names no waterway"),
        (lambda r: r["gauges"][0].update(waterway=""), "names no waterway"),
        (lambda r: r["gauges"][0].update(waterway="Nowhere Creek"), "not in waterways"),
        (lambda r: r["help"].pop(0), "missing 911"),
        (lambda r: r["help"].pop(1), "missing 211"),
        (lambda r: r["gauges"][3]["stages"].update(action="ten"), "not numeric"),
        (lambda r: r["gauges"][3]["stages"].update(minor=None), "not numeric"),
        (lambda r: r["flood_spots"][0].update(waterway="Nowhere Creek"), "nearest gauge"),
        (lambda r: r.update(version="10/25/2026"), "YYYY-MM-DD"),
        (lambda r: r.pop("shelters"), "missing 'shelters'"),
        (lambda r: r["area"].update(lat="x"), "lat/lon"),
    ],
)
def test_ac_2_2_invalid_records_raise(record, edit, message):
    with pytest.raises(places.PlacesError, match=message):
        places.validate_places(_broken(record, edit))


def test_ac_2_2_spot_beyond_5_mi_of_any_gauge_has_no_waterway(record):
    far = _broken(record, lambda r: r["flood_spots"].append(
        {"id": "far", "date": "2020-01-01T00:00:00Z", "type": "FLOOD", "remark": "", "lat": 41.5, "lon": -75.5,
         "waterway": None}))
    places.validate_places(far)
    with pytest.raises(places.PlacesError, match="no gauge is within"):
        places.validate_places(_broken(far, lambda r: r["flood_spots"][-1].update(waterway="Middle Brook")))


def test_ac_2_2_invalid_record_is_not_saved(rsps, supabase, cache_dir, monkeypatch):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    real_build = places.build_places

    def bad_build(loc, now=None):
        rec = real_build(loc, now)
        rec["help"] = [h for h in rec["help"] if h["number"] != "911"]
        return rec

    monkeypatch.setattr(places, "build_places", bad_build)
    rec = places.get_places(loc)
    assert supabase.versions("08807") == []
    assert not (cache_dir / "places" / "08807.json").exists()
    assert "911" in numbers(rec)  # the fixed-help fallback is returned instead
