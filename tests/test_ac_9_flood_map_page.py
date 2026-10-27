"""Story 9 (CAP-5): the Flood map page, its three pydeck layers, tooltips and degraded record.

Every page test runs the real app shell with AppTest and reads the deck spec JSON. The places
record and live inputs are patched into ``assessment_loader``, so no request is made (any
unmocked HTTP call fails through ``rsps``).
"""

from __future__ import annotations

import copy
import json
import logging
import math

import pytest
import responses
import streamlit
from streamlit.testing.v1 import AppTest

import app_logging
import assessment_loader
from data import places
from data.location import resolve_zip
from data import location
from tests.conftest import NWPS_GAUGES_URL, PLACES_NOW, load_fixture, mock_nws, mock_places_sources, shift_nwps
from tests.test_ac_3_1_graph_schema import STAGEFLOW_URL, stageflow_callback
from tests.graph_helpers import FLASH_FLOOD_WARNING, SPOT_2009, make_inputs, make_places
from tests.test_ac_5_help_page import SHELTER_CLOSED, SHELTER_NEAR
from ui_shared import ASK_FOR_ZIP

APP = "../app.py"
PAGE = "app_pages/map.py"
BLUE_GRAY, ORANGE, RED = [96, 125, 139], [245, 124, 0], [211, 47, 47]
GREEN, GRAY, VIOLET = [46, 160, 67], [140, 140, 140], [123, 31, 162]
UNKNOWN_GRAY = [176, 176, 176]
HISTORY_UNAVAILABLE = "Local flood history is unavailable for this ZIP."
SHELTERS_UNAVAILABLE = "Shelter data is unavailable right now"
LEGEND = [":blue-badge[:material/check_circle: Low risk]", ":orange-badge[:material/warning: Elevated risk]",
          ":red-badge[:material/emergency_home: High risk]", ":gray-badge[:material/help: Risk unknown]",
          ":green-badge[:material/check_circle: No flooding]", ":orange-badge[:material/warning: Action stage]",
          ":red-badge[:material/emergency_home: Minor flooding or worse]",
          ":gray-badge[:material/remove: No flood status]", ":violet-badge[:material/home: Open shelter]"]


# --- helpers -----------------------------------------------------------------------

def patch_data(monkeypatch, record: dict, inputs=None):
    """Serve ``record`` and ``inputs`` (default: calm) from the cached loaders."""
    inputs = inputs or make_inputs()
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: record)
    monkeypatch.setattr(assessment_loader, "load_inputs", lambda loc, rec: inputs)


def patch_malformed(monkeypatch, record: dict, inputs=None, clean: dict | None = None):
    """Serve a malformed ``record`` and ``inputs`` to the map, with the calm assessment of ``clean``
    (default: make_places()).

    Validated places records never hold such values and the rules don't guard against them; these
    tests check that the map's own row builders never raise.
    """
    from graph import build, reason

    inputs = inputs or make_inputs()
    clean = clean or make_places()
    G = build.build_graph(clean, make_inputs())
    assessment = reason.assess(G, clean, make_inputs())
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: record)
    monkeypatch.setattr(assessment_loader, "load_assessment", lambda loc: (record, inputs, G, assessment))


def open_map(zip_code=None):
    at = AppTest.from_file(APP, default_timeout=30).switch_page(PAGE)
    at.run()
    assert not at.exception, at.exception
    if zip_code is not None:
        at.text_input(key="zip").set_value(zip_code).run()
        assert not at.exception, at.exception
    return at


def decks(at):
    return at.get("deck_gl_json_chart")


def _no_constants(name):
    raise ValueError(f"bare {name} in the deck JSON")


def deck_spec(at) -> dict:
    """The deck spec; a bare NaN or Infinity (which breaks the map in the browser) fails the test."""
    return json.loads(decks(at)[0].proto.json, parse_constant=_no_constants)


def layers(at) -> dict[str, list[dict]]:
    """Layer id -> its rows (lat, lon, color, tooltip)."""
    return {layer["id"]: layer["data"] for layer in deck_spec(at)["layers"]}


