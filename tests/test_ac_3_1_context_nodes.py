"""AC 3.1 (Oct 25 request): road, storm-event and FEMA-disaster context nodes, and spot -> gauge links.

The context is for reading the graph only: the risk rules must give the same result with or without it.
"""

import pytest

from data import places
from data.location import resolve_zip
from graph import build, context, reason
from tests.conftest import PLACES_NOW, mock_places_sources
from tests.graph_helpers import FLASH_FLOOD_WARNING, make_inputs, make_places


@pytest.mark.parametrize("remark, roads", [
    ("FLOODING ON U.S. HIGHWAY 22 WEST OF VOSSELLER AVENUE IN BRIDGEWATER TWP.", ["US 22", "Vosseller Ave"]),
    ("Flooding on I-78 Eastbound At East of Exit 29 - I-287/US 202/US 206 (Bridgewater Twp), 1 right lane of 3 lanes",
     ["I-78", "I-287", "US 202", "US 206"]),
    ("FLOODING ON INTERSTATE 287 NORTHBOUND AT EXIT 14.", ["I-287"]),
    ("STATE HIGHWAY 28 CLOSED EAST OF GATSON AVENUE.", ["NJ 28", "Gatson Ave"]),
    ("Route 28 and Route 22 closed.", ["NJ 28", "US 22"]),
    ("FLOODING ON CR-622 SOUTHBOUND CR-607 (MIDDLESEX) TO BAEKELAND AVE", ["CR 622", "CR 607", "Baekeland Ave"]),
    ("OLD YORK ROAD WAS CLOSED BETWEEN BRANCHBURG AND BRIDGEWATER DUE TO FLOODING.", ["Old York Rd"]),
    ("Flooding at the intersection of West End Ave and West Main Street.", ["West End Ave", "West Main St"]),
    ("1 right lane closed; left lane closed; all lanes closed. Lane restrictions in place.", []),
    ("FLOODING ON U.S. HIGHWAY 22 NEAR THE INTERSTATE 287 JUNCTION. LANE RESTRICTIONS IN PLACE.", ["US 22", "I-287"]),
    ("Report from mPING: Street/road flooding; Street/road closed.", []),
    ("HEAVY RAIN CAUSED ROAD CLOSURES ACROSS SOMERSET COUNTY.", []),
    ("Middle Brook at Bound Brook reached flood stage.", []),
    (None, []),
])
def test_ac_3_1_roads_in_remarks(remark, roads):
    assert context.roads_in(remark) == roads


# Outside New Jersey (Farmington, NM, Oct 25): the ZIP's own state names its routes.
@pytest.mark.parametrize("remark, state, us_seen, roads", [
    ("NM DOT reported NM 371 closed b/w MM 103 and 107.", "NM", set(), ["NM 371"]),
    ("Water over Highway 64 at mile marker 57 between CR 5500 and CR 5777.", "NM", {64},
     ["US 64", "CR 5500", "CR 5777"]),
    ("BRIDGE WASHED OUT ON HIGHWAY 170.", "NM", set(), ["Hwy 170"]),
    ("Route 22 closed.", "NM", set(), ["Hwy 22"]),  # NJ's US-route table doesn't apply elsewhere
    ("WATER IN 5 HOMES ON OAK STREET.", "IN", set(), ["Oak St"]),  # "IN" is a word, not a route prefix
    ("Flooding on IN 37 near Oak Street.", "TX", set(), ["Oak St"]),  # another state's code isn't this state's
    # Saint Johns, FL (32259): "St" before a name is "Saint", and both spellings are one road.
    ("Julington Creek at Old Saint Augustine Rd, peaked at 2.03 feet.", "FL", set(), ["Old St Augustine Rd"]),
    ("Vehicles stuck at Gran Bay Pkwy and Old St Augustine road.", "FL", set(), ["Old St Augustine Rd"]),
    ("STANDING WATER NEAR I-95, I-295, AND OLD ST. AUGUSTINE RD.", "FL", set(),
     ["I-95", "I-295", "Old St Augustine Rd"]),
    ("EAST BOUND LANES OF SR 210 CLOSED AT THE I-95 INTERCHANGE.", "FL", set(), ["FL 210", "I-95"]),
    ("FLOODING ON MAIN ST NEAR THE RIVER ROAD BRIDGE.", "FL", set(), ["Main St", "River Rd"]),
])
def test_ac_3_1_roads_in_other_states(remark, state, us_seen, roads):
    assert context.roads_in(remark, state, us_seen) == roads


