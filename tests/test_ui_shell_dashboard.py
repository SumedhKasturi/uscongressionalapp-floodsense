"""UI refresh: the one sidebar ZIP box, its risk badge, the shell's ?zip= link and the forecast dashboard.

Every test runs the real app shell with AppTest; all HTTP is mocked (see conftest).
"""

import json

import pytest
from streamlit.testing.v1 import AppTest

import assessment_loader
from data import location
from data.location import INVALID_ZIP_MESSAGE, resolve_zip
from graph import build
from tests.conftest import NWPS_GAUGES_URL, load_fixture, mock_live, mock_nws
from tests.test_ac_4_flood_alerts_page import cards, fail_alerts_no_cache, mock_page
from tests.test_ac_5_help_page import mock_zip
from ui_shared import ASK_FOR_ZIP, BREAKING_ICON

APP = "../app.py"
FORECAST = "app_pages/forecast.py"
ALERTS = "app_pages/alerts.py"
HELP = "app_pages/help.py"
INTEL = "app_pages/intelligence.py"
PHONE = "app_pages/phone_alerts.py"
PAGES = (ALERTS, HELP, INTEL, FORECAST)
PLACES = {"08807": "Bridgewater, NJ (08807)", "10001": "New York, NY (10001)"}
METRIC_LABELS = ["Flood risk", "Temperature now", "Max rain chance next 12 h", "Active alerts"]


def open_app(query_zip=None, page=FORECAST):
    """Run the shell on ``page`` (the forecast page by default; the app itself opens on intelligence)."""
    at = AppTest.from_file(APP, default_timeout=30)
    if page is not None:
        at.switch_page(page)
    if query_zip is not None:
        at.query_params["zip"] = query_zip
    at.run()
    assert not at.exception, at.exception
    return at


def go(at, page):
    at.switch_page(page).run()
    assert not at.exception, (page, at.exception)
    return at


def sidebar_badges(at) -> list[str]:
    return [m.value for m in at.sidebar.markdown if "-badge[" in m.value]


def kpis(at) -> dict:
    return {m.label: m.value for m in at.metric if m.label in METRIC_LABELS}


def gauge_cards(at):
    """Bordered containers that hold a "Level" metric (one per gauge card)."""
    return [c for c in cards(at) if any(m.label == "Level" for m in c.metric)]


# --- One ZIP box, in the sidebar, for every page ---------------------------------------------

def test_sidebar_zip_is_the_only_zip_box_on_each_page(rsps):
    mock_zip(rsps)
    at = open_app()
    for page in PAGES:
        go(at, page)
        assert [t.key for t in at.text_input if t.key == "zip"] == ["zip"]
        assert [t.key for t in at.sidebar.text_input] == ["zip"]
        assert not [t for t in at.main.text_input if t.label == "ZIP code"], page


def test_sidebar_zip_shared_across_pages(rsps):
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_app()
    assert at.text_input(key="zip").value == "08807"
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert at.session_state["current_zip"] == "10001"
    for page in (ALERTS, HELP, INTEL):
        go(at, page)
        assert at.text_input(key="zip").value == "10001"
        assert PLACES["10001"] in [s.value for s in at.subheader], page
        assert "**New York, NY**" in [m.value for m in at.sidebar.markdown]
    # Change it on the graph page; the forecast follows.
    at.text_input(key="zip").set_value("08807").run()
    go(at, FORECAST)
    assert at.text_input(key="zip").value == "08807"
    assert "Stream gauges near Bridgewater, NJ" in [h.value for h in at.header]