def captions(at) -> list[str]:
    return [c.value for c in at.caption]


def fixture_08807() -> dict:
    """The 08807 places record built from the recorded source responses."""
    loc = resolve_zip("08807")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as r:
        mock_places_sources(r, loc)
        return places.build_places(loc, now=PLACES_NOW)


# --- AC 9.1: centered on the ZIP, three layers -------------------------------------------

def test_ac_9_1_menu_has_flood_map_after_forecast(rsps, monkeypatch):
    seen = []
    real = streamlit.navigation

    def spy(pages, **kw):
        seen.append(list(pages))
        return real(pages, **kw)

    monkeypatch.setattr(streamlit, "navigation", spy)
    patch_data(monkeypatch, make_places())
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    pages = seen[-1]
    titles = [p.title for p in pages]
    assert titles.index("Flood map") == titles.index("Search weather forecast") + 1
    flood_map = pages[titles.index("Flood map")]
    assert flood_map.url_path == "map" and flood_map.icon == ":material/map:"


def test_ac_9_1_normal_08807_centered_with_three_layers(rsps, monkeypatch):
    """Matrix row Normal: 08807, calm -> centered near 40.59, -74.61, blue-gray spots, counts caption."""
    record = fixture_08807()
    patch_data(monkeypatch, record)
    at = open_map()
    assert len(decks(at)) == 1
    view = deck_spec(at)["initialViewState"]
    assert abs(view["latitude"] - 40.59) < 0.05 and abs(view["longitude"] + 74.61) < 0.05
    by_id = layers(at)
    assert list(by_id) == ["flood_spots", "gauges", "shelters"]
    assert all(layer["@@type"] == "ScatterplotLayer" and layer["pickable"] for layer in deck_spec(at)["layers"])
    n_spots = len([s for s in record["flood_spots"] if s.get("lat") is not None and s.get("lon") is not None])
    assert n_spots > 0 and len(by_id["flood_spots"]) == n_spots
    assert len(by_id["gauges"]) == len(record["gauges"]) > 0
    assert by_id["shelters"] == []
    assert all(row["color"] == BLUE_GRAY for row in by_id["flood_spots"])
    assert f"{n_spots} flood spots · {len(record['gauges'])} gauges · 0 open shelters" in captions(at)
    tip = json.loads(decks(at)[0].proto.tooltip)
    assert tip == {"text": "{tooltip}"} and "html" not in tip  # plain text only, never HTML
    assert not any(HISTORY_UNAVAILABLE in c or SHELTERS_UNAVAILABLE in c for c in captions(at))


def test_ac_9_1_map_has_alt_text_and_default_carto_basemap(rsps, monkeypatch):
    patch_data(monkeypatch, make_places())
    at = open_map()
    spec = deck_spec(at)
    # No style or key set: Streamlit draws its default Carto basemap.
    assert spec["mapProvider"] == "carto" and "cartocdn.com" in spec["mapStyle"]
    assert decks(at)[0].proto.alt == ("Map of flood spots, river gauges and open shelters around "
                                      "Bridgewater, NJ (08807)")


def test_ac_9_1_layer_pills_all_on_by_default(rsps, monkeypatch):
    patch_data(monkeypatch, make_places())
    at = open_map()
    pills = at.pills(key="map_layers")
    assert pills.options == ["Flood spots", "Gauges", "Shelters"]
    assert pills.value == ["Flood spots", "Gauges", "Shelters"]


@pytest.mark.parametrize("shown, expected", [
    (["Flood spots", "Shelters"], ["flood_spots", "shelters"]),
    (["Gauges", "Shelters"], ["gauges", "shelters"]),
    (["Flood spots", "Gauges"], ["flood_spots", "gauges"]),
    ([], []),
])
def test_ac_9_1_layer_off_removes_that_layer(rsps, monkeypatch, shown, expected):
    """Matrix row Layer off: a deselected layer is not in the deck spec; none selected draws an empty map."""
    patch_data(monkeypatch, make_places())
    at = open_map()
    at.pills(key="map_layers").set_value(shown).run()
    assert not at.exception, at.exception
    assert list(layers(at)) == expected
    assert "5 flood spots · 3 gauges · 2 open shelters" in captions(at)