def test_ac_3_1_state_and_us_routes_from_places():
    assert context.state_of({"area": {"county_ugc": "NMC045", "place": "Farmington, NM"}}) == "NM"
    assert context.state_of({"area": {"place": "Seattle, WA"}}) == "WA"
    assert context.state_of({}) == "NJ"
    assert context.us_routes_in(["FLASH FLOOD ALONG U.S. HIGHWAY 64.", None, "Highway 170"]) == {64}


def _spot(i, when, remark="flooding", lat=40.58, lon=-74.57, waterway="Middle Brook"):
    return {"id": f"s{i}", "date": when, "type": "FLOOD", "remark": remark, "lat": lat, "lon": lon,
            "waterway": waterway}


def test_ac_3_1_storm_events_merge_consecutive_local_days():
    spots = [
        _spot(1, "2021-09-01T23:00:00Z"), _spot(2, "2021-09-02T03:00:00Z"),  # Sep 1 and Sep 1 (local, EDT)
        _spot(3, "2021-09-02T15:00:00Z"),  # Sep 2: merged into the same storm
        _spot(4, "2021-09-10T15:00:00Z"),  # single-report day: no event
        _spot(5, "2023-12-18T15:00:00Z"), _spot(6, "2023-12-18T18:00:00Z"),
    ]
    events = context.storm_events(spots)
    assert [(e["id"], e["label"], e["spots"]) for e in events] == [
        ("event:2021-09-01", "Storm of Sep 1–2, 2021 · 3 reports", ["s1", "s2", "s3"]),
        ("event:2023-12-18", "Storm of Dec 18, 2023 · 2 reports", ["s5", "s6"]),
    ]


IDA_DR = {"number": 4614, "id": "DR-4614-NJ", "declared": "2021-09-05", "incident_type": "Hurricane",
          "title": "REMNANTS OF HURRICANE IDA"}
IDA_EM = {"number": 3573, "id": "EM-3573-NJ", "declared": "2021-09-02", "incident_type": "Hurricane",
          "title": "REMNANTS OF HURRICANE IDA"}
COVID = {"number": 4488, "id": "DR-4488-NJ", "declared": "2020-03-25", "incident_type": "Biological",
         "title": "COVID-19 PANDEMIC"}
SNOW = {"number": 4368, "id": "DR-4368-NJ", "declared": "2018-06-08", "incident_type": "Severe Storm",
        "title": "SEVERE WINTER STORM AND SNOWSTORM"}
FLOOD_1996 = {"number": 1145, "id": "DR-1145-NJ", "declared": "1996-11-19", "incident_type": "Severe Storm",
              "title": "SEVERE STORMS AND FLOODING"}
FLOYD_DR = {"number": 1295, "id": "DR-1295-NJ", "declared": "1999-09-18", "incident_type": "Hurricane",
            "title": "HURRICANE FLOYD MAJOR DISASTER DECLARATIONS"}
FLOYD_EM = {"number": 3148, "id": "EM-3148-NJ", "declared": "1999-09-17", "incident_type": "Hurricane",
            "title": "HURRICANE FLOYD EMERGENCY DECLARATIONS"}


def test_ac_3_1_flood_disasters_link_dedupe_and_filter():
    events = context.storm_events([_spot(1, "2021-09-01T23:00:00Z"), _spot(2, "2021-09-02T15:00:00Z"),
                                   _spot(3, "2021-08-22T15:00:00Z"), _spot(4, "2021-08-22T18:00:00Z")])
    out = context.flood_disasters([IDA_DR, IDA_EM, COVID, SNOW, FLOOD_1996, FLOYD_DR, FLOYD_EM], events)
    by_id = {d["id"]: d for d in out}
    assert list(by_id) == ["disaster:4614", "disaster:1145", "disaster:1295"]  # EMs folded into DRs; no COVID/snow
    assert by_id["disaster:4614"]["label"] == "Remnants of Hurricane Ida (DR-4614)"
    assert by_id["disaster:4614"]["events"] == ["event:2021-09-01"]  # not the Aug 22 storm (Henri)
    assert by_id["disaster:1145"]["events"] == []  # flood-related, kept without a linked storm
    # A linked snow declaration would be kept, because a storm links to it.
    snow_events = context.storm_events([_spot(5, "2018-06-03T15:00:00Z"), _spot(6, "2018-06-03T16:00:00Z")])
    assert [d["id"] for d in context.flood_disasters([SNOW], snow_events)] == ["disaster:4368"]