def test_phone_form_zip_prefilled_from_sidebar_zip(rsps, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_SECRET", "test-topic-secret-123")
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_app()
    at.text_input(key="zip").set_value("10001").run()
    go(at, PHONE)
    assert at.text_input(key="phone_zip").value == "10001"  # the form keeps its own ZIP field
    assert at.text_input(key="zip").value == "10001"


@pytest.mark.parametrize("bad", ["abcde", "00000", "123"])
def test_invalid_sidebar_zip_message_and_pages_ask_for_a_zip(rsps, bad):
    mock_zip(rsps)
    at = open_app()
    at.text_input(key="zip").set_value(bad).run()
    assert not at.exception, at.exception
    for page in PAGES:
        go(at, page)
        assert [e.value for e in at.sidebar.error] == [INVALID_ZIP_MESSAGE], page
        assert not at.main.error, page
        assert [i.value for i in at.main.info] == [ASK_FOR_ZIP], page
        assert not at.sidebar.markdown  # no place name or badge for a bad entry
        assert at.session_state["current_zip"] == "08807"
        assert at.text_input(key="zip").value == bad


def test_sidebar_lookup_outage_is_a_caption_not_a_second_error(rsps, monkeypatch):
    mock_zip(rsps)
    at = open_app()

    def broken():
        raise OSError("pgeocode data unavailable")

    monkeypatch.setattr(location, "_nominatim", broken)
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert "ZIP lookup unavailable" in [c.value for c in at.sidebar.caption]
    assert [e.value for e in at.error] == ["ZIP lookup unavailable"]  # the page's own message only


# --- Sidebar risk badge ----------------------------------------------------------------------

def test_sidebar_shows_place_and_low_risk_badge(rsps):
    mock_zip(rsps)
    at = open_app()
    assert "**Bridgewater, NJ**" in [m.value for m in at.sidebar.markdown]
    assert sidebar_badges(at) == [":green-badge[:material/check_circle: Low risk]"]


def test_sidebar_high_risk_badge_matches_alerts_banner(rsps):
    mock_page(rsps, ("Flash Flood Warning", "Severe"))
    at = go(open_app(), ALERTS)
    assert sidebar_badges(at) == [":red-badge[:material/emergency_home: High risk]"]
    assert [e.proto.title for e in at.main.error if e.icon != BREAKING_ICON] == ["High risk"]


def test_sidebar_elevated_risk_badge_is_orange(rsps):
    mock_page(rsps, ("Flood Watch", "Severe"))
    at = open_app()
    assert sidebar_badges(at) == [":orange-badge[:material/warning: Elevated risk]"]
    assert kpis(at)["Flood risk"] == "Elevated"


@pytest.mark.parametrize("page", PAGES)
def test_sidebar_badge_failure_shows_risk_unavailable_and_page_still_renders(rsps, monkeypatch, page):
    mock_zip(rsps)

    def boom(*args, **kwargs):
        raise RuntimeError("graph bug")

    monkeypatch.setattr(build, "build_graph", boom)
    at = go(open_app(), page)
    assert sidebar_badges(at) == [":gray-badge[:material/help: Risk unavailable]"]
    assert "**Bridgewater, NJ**" in [m.value for m in at.sidebar.markdown]
    assert not at.sidebar.error


def test_sidebar_badge_skips_assessment_when_no_places_source_answered(rsps, monkeypatch):
    mock_live(rsps, resolve_zip("08807"))
    called = []
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: {"all_sources_failed": True})
    monkeypatch.setattr(assessment_loader, "load_assessment", lambda loc: called.append(loc))
    at = open_app()
    assert sidebar_badges(at) == [":gray-badge[:material/help: Risk unavailable]"]
    assert kpis(at)["Flood risk"] == "Unavailable"
    assert not called


def test_alerts_unknown_never_shows_green_badge(rsps):
    mock_page(rsps)
    fail_alerts_no_cache(rsps)
    at = open_app()
    assert sidebar_badges(at) == [":gray-badge[:material/help: Risk unavailable]"]


# --- ?zip= push link, handled by the shell ---------------------------------------------------

def test_query_zip_sets_current_zip_on_any_page(rsps):
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_app(query_zip="10001")  # opens on the forecast page
    assert at.session_state["current_zip"] == "10001"
    assert at.text_input(key="zip").value == "10001"
    assert "Stream gauges near New York, NY" in [h.value for h in at.header]
    go(at, HELP)
    assert PLACES["10001"] in [s.value for s in at.subheader]


def test_query_zip_used_once_per_link_value(rsps):
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_app(query_zip="10001")
    at.text_input(key="zip").set_value("08807").run()
    go(at, ALERTS)  # same link still in the URL: the typed ZIP wins
    assert at.session_state["current_zip"] == "08807"
    assert at.text_input(key="zip").value == "08807"
    at.query_params["zip"] = "08807"  # another push link...
    at.run()
    at.query_params["zip"] = "10001"  # ...then a new one for 10001 again: a new link value is used
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["current_zip"] == "10001"
    assert at.text_input(key="zip").value == "10001"
    assert PLACES["10001"] in [s.value for s in at.subheader]