def test_ac_9_1_legend_and_counts(rsps, monkeypatch):
    patch_data(monkeypatch, make_places())
    at = open_map()
    # Exact badges, each color with its label, in legend order.
    assert [m.value for m in at.main.markdown if "-badge[" in m.value] == LEGEND
    assert "5 flood spots · 3 gauges · 2 open shelters" in captions(at)


# --- AC 9.2: colors follow spot_levels, tooltips name each place ---------------------------

def test_ac_9_2_risk_raised_middle_brook_spots_orange(rsps, monkeypatch):
    """Matrix row Risk raised: BDBN4 at 10.2 ft (action) -> Middle Brook spots orange, others blue-gray."""
    patch_data(monkeypatch, make_places(), make_inputs(gauge_levels={"BDBN4": 10.2}))
    at = open_map()
    by_id = layers(at)
    colors = {row["tooltip"].split("\n")[0]: row["color"] for row in by_id["flood_spots"]}
    assert colors == {
        "US 22 at Mountain Ave closed": ORANGE,
        "US 22 at Vosseller Ave flooded": ORANGE,
        "Old York Rd flooded": ORANGE,
        "Washington Valley Rd flooded near Martinsville": BLUE_GRAY,
        "NJ 28 east of Mountain Ave flooded": BLUE_GRAY,
    }
    gauge = next(row for row in by_id["gauges"] if row["tooltip"].startswith("Middle Brook at Bound Brook"))
    assert gauge["tooltip"] == "Middle Brook at Bound Brook\nLevel: 10.20 ft\nAction stage"
    assert gauge["color"] == ORANGE


def test_ac_9_2_flash_flood_warning_recent_spots_red(rsps, monkeypatch):
    """Matrix row Flash Flood Warning: recent spots red; a spot older than 10 years keeps its level."""
    patch_data(monkeypatch, make_places(), make_inputs(alerts=[FLASH_FLOOD_WARNING]))
    at = open_map()
    rows = layers(at)["flood_spots"]
    old = [r for r in rows if r["tooltip"].startswith("Old York Rd")]
    assert [r["color"] for r in old] == [BLUE_GRAY]
    assert all(r["color"] == RED and r["tooltip"].endswith("High risk") for r in rows if r not in old)
    assert len(rows) == 5


def test_ac_9_2_colors_match_spot_levels(rsps, monkeypatch):
    record = make_places()
    inputs = make_inputs(gauge_levels={"BDBN4": 11.5, "MRTN4": 5.2})
    patch_data(monkeypatch, record, inputs)
    _, _, _, assessment = assessment_loader.load_assessment(resolve_zip("08807"))
    at = open_map()
    rgb = {"low": BLUE_GRAY, "elevated": ORANGE, "high": RED}
    expected = [rgb[assessment.spot_levels[f"spot:{s['id']}"]] for s in record["flood_spots"]]
    assert [r["color"] for r in layers(at)["flood_spots"]] == expected
    assert RED in expected and ORANGE in expected


def test_ac_9_2_tooltips_name_each_place(rsps, monkeypatch):
    long_spot = dict(SPOT_2009, id="long", remark="Main Street " + "flooded and closed " * 10)
    record = make_places(spots=[SPOT_2009, long_spot], shelters=[SHELTER_NEAR, SHELTER_CLOSED])
    patch_data(monkeypatch, record, make_inputs(gauge_levels={"MRTN4": 4.0}))
    at = open_map()
    by_id = layers(at)
    spot_tips = [r["tooltip"] for r in by_id["flood_spots"]]
    assert spot_tips[0] == "Old York Rd flooded\nAug 1, 2009\nLow risk"
    name = spot_tips[1].split("\n")[0]
    assert name.startswith("Main Street flooded") and len(name) <= 80 and name.endswith("…")
    gauge_tips = [r["tooltip"] for r in by_id["gauges"]]
    assert "West Branch Middle Brook near Martinsville\nLevel: 4.00 ft\nNo flooding" in gauge_tips
    # A gauge with no stages has no flood status; one with no reading says so.
    assert "Peters Brook at Somerville\nLevel: No reading\nNo flood status" in gauge_tips
    assert [r["color"] for r in by_id["gauges"]] == [GRAY, GREEN, GRAY]
    # Only open shelters, named with their address; the closed one is left off.
    assert by_id["shelters"] == [{"lat": 40.6012, "lon": -74.6573, "color": VIOLET,
                                  "tooltip": "Bridgewater-Raritan High School\n600 Garretson Rd, Bridgewater, NJ"}]
    assert any("1 open shelter" in c for c in captions(at))