def test_ac_3_1_incident_period_used_when_present():
    item = dict(FLOOD_1996, incident_begin="1996-10-18", incident_end="1996-10-21")
    events = context.storm_events([_spot(1, "1996-10-21T15:00:00Z"), _spot(2, "1996-10-22T15:00:00Z")])
    assert context.flood_disasters([item], events)[0]["events"] == ["event:1996-10-21"]


@pytest.fixture
def fixture_record(rsps):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    return places.build_places(loc, now=PLACES_NOW)


def test_ac_3_1_context_from_08807_fixture(fixture_record):
    G = build.build_graph(fixture_record, make_inputs())
    by_type = {}
    for n, d in G.nodes(data=True):
        by_type.setdefault(d["type"], []).append(n)
    assert len(by_type["road"]) >= 30 and len(by_type["storm event"]) >= 15
    assert G.nodes["road:us_22"]["label"] == "US 22"
    on_22 = [u for u in G.predecessors("road:us_22") if G.edges[u, "road:us_22"]["rel"] == "on"]
    assert len(on_22) >= 15 and all(G.nodes[u]["type"] == "spot" for u in on_22)
    for name in ("I-287", "US 206", "I-78", "US 202", "Old York Rd", "River Rd", "Island Rd"):
        assert context.road_id(name) in G, name
    ida = G.nodes["event:2021-09-01"]
    assert ida["label"].startswith("Storm of Sep 1–2, 2021 · ") and ida["reports"] >= 13
    assert G.edges["event:2021-09-01", "disaster:4614"]["rel"] == "declared as"
    assert not any(G.nodes[n]["label"].upper().startswith("COVID") for n in by_type["disaster"])
    # Every spot links to its nearest gauge within 5 miles, the same gauge whose waterway it took.
    gauges = {g["lid"]: g for g in fixture_record["gauges"]}
    for s in fixture_record["flood_spots"]:
        monitored = [v for v in G.successors(f"spot:{s['id']}") if G.edges[f"spot:{s['id']}", v]["rel"] == "monitored by"]
        if s["waterway"]:
            assert len(monitored) == 1 and gauges[monitored[0].split(":")[1]]["waterway"] == s["waterway"]
        else:
            assert monitored == []


SCENARIOS = [
    {},
    {"alerts": [FLASH_FLOOD_WARNING]},
    {"gauge_levels": {"BDBN4": 10.2}},
    {"gauge_levels": {"BDBN4": 11.3}, "rain_in_per_hr_max": 1.2},
    {"gauge_levels": {"BDBN4": 9.0}, "gauge_rises": {"BDBN4": 1.2}},
    {"gauge_levels": {"SMVN4": 3.0, "MRTN4": 6.5}, "gauge_rises": {"SMVN4": 1.5}},
    {"rain_6h_in": 2.2},
    {"alerts": [FLASH_FLOOD_WARNING], "gauge_levels": {"BDBN4": 10.5, "MRTN4": 5.2}, "rain_6h_in": 2.5},
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_ac_3_1_context_does_not_change_the_rules(fixture_record, monkeypatch, scenario):
    for record in (make_places(), fixture_record):
        inputs = make_inputs(**scenario)
        with_context = build.build_graph(record, inputs)
        with monkeypatch.context() as m:
            m.setattr(context, "add_context", lambda *a, **k: None)
            without = build.build_graph(record, inputs)
        assert with_context.number_of_nodes() > without.number_of_nodes()
        a, b = reason.assess(with_context, record, inputs), reason.assess(without, record, inputs)
        assert a.area_level == b.area_level
        assert a.waterway_levels == b.waterway_levels and a.spot_levels == b.spot_levels
        assert a.reasons == b.reasons
        assert a.precautions == b.precautions and a.help_order == b.help_order