@pytest.mark.parametrize("bad", ["abcde", "00000", "1234", "123456"])
def test_bad_query_zip_is_ignored(rsps, bad):
    mock_zip(rsps)
    at = open_app(query_zip=bad)
    assert at.session_state["current_zip"] == "08807"
    assert at.text_input(key="zip").value == "08807"
    assert not at.error


# --- Forecast dashboard ----------------------------------------------------------------------

@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_forecast_metric_cards(rsps, zip_code):
    mock_zip(rsps, zip_code)
    if zip_code != "08807":
        mock_zip(rsps)
    at = open_app()
    if zip_code != "08807":
        at.text_input(key="zip").set_value(zip_code).run()
    metrics = [m for m in at.metric if m.label in METRIC_LABELS]
    assert [m.label for m in metrics] == METRIC_LABELS
    assert all(m.proto.show_border for m in metrics)
    assert all(not m.delta for m in metrics)  # delta-free, so the sibling cards line up
    periods = load_fixture(f"nws_hourly_{zip_code}.json")["properties"]["periods"]
    values = kpis(at)
    assert values["Flood risk"] == "Low"
    assert values["Temperature now"] == f"{periods[0]['temperature']}°F"
    rain = [(p.get("probabilityOfPrecipitation") or {}).get("value") for p in periods[:12]]
    assert values["Max rain chance next 12 h"] == f"{max(v for v in rain if v is not None)}%"
    assert values["Active alerts"] == "0"


def test_forecast_metric_cards_count_alerts(rsps):
    mock_page(rsps, ("Flash Flood Warning", "Severe"), ("Coastal Flood Advisory", "Minor"))
    values = kpis(open_app())
    assert values["Active alerts"] == "2"
    assert values["Flood risk"] == "High"


def test_forecast_metric_cards_when_live_data_is_down(rsps):
    at = open_app()  # empty RequestsMock: every request fails
    assert kpis(at) == {"Flood risk": "Unavailable", "Temperature now": "Unavailable",
                        "Max rain chance next 12 h": "Unavailable", "Active alerts": "Unavailable"}


def test_forecast_hourly_chart_and_details_expander(rsps):
    mock_zip(rsps)
    at = open_app()
    [chart] = at.get("vega_lite_chart")
    spec = json.loads(chart.proto.spec)
    assert [layer["mark"]["type"] for layer in spec["layer"]] == ["bar", "line"]
    assert spec["resolve"]["scale"]["y"] == "independent"
    assert all(layer["encoding"]["tooltip"] for layer in spec["layer"])
    assert chart.proto.alt == "Temperature and rain chance for each of the next 12 hours"
    [details] = [e for e in at.expander if e.label == "Hourly details"]
    assert len(details.dataframe[0].value) == 12


def test_forecast_gauge_cards(rsps):
    mock_zip(rsps)
    at = open_app()
    gauge_rows = at.dataframe[-1].value
    found = gauge_cards(at)
    assert len(found) == len(gauge_rows) == 6
    first = found[0]
    assert first.markdown[0].value == "**Peters Brook at Somerville**"
    assert [m.value for m in first.metric] == [gauge_rows.iloc[0]["Level"]]
    assert first.caption[0].value == "1.2 mi away · Status: live"
    badges = [next(m.value for m in c.markdown if "-badge[" in m.value) for c in found]
    by_site = dict(zip(gauge_rows["Site"], badges))
    assert by_site["MNVN4"] == ":green-badge[:material/check_circle: No flooding]"
    assert by_site["MILN4"] == ":red-badge[:material/emergency_home: Minor flooding]"
    assert by_site["SMVN4"] == ":gray-badge[:material/remove: No flood status]"
    [table] = [e for e in at.expander if e.label == "All gauges as a table"]
    assert len(table.dataframe[0].value) == 6


def test_forecast_no_gauge_cards_when_none_found(rsps):
    mock_nws(rsps, resolve_zip("08807"))
    rsps.get(NWPS_GAUGES_URL, json={"gauges": []})
    at = open_app()
    assert not gauge_cards(at)
    assert not [e for e in at.expander if e.label == "All gauges as a table"]
    assert [i.value for i in at.info] == ["No active stream gauges within about 35 miles"]