def test_ac_9_2_no_help_numbers_on_the_map(rsps, monkeypatch):
    patch_data(monkeypatch, make_places())
    at = open_map()
    text = json.dumps(deck_spec(at))
    assert "911" not in text and "211" not in text and "tel:" not in text


# --- AC 9.3: degraded places record ----------------------------------------------------------

def test_ac_9_3_degraded_record_renders_empty_layers(rsps, monkeypatch):
    """Matrix row No places data: centered on the ZIP, all three layers (empty), a caption, no crash."""
    loc = resolve_zip("08807")
    record = places.fallback_record(loc, PLACES_NOW)
    assessed = []
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: record)
    monkeypatch.setattr(assessment_loader, "load_assessment", lambda loc: assessed.append(loc))
    at = open_map()
    view = deck_spec(at)["initialViewState"]
    assert (view["latitude"], view["longitude"]) == (loc.lat, loc.lon)
    assert layers(at) == {"flood_spots": [], "gauges": [], "shelters": []}
    assert "0 flood spots · 0 gauges · shelters unavailable" in captions(at)
    assert HISTORY_UNAVAILABLE in captions(at) and SHELTERS_UNAVAILABLE in captions(at)
    assert not at.error
    assert not assessed  # no live refetch for the fallback record


def test_ac_9_3_failed_history_keeps_live_gauge_layer(rsps, monkeypatch):
    """A record whose spots and shelters failed still maps its gauges with their live levels."""
    record = make_places(spots=[], shelters=[])
    record["status"] = {"flood_spots": places.FAILED, "shelters": places.FAILED, "gauges": places.OK}
    patch_data(monkeypatch, record, make_inputs(gauge_levels={"BDBN4": 11.2}))
    at = open_map()
    by_id = layers(at)
    assert by_id["flood_spots"] == [] and by_id["shelters"] == []
    assert len(by_id["gauges"]) == 3
    assert by_id["gauges"][0]["tooltip"] == "Middle Brook at Bound Brook\nLevel: 11.20 ft\nMinor flooding"
    assert by_id["gauges"][0]["color"] == RED
    assert HISTORY_UNAVAILABLE in captions(at) and SHELTERS_UNAVAILABLE in captions(at)


# --- Remaining matrix rows ------------------------------------------------------------------

def test_ac_9_spot_without_coordinates_is_skipped_and_counted(rsps, monkeypatch):
    """Matrix row Spot without coordinates: skipped, counted as not mapped."""
    record = make_places()
    record["flood_spots"][0]["lat"] = None
    patch_data(monkeypatch, record)
    at = open_map()
    rows = layers(at)["flood_spots"]
    assert len(rows) == 4 and not any(r["tooltip"].startswith("US 22 at Mountain Ave") for r in rows)
    assert "4 flood spots · 3 gauges · 2 open shelters · 1 not mapped (no location)" in captions(at)


def test_ac_9_load_failure_shows_unavailable_and_stops(rsps, monkeypatch, caplog):
    """Matrix row Load failure: "Live data unavailable", page stops, logged."""
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: make_places())

    def boom(loc):
        raise RuntimeError("assessment down")

    monkeypatch.setattr(assessment_loader, "load_assessment", boom)
    with caplog.at_level(logging.ERROR, logger=app_logging.log.name):
        at = open_map()
    assert [e.value for e in at.error] == ["Live data unavailable"]
    assert not decks(at) and not at.pills
    assert any("Flood map data could not be loaded" in r.getMessage() for r in caplog.records)


