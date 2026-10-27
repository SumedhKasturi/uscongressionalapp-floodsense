"""AC 2.3: built from recorded 08807 fixtures, the record has the known flood spots and gauges."""

import re

import pytest

from data import places
from data.location import resolve_zip
from tests.conftest import mock_places_sources, places_fixture


@pytest.fixture
def record(rsps):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    return places.build_places(loc)


def remarks_matching(record, *patterns):
    return [
        s for s in record["flood_spots"]
        if all(re.search(p, s["remark"], re.IGNORECASE) for p in patterns)
    ]


US_22 = r"\b(US|U\.S\.|US-|U\.S\. HIGHWAY|US ROUTE)\s?22\b"


@pytest.mark.parametrize(
    "label, patterns",
    [
        ("US 22 at Vosseller Ave", (US_22, r"VOSSELLER")),
        ("US 22 at Mountain Ave", (US_22, r"MOUNTAIN AVE")),
        ("US 22 at I-287", (US_22, r"(I-287|INTERSTATE 287)")),
        ("NJ 28", (r"\b(NJ|STATE HIGHWAY|ROUTE)\s?28\b",)),
        ("Old York Rd", (r"OLD YORK ROAD",)),
    ],
)
def test_ac_2_3_known_flood_spots(record, label, patterns):
    spots = remarks_matching(record, *patterns)
    assert spots, f"no flood spot for {label}"
    for s in spots:
        assert s["waterway"], f"{label} spot is not linked to a waterway"
        assert s["id"] == places.spot_id(s["date"], s["lat"], s["lon"])


def test_ac_2_3_known_gauges_and_stages(record):
    by_lid = {g["lid"]: g for g in record["gauges"]}
    bdbn4 = by_lid["BDBN4"]
    assert bdbn4["name"] == "Middle Brook at Bound Brook" and bdbn4["waterway"] == "Middle Brook"
    assert bdbn4["stages"] == {"action": 10.0, "minor": 11.0}  # -9999 moderate/major dropped
    mrtn4 = by_lid["MRTN4"]
    assert mrtn4["waterway"] == "West Branch Middle Brook"
    assert mrtn4["stages"] == {"action": 5.0, "minor": 6.0}
    assert by_lid["MNVN4"]["stages"] == {"action": 12.0, "minor": 14.0, "moderate": 17.0, "major": 20.0}
    assert {"Middle Brook", "West Branch Middle Brook", "Raritan River"} <= {w["name"] for w in record["waterways"]}


def test_ac_2_3_only_flood_reports_kept_and_deduplicated(record):
    raw = places_fixture("iem_lsr_08807.json")["data"]
    flood_rows = [r for r in raw if "FLOOD" in r["typetext"]]
    assert len(raw) > len(flood_rows)  # the fixture keeps some non-flood rows
    assert all("FLOOD" in s["type"] for s in record["flood_spots"])
    unique = {places.spot_id(r["valid"], r["lat"], r["lon"]) for r in flood_rows}
    assert len(record["flood_spots"]) == len(unique)
    dates = [s["date"] for s in record["flood_spots"]]
    assert dates == sorted(dates, reverse=True)


def test_ac_2_3_spot_links_to_nearest_gauge_within_5_mi(record):
    gauges = record["gauges"]
    from data.sources import haversine_mi

    for s in record["flood_spots"]:
        dists = sorted((haversine_mi(s["lat"], s["lon"], g["lat"], g["lon"]), g["waterway"]) for g in gauges)
        expected = dists[0][1] if dists and dists[0][0] <= 5 else None
        assert s["waterway"] == expected


def test_ac_2_3_history_summaries(record):
    w = record["history"]["warnings"]
    events = places_fixture("iem_vtec_NJC035.json")["events"]
    assert w["count"] == len(events) and w["ugc"] == "NJC035"
    assert sum(p["count"] for p in w["by_phenomena"].values()) == len(events)
    assert len(w["latest"]) == 10 and w["latest"][0]["issue"] == max(e["issue"] for e in events)
    d = record["history"]["disasters"]
    assert d["fips"] == "34035" and d["count"] == 30
    assert any(i["incident_type"] == "Hurricane" for i in d["items"])
    c = record["history"]["flood_claims"]
    assert c["count"] == 258 and c["by_year"]["2021"]["count"] > 0
    assert c["total_paid"] == pytest.approx(sum(y["paid"] for y in c["by_year"].values()), abs=1)


@pytest.mark.parametrize(
    "name, waterway",
    [
        ("Middle Brook at Bound Brook", "Middle Brook"),
        ("West Branch Middle Brook near Martinsville", "West Branch Middle Brook"),
        ("Raritan River below Calco Dam at Bound Brook", "Raritan River"),
        ("Passaic River above Pompton River", "Passaic River"),
        ("Mississippi River At St. Louis", "Mississippi River"),
        ("Green River (WA) NEAR Auburn", "Green River (WA)"),
        ("Lake Okeechobee", "Lake Okeechobee"),
        ("  Peters Brook   at Somerville ", "Peters Brook"),
        ("Batten Kill", "Batten Kill"),  # "at" inside a word is not a separator
        ("", None),
        (None, None),
    ],
)
def test_ac_2_3_waterway_from_gauge_name(name, waterway):
    assert places.waterway_from_gauge_name(name) == waterway


def test_ac_2_3_shelters_nearest_first(rsps):
    loc = resolve_zip("08807")
    body = places_fixture("fema_shelters_sample.json")
    # Move the two real sample shelters near Bridgewater (the 25 mi filter runs server side).
    body["features"][0]["geometry"] = {"x": -74.50, "y": 40.70}  # about 9 mi
    body["features"][1]["geometry"] = {"x": -74.60, "y": 40.60}  # about 1.5 mi
    mock_places_sources(rsps, loc, shelters=body)
    shelters = places.build_places(loc)["shelters"]
    assert [s["name"] for s in shelters] == ["Bellevue Recreation Center", "Rock Point Community School"]
    assert shelters[0]["distance_mi"] < shelters[1]["distance_mi"]
    assert shelters[0]["address"] == "826 Lucile Avenue"