def test_ac_9_invalid_zip_asks_for_zip(rsps, monkeypatch):
    """Matrix row Invalid ZIP: the shared ask-for-ZIP info; the page stops."""
    patch_data(monkeypatch, make_places())
    at = open_map("abcde")
    assert ASK_FOR_ZIP in [i.value for i in at.info]
    assert not decks(at)


def test_ac_9_open_shelter_filter_shared_with_help_page():
    from ui_shared import is_open

    assert is_open({"status": "open"}) and is_open({"status": None}) and not is_open(copy.copy(SHELTER_CLOSED))


# --- Review patches ---------------------------------------------------------------------------

# P1: only finite, in-range coordinates are mapped.

@pytest.mark.parametrize("field, value", [("lat", math.nan), ("lat", "north"), ("lon", None), ("lat", 91.0),
                                          ("lon", -181.0), ("lat", math.inf)])
def test_ac_9_p1_bad_spot_coordinates_not_mapped(rsps, monkeypatch, field, value):
    record = make_places()
    record["flood_spots"][0][field] = value
    patch_malformed(monkeypatch, record)
    at = open_map()
    rows = layers(at)["flood_spots"]  # parses with no bare NaN/Infinity
    assert len(rows) == 4 and not any(r["tooltip"].startswith("US 22 at Mountain Ave") for r in rows)
    assert "4 flood spots · 3 gauges · 2 open shelters · 1 not mapped (no location)" in captions(at)


def test_ac_9_p1_gauge_and_shelter_without_position_not_mapped(rsps, monkeypatch):
    record = make_places()
    record["gauges"][0]["lat"] = None
    record["shelters"][0]["lon"] = math.nan
    patch_data(monkeypatch, record)
    at = open_map()
    by_id = layers(at)
    assert [r["tooltip"].split("\n")[0] for r in by_id["gauges"]] == [
        "West Branch Middle Brook near Martinsville", "Peters Brook at Somerville"]
    assert [r["tooltip"].split("\n")[0] for r in by_id["shelters"]] == ["Near Gym"]
    assert "5 flood spots · 2 gauges · 1 open shelter · 2 not mapped (no location)" in captions(at)


# P2: unknown readings and stages are never shown as safe.

def gauge_tip(at, name: str) -> dict:
    return next(r for r in layers(at)["gauges"] if r["tooltip"].startswith(name))


def test_ac_9_p2_nan_reading_is_no_reading(rsps, monkeypatch):
    patch_data(monkeypatch, make_places(), make_inputs(gauge_levels={"BDBN4": math.nan}))
    at = open_map()
    row = gauge_tip(at, "Middle Brook at Bound Brook")
    assert row["tooltip"] == "Middle Brook at Bound Brook\nLevel: No reading\nNo flood status"
    assert row["color"] == GRAY


def test_ac_9_p2_all_null_stages_are_no_flood_status(rsps, monkeypatch):
    record = make_places()
    record["gauges"][0]["stages"] = {"action": None, "minor": None, "moderate": None, "major": None}
    patch_data(monkeypatch, record, make_inputs(gauge_levels={"BDBN4": 3.0}))
    at = open_map()
    row = gauge_tip(at, "Middle Brook at Bound Brook")
    assert row["tooltip"].endswith("No flood status") and row["color"] == GRAY


def test_ac_9_p2_string_stage_is_coerced(rsps, monkeypatch):
    record = make_places()
    record["gauges"][0]["stages"] = {"action": "10.2", "minor": "eleven"}
    patch_malformed(monkeypatch, record, make_inputs(gauge_levels={"BDBN4": 10.3}))
    at = open_map()
    row = gauge_tip(at, "Middle Brook at Bound Brook")
    assert row["tooltip"] == "Middle Brook at Bound Brook\nLevel: 10.30 ft\nAction stage"


def test_ac_9_p2_gauge_levels_none_does_not_crash(rsps, monkeypatch):
    patch_malformed(monkeypatch, make_places(), make_inputs(gauge_levels=None))
    at = open_map()
    assert all("Level: No reading" in r["tooltip"] for r in layers(at)["gauges"])


def test_ac_9_p2_malformed_fields_do_not_break_the_map(rsps, monkeypatch):
    record = make_places()
    record["flood_spots"][0]["type"] = 7
    record["flood_spots"][0]["remark"] = None
    record["gauges"][0]["stages"] = ["not", "a", "dict"]
    record["shelters"].append("not a shelter")
    patch_malformed(monkeypatch, record, make_inputs(gauge_levels={"BDBN4": "high"}))
    at = open_map()
    assert len(layers(at)["gauges"]) == 3
    assert gauge_tip(at, "Middle Brook")["tooltip"].endswith("No flood status")


@pytest.mark.parametrize("level, status, color", [
    (9.9, "No flooding", GREEN), (10.0, "Action stage", ORANGE), (11.0, "Minor flooding", RED),
    (12.5, "Moderate flooding", RED), (13.0, "Major flooding", RED),
])
def test_ac_9_p6_flood_category_stage_table(rsps, monkeypatch, level, status, color):
    record = make_places()
    record["gauges"][0]["stages"] = {"action": 10.0, "minor": 11.0, "moderate": 12.0, "major": 13.0}
    patch_data(monkeypatch, record, make_inputs(gauge_levels={"BDBN4": level}))
    at = open_map()
    row = gauge_tip(at, "Middle Brook at Bound Brook")
    assert row["tooltip"].split("\n")[2] == status and row["color"] == color


# P3: unknown spot risk.

def test_ac_9_p3_alerts_unavailable_low_spots_are_unknown(rsps, monkeypatch):
    patch_data(monkeypatch, make_places(),
               make_inputs(gauge_levels={"BDBN4": 10.2}, alerts_unavailable=True))
    at = open_map()
    rows = layers(at)["flood_spots"]
    raised = [r for r in rows if r["color"] == ORANGE]
    unknown = [r for r in rows if r["color"] == UNKNOWN_GRAY]
    assert len(raised) == 3 and all(r["tooltip"].endswith("Elevated risk") for r in raised)
    assert len(unknown) == 2 and all(r["tooltip"].endswith("Risk unknown") for r in unknown)
    assert UNKNOWN_GRAY != BLUE_GRAY


def test_ac_9_p3_spot_without_assessed_level_is_unknown(rsps, monkeypatch):
    record = make_places()
    patch_data(monkeypatch, record)
    real = assessment_loader.load_assessment

    def drop_one(loc):
        rec, inputs, G, a = real(loc)
        a.spot_levels.pop("spot:s2025")
        return rec, inputs, G, a

    monkeypatch.setattr(assessment_loader, "load_assessment", drop_one)
    at = open_map()
    rows = {r["tooltip"].split("\n")[0]: r for r in layers(at)["flood_spots"]}
    assert rows["US 22 at Mountain Ave closed"]["color"] == UNKNOWN_GRAY
    assert rows["US 22 at Mountain Ave closed"]["tooltip"].endswith("Risk unknown")
    assert rows["Old York Rd flooded"]["color"] == BLUE_GRAY


# P4: the map and the forecast page agree on a gauge's flood status.

def test_ac_9_p4_map_and_forecast_agree_at_action_stage(rsps, monkeypatch):
    loc = resolve_zip("08807")
    patch_places = fixture_08807()
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: patch_places)
    mock_nws(rsps, loc)
    near = shift_nwps(load_fixture("nwps_near_08807.json"))
    bdbn4 = next(g for g in near["gauges"] if g["lid"] == "BDBN4")
    bdbn4["status"]["observed"].update(primary=10.2, floodCategory="action")
    rsps.get(NWPS_GAUGES_URL, json=near)
    rsps.add_callback(responses.GET, STAGEFLOW_URL, callback=stageflow_callback(), content_type="application/json")
    at = open_map()
    row = gauge_tip(at, "Middle Brook at Bound Brook")
    assert row["tooltip"] == "Middle Brook at Bound Brook\nLevel: 10.20 ft\nAction stage" and row["color"] == ORANGE
    at.switch_page("app_pages/forecast.py").run()
    assert not at.exception, at.exception
    cards = [c for c in at.main.markdown if c.value == "**Middle Brook at Bound Brook**"]
    assert cards
    badges = [m.value for m in at.main.markdown if "-badge[" in m.value]
    assert ":orange-badge[:material/warning: Action stage]" in badges


def test_ac_9_p4_nwps_category_wins_over_stages(rsps, monkeypatch):
    """The forecast page's source (the NWPS category of the reading) is used when it is known."""
    patch_data(monkeypatch, make_places(),
               make_inputs(gauge_levels={"BDBN4": 4.0}, gauge_categories={"BDBN4": "minor"}))
    at = open_map()
    assert gauge_tip(at, "Middle Brook at Bound Brook")["tooltip"].endswith("Minor flooding")


# P5: one caption per failed source.

def test_ac_9_p5_spots_failed_shelters_ok(rsps, monkeypatch):
    record = make_places(spots=[])
    record["status"] = {"flood_spots": places.FAILED, "shelters": places.OK}
    patch_data(monkeypatch, record)
    at = open_map()
    assert HISTORY_UNAVAILABLE in captions(at) and SHELTERS_UNAVAILABLE not in captions(at)
    assert "0 flood spots · 3 gauges · 2 open shelters" in captions(at)


def test_ac_9_p5_shelters_failed_spots_ok(rsps, monkeypatch):
    record = make_places(shelters=[])
    record["status"] = {"flood_spots": places.OK, "shelters": places.FAILED}
    patch_data(monkeypatch, record)
    at = open_map()
    assert SHELTERS_UNAVAILABLE in captions(at) and HISTORY_UNAVAILABLE not in captions(at)
    assert "5 flood spots · 3 gauges · shelters unavailable" in captions(at)
    assert not any("0 open shelters" in c for c in captions(at))


# P6: remaining gaps.

def test_ac_9_p6_script_remark_survives_verbatim(rsps, monkeypatch):
    spot = dict(SPOT_2009, id="xss", remark="<script>{lat}</script> flooded")
    patch_data(monkeypatch, make_places(spots=[spot]))
    at = open_map()
    assert layers(at)["flood_spots"][0]["tooltip"].startswith("<script>{lat}</script> flooded\n")
    assert "html" not in json.loads(decks(at)[0].proto.tooltip)


def test_ac_9_p6_load_places_failure_shows_unavailable(rsps, monkeypatch):
    def boom(loc):
        raise RuntimeError("places down")

    monkeypatch.setattr(assessment_loader, "load_places", boom)
    at = open_map()
    assert [e.value for e in at.main.error] == ["Live data unavailable"]
    assert not decks(at)


def test_ac_9_p6_zip_lookup_failure_shows_lookup_unavailable(rsps, monkeypatch):
    def boom(zip_code):
        raise RuntimeError("geocoder down")

    monkeypatch.setattr(location, "resolve_zip", boom)
    at = open_map()
    assert "ZIP lookup unavailable" in [e.value for e in at.main.error]
    assert not decks(at)


# P7: tooltip lines are truncated and never blank.

def test_ac_9_p7_tooltip_lines_truncated_and_not_blank(rsps, monkeypatch):
    long = "Very Long Name " * 10
    record = make_places(spots=[dict(SPOT_2009, date=None)])
    record["gauges"][0]["name"] = long
    record["shelters"] = [dict(SHELTER_NEAR, name=long, address=long, city="", state=None)]
    patch_data(monkeypatch, record)
    at = open_map()
    by_id = layers(at)
    for row in by_id["flood_spots"] + by_id["gauges"] + by_id["shelters"]:
        lines = row["tooltip"].split("\n")
        assert all(line.strip() and len(line) <= 80 for line in lines), lines
    assert by_id["flood_spots"][0]["tooltip"] == "Old York Rd flooded\nLow risk"
    assert len(by_id["shelters"][0]["tooltip"].split("\n")) == 2
    assert by_id["gauges"][0]["tooltip"].split("\n")[0].endswith("…")
