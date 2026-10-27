"""Story 5 (CAP-4): the Get help page, its one-tap call links, shelters and offline fallback.

Every test runs the real app shell with AppTest; HTTP is mocked (see conftest), or blocked
outright for the network-off case.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
import requests
import responses
import streamlit
from streamlit.testing.v1 import AppTest

import assessment_loader
import config
from data import location, places
from data.location import resolve_zip
from tests.conftest import mock_places_sources, places_fixture, places_urls
from tests.test_ac_3_9_graph_page import mock_all
from tests.test_ac_4_flood_alerts_page import fail_alerts_no_cache, set_alerts
from ui_shared import ASK_FOR_ZIP, SEE_ALERTS

APP = "../app.py"
PAGE = "app_pages/help.py"
FORECAST = "app_pages/forecast.py"
ALERTS = "app_pages/alerts.py"
INTEL = "app_pages/intelligence.py"

NATIONAL = {"911": "tel:911", "211": "tel:211", "1-800-621-3362": "tel:18006213362",
            "1-800-733-2767": "tel:18007332767"}
OFFICES = {"08807": ("609-261-6600", "tel:6092616600"), "10001": ("631-924-0517", "tel:6319240517")}
OKX_OFFICE = {"@type": "GovernmentOrganization", "id": "OKX", "name": "New York, NY", "telephone": "631-924-0517"}
NO_SHELTERS = "No open shelters within 25 miles right now"

SHELTER_NEAR = {"id": "near", "name": "Bridgewater-Raritan High School", "address": "600 Garretson Rd",
                "city": "Bridgewater", "state": "NJ", "zip": "08807", "status": "OPEN", "org": "ARC",
                "lat": 40.6012, "lon": -74.6573, "distance_mi": 3.1}
SHELTER_FAR = {"id": "far", "name": "Somerville Armory", "address": "1 Armory Dr", "city": "Somerville",
               "state": "NJ", "zip": "08876", "status": "OPEN", "org": "ARC",
               "lat": 40.5701, "lon": -74.4502, "distance_mi": 9.8}
SHELTER_CLOSED = {"id": "closed", "name": "Closed Church Hall", "address": "5 Main St", "city": "Raritan",
                  "state": "NJ", "zip": "08869", "status": "CLOSED", "org": "ARC",
                  "lat": 40.5698, "lon": -74.6329, "distance_mi": 1.5}
SHELTER_NO_DISTANCE = {"id": "nodist", "name": "Unknown Distance Center", "address": "9 Elm St",
                       "city": "Manville", "state": "NJ", "zip": "08835", "status": None, "org": "ARC",
                       "lat": 40.5409, "lon": -74.5877, "distance_mi": None}
SHELTER_NAMES = ("Bridgewater-Raritan High School", "Somerville Armory", "Closed Church Hall",
                 "Unknown Distance Center")


# --- helpers -----------------------------------------------------------------------

def mock_places_10001(rsps):
    """Every places source for 10001, from fixtures: the OKX office, and empty history and shelters."""
    urls = places_urls()
    rsps.get(f"{config.NWS_BASE}/offices/OKX", json=OKX_OFFICE)
    rsps.get(urls["lsr"], json={"data": []})
    rsps.get(urls["vtec"], json={"events": []})
    rsps.get(urls["disasters"], json={"DisasterDeclarationsSummaries": []})
    rsps.get(urls["nfip"], json={"NfipClaims": []})
    rsps.get(urls["shelters"], json=places_fixture("fema_shelters_empty_08807.json"))
    rsps.add(responses.GET, re.compile(re.escape(urls["gauge"]) + r"\w+$"), json={"flood": {"categories": {}}})


def mock_zip(rsps, zip_code="08807", **live_kw):
    """Places sources plus live NWS and gauge data for ``zip_code``."""
    mock_all(rsps, zip_code, **live_kw)
    if zip_code == "10001":
        mock_places_10001(rsps)


def seed_places(zip_code="08807", shelters=None, drop_office=False, shelters_status=None) -> dict:
    """Build the ZIP's places record from fixtures and save it as the local copy.

    ``shelters`` replaces the (empty) shelter list; ``drop_office`` removes the NWS office line;
    ``shelters_status`` overrides the shelters section status.
    The page then reads this cached copy without calling any places source.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as r:
        loc = resolve_zip(zip_code)
        if zip_code == "08807":
            mock_places_sources(r, loc)
        else:
            mock_zip(r, zip_code)
        record = places.get_places(loc)
    if shelters is not None:
        record["shelters"] = [dict(s) for s in shelters]
    if drop_office:
        record["help"] = [h for h in record["help"] if h.get("kind") != "nws_office"]
    if shelters_status is not None:
        record["status"]["shelters"] = shelters_status
    record.pop("stale", None)
    places.write_local(record)
    return record


def open_help(zip_code="08807"):
    # Open straight on the help page (its url_path matches its filename), not the default page.
    at = AppTest.from_file(APP, default_timeout=30).switch_page(PAGE)
    at.run()
    assert not at.exception, at.exception
    if zip_code != "08807":
        at.text_input(key="zip").set_value(zip_code).run()
        assert not at.exception, at.exception
    return at


def links(at) -> list[tuple[str, str]]:
    """(label, url) of every link button, in page order."""
    return [(b.proto.label, b.proto.url) for b in at.get("link_button")]


def call_links(at) -> list[tuple[str, str]]:
    """The contact list's call links (not the "Call 911 now" shortcut under a high-risk banner)."""
    return [(label, url) for label, url in links(at) if url.startswith("tel:") and label != "Call 911 now"]


def map_links(at) -> list[tuple[str, str]]:
    return [(label, url) for label, url in links(at) if url.startswith("https://www.google.com/maps/")]


def section_order(at) -> list[str]:
    return [s.value for s in at.subheader if s.value in ("Call for help", "Open shelters")]


RISK_TITLES = ("Low risk", "Elevated risk", "High risk")


def banner_kinds(at) -> list[tuple[str, str]]:
    return [(kind, el.proto.title) for kind in ("success", "warning", "error") for el in getattr(at, kind)
            if el.proto.title in RISK_TITLES]


def page_links(at):
    """Page links drawn by the help page (not the shell's breaking-alert banner link)."""
    return [p for p in at.page_link if p.proto.label != SEE_ALERTS]


def warning_titles(at) -> list[str]:
    return [w.proto.title for w in at.warning if w.proto.title not in RISK_TITLES]


def shelter_names(at) -> list[str]:
    return [m.value.strip("*") for m in at.markdown if m.value.strip("*") in SHELTER_NAMES]


# --- AC 5.1: contacts as one-tap tel: links ------------------------------------------

@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_ac_5_1_national_lines_and_nws_office_as_tel_links(rsps, zip_code):
    mock_zip(rsps, zip_code)
    at = open_help(zip_code)
    assert at.text_input(key="zip").value == zip_code
    office_number, office_url = OFFICES[zip_code]
    expected = [(f"Call {n}", u) for n, u in NATIONAL.items()] + [(f"Call {office_number}", office_url)]
    assert call_links(at) == expected
    md = [m.value for m in at.markdown]
    assert "1-800-621-3362" in md and office_number in md  # display text keeps the dashes
    assert any("forecast office" in m for m in md)


def test_ac_5_1_national_lines_carry_verified_caption(rsps):
    mock_zip(rsps)
    at = open_help()
    captions = [c.value for c in at.caption]
    assert captions.count("Verified Oct 25, 2026") == 4  # the NWS office line has no verified date


def test_ac_5_1_normal_calm_no_shelters(rsps):
    """Matrix row Normal: 5 contacts, numbers first, then the none-open message; no banner when low."""
    mock_zip(rsps)
    at = open_help()
    assert "Bridgewater, NJ (08807)" in [s.value for s in at.subheader]
    assert len(call_links(at)) == 5
    assert section_order(at) == ["Call for help", "Open shelters"]
    assert [i.value for i in at.info] == [NO_SHELTERS]
    assert not banner_kinds(at)
    assert not map_links(at)


def test_ac_5_1_no_sms_links(rsps):
    mock_zip(rsps)
    at = open_help()
    assert not [u for _, u in links(at) if u.startswith("sms:")]


def test_ac_5_1_no_nws_office_still_shows_national_lines(rsps):
    mock_zip(rsps)
    seed_places(drop_office=True)
    at = open_help()
    assert call_links(at) == [(f"Call {n}", u) for n, u in NATIONAL.items()]


def test_ac_5_1_zip_shared_with_other_pages(rsps):
    mock_zip(rsps)
    mock_zip(rsps, "10001")
    at = open_help("10001")
    assert "New York, NY (10001)" in [s.value for s in at.subheader]
    assert at.session_state["current_zip"] == "10001"
    at.switch_page(ALERTS).run()
    assert at.text_input(key="zip").value == "10001"
    at.text_input(key="zip").set_value("08807").run()
    at.switch_page(PAGE).run()
    assert at.text_input(key="zip").value == "08807"


@pytest.mark.parametrize("bad", ["abcde", "00000"])
def test_ac_5_1_invalid_zip_message_not_shared(rsps, bad):
    mock_zip(rsps)
    at = open_help()
    at.text_input(key="zip").set_value(bad).run()
    assert not at.exception, at.exception
    assert [e.value for e in at.sidebar.error] == ["Enter a valid 5-digit US ZIP"]
    assert [e.value for e in at.error] == ["Enter a valid 5-digit US ZIP"]
    assert [i.value for i in at.main.info] == [ASK_FOR_ZIP]
    assert not links(at)
    assert at.session_state["current_zip"] == "08807"
    at.switch_page(FORECAST).run()
    assert at.text_input(key="zip").value == bad and [i.value for i in at.main.info] == [ASK_FOR_ZIP]
    at.text_input(key="zip").set_value("08807").run()
    assert not at.error and at.session_state["current_zip"] == "08807"


def test_ac_5_1_lookup_unavailable_message(rsps, monkeypatch):
    mock_zip(rsps)
    at = open_help()

    def broken():
        raise OSError("pgeocode data unavailable")

    monkeypatch.setattr(location, "_nominatim", broken)
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == ["ZIP lookup unavailable"]
    assert not links(at)


def test_ac_5_1_menu_order(rsps, monkeypatch):
    seen = []
    real = streamlit.navigation

    def spy(pages, **kw):
        seen.append(list(pages))
        return real(pages, **kw)

    monkeypatch.setattr(streamlit, "navigation", spy)
    mock_zip(rsps)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    pages = seen[-1]
    assert [p.title for p in pages] == ["View environmental intelligence", "Search weather forecast",
                                        "Flood map", "Flood alerts", "Local news", "Get phone alerts",
                                        "Get help"]
    assert [p._default for p in pages] == [True, False, False, False, False, False, False]  # intelligence
    assert pages[6].url_path == "help" and pages[6].icon == ":material/call:"


# --- AC 5.2: renders from the cached places file with the network off -----------------

@pytest.fixture
def network_off(monkeypatch):
    calls = []

    def blocked(self, request, *args, **kwargs):
        calls.append(request.url)
        raise requests.ConnectionError(f"network off: {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked)
    return calls


@pytest.mark.parametrize("zip_code", ["08807", "10001"])
def test_ac_5_2_network_off_renders_from_cached_places(zip_code, network_off):
    seed_places(zip_code, shelters=[SHELTER_NEAR])
    network_off.clear()  # count only the page's own attempts for this ZIP
    at = open_help()
    if zip_code != "08807":
        network_off.clear()  # the first run loads the default ZIP, which has no cached copy here
        at.text_input(key="zip").set_value(zip_code).run()
        assert not at.exception, at.exception
    office_number, office_url = OFFICES[zip_code]
    assert call_links(at) == [(f"Call {n}", u) for n, u in NATIONAL.items()] + [(f"Call {office_number}", office_url)]
    assert len(map_links(at)) == 1
    assert not at.error
    # The page did try live data (and was blocked), but never called a places source.
    assert any(u.startswith(config.NWS_BASE) for u in network_off), network_off
    places_sources = (config.IEM_BASE, config.OPENFEMA_BASE, config.FEMA_SHELTERS_URL,
                      f"{config.NWS_BASE}/offices")
    assert all(not u.startswith(places_sources) for u in network_off), network_off


def test_ac_5_2_live_data_down_default_order_and_caption(rsps, monkeypatch, caplog):
    """Matrix row Live data down: contacts and shelters in default order, a caption, logged."""
    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(("Flash Flood Warning", "Severe")))
    seed_places(shelters=[SHELTER_NO_DISTANCE, SHELTER_FAR, SHELTER_NEAR])

    def boom(loc):
        raise RuntimeError("live inputs down")

    monkeypatch.setattr(assessment_loader, "load_assessment", boom)
    with caplog.at_level("ERROR"):
        at = open_help()
    assert "Live risk unavailable" in [c.value for c in at.caption]
    assert not banner_kinds(at)
    assert section_order(at) == ["Call for help", "Open shelters"]
    assert len(call_links(at)) == 5
    # Nearest first; the shelter with no distance goes last and gets no distance caption.
    assert shelter_names(at) == ["Bridgewater-Raritan High School", "Somerville Armory", "Unknown Distance Center"]
    assert [c.value for c in at.caption if c.value.endswith("mi away")] == ["3.1 mi away", "9.8 mi away"]
    assert len(map_links(at)) == 3
    assert not at.page_link
    assert any("Live risk could not be assessed" in r.getMessage() for r in caplog.records)


def test_ac_5_2_places_load_failure_falls_back_to_fixed_lines(rsps, monkeypatch):
    mock_zip(rsps)
    assessed = []

    def boom(loc):
        raise RuntimeError("places bug")

    def spy(loc):
        assessed.append(loc.zip)
        raise RuntimeError("should not be called")

    monkeypatch.setattr(assessment_loader, "load_places", boom)
    monkeypatch.setattr(assessment_loader, "load_assessment", spy)
    at = open_help()
    assert call_links(at) == [(f"Call {n}", u) for n, u in NATIONAL.items()]
    # The fallback record's shelter lookup failed: say so, never "no open shelters".
    assert not [i.value for i in at.info if i.value == NO_SHELTERS]
    assert warning_titles(at) == ["Shelter list unavailable"]
    assert [w.value for w in at.warning] == ["Shelters could not be checked. Call 211 for shelter information."]
    assert "Live risk unavailable" in [c.value for c in at.caption]
    assert not assessed  # no live refetch for the fallback record


def test_ac_5_2_failed_shelter_lookup_is_not_none_open(rsps):
    mock_zip(rsps)
    seed_places(shelters=[], shelters_status="failed")
    at = open_help()
    assert warning_titles(at) == ["Shelter list unavailable"]
    assert NO_SHELTERS not in [i.value for i in at.info]
    assert not any(c.value.startswith("Shelter list as of") for c in at.caption)
    assert len(call_links(at)) == 5


@pytest.mark.parametrize("case", ["carried", "stale"])
def test_ac_5_2_stale_or_carried_shelters_warn(rsps, monkeypatch, case):
    mock_zip(rsps)
    if case == "carried":
        record = seed_places(shelters=[SHELTER_NEAR], shelters_status="carried")
    else:
        # get_places marks a record stale when its rebuild failed and the old version is served.
        record = seed_places(shelters=[SHELTER_NEAR])
        monkeypatch.setattr(assessment_loader, "load_places", lambda loc: dict(places.read_local(loc.zip), stale=True))
    at = open_help()
    assert warning_titles(at) == ["Shelter list may be out of date"]
    assert shelter_names(at) == ["Bridgewater-Raritan High School"]
    as_of = [c.value for c in at.caption if c.value.startswith("Shelter list as of")]
    assert as_of == [f"Shelter list as of {help_page_fmt(record['built_at'])}"]
    assert as_of[0].endswith(" ET")


def help_page_fmt(iso: str) -> str:
    from zoneinfo import ZoneInfo
    from datetime import datetime

    return datetime.fromisoformat(iso).astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p ET")


def test_ac_5_2_fresh_shelters_show_as_of_without_warning(rsps):
    mock_zip(rsps)
    seed_places(shelters=[SHELTER_NEAR])
    at = open_help()
    assert not warning_titles(at)
    assert any(c.value.startswith("Shelter list as of") for c in at.caption)


def test_ac_5_2_alerts_unavailable_caption(rsps):
    mock_zip(rsps)
    fail_alerts_no_cache(rsps)
    at = open_help()
    assert "NWS alerts could not be checked" in [c.value for c in at.caption]
    assert len(call_links(at)) == 5


# --- AC 5.3: shelters nearest first; above the contacts at high risk -------------------

def test_ac_5_3_low_risk_contacts_then_shelters_nearest_first(rsps):
    mock_zip(rsps)
    seed_places(shelters=[SHELTER_FAR, SHELTER_NEAR])  # stored out of order
    at = open_help()
    assert not banner_kinds(at)
    assert section_order(at) == ["Call for help", "Open shelters"]
    assert shelter_names(at) == ["Bridgewater-Raritan High School", "Somerville Armory"]
    captions = [c.value for c in at.caption]
    assert captions.index("3.1 mi away") < captions.index("9.8 mi away")
    assert map_links(at) == [
        ("Open in maps", "https://www.google.com/maps/search/?api=1&query=40.6012,-74.6573"),
        ("Open in maps", "https://www.google.com/maps/search/?api=1&query=40.5701,-74.4502"),
    ]
    assert "600 Garretson Rd, Bridgewater, NJ" in [m.value for m in at.markdown]
    assert not at.info
    assert not at.page_link  # no banner, no alerts shortcut at low risk


def test_ac_5_3_closed_shelters_are_not_listed(rsps):
    mock_zip(rsps)
    seed_places(shelters=[SHELTER_CLOSED, SHELTER_NEAR])
    at = open_help()
    assert shelter_names(at) == ["Bridgewater-Raritan High School"]
    assert "Status: Open" in [c.value for c in at.caption]
    assert len(map_links(at)) == 1


def test_ac_5_3_only_closed_shelters_shows_none_open(rsps):
    mock_zip(rsps)
    seed_places(shelters=[SHELTER_CLOSED])
    at = open_help()
    assert not shelter_names(at)
    assert [i.value for i in at.info] == [NO_SHELTERS]


def test_ac_5_3_high_risk_banner_and_shelters_first(rsps):
    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(("Flash Flood Warning", "Severe")))
    seed_places(shelters=[SHELTER_FAR, SHELTER_NEAR])
    at = open_help()
    assert banner_kinds(at) == [("error", "High risk")]
    assert [e for e in at.error if e.proto.title][0].value.startswith("Flash Flood Warning covers Bridgewater, NJ")
    assert section_order(at) == ["Open shelters", "Call for help"]
    assert shelter_names(at) == ["Bridgewater-Raritan High School", "Somerville Armory"]
    # Under the banner: a 911 shortcut first, and a link to the alerts page.
    assert links(at)[0] == ("Call 911 now", "tel:911")
    assert [(p.proto.label, p.proto.page) for p in page_links(at)] == [
        ("See flood alerts and roads to avoid", "alerts")]
    # Shelter map links come before every contact-list call link.
    urls = [u for label, u in links(at) if label != "Call 911 now"]
    assert max(i for i, u in enumerate(urls) if u.startswith("https://")) < min(
        i for i, u in enumerate(urls) if u.startswith("tel:"))


def test_ac_5_3_elevated_banner_keeps_contacts_first(rsps):
    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(("Flood Watch", "Moderate")))
    seed_places(shelters=[SHELTER_NEAR])
    at = open_help()
    assert banner_kinds(at) == [("warning", "Elevated risk")]
    assert section_order(at) == ["Call for help", "Open shelters"]
    assert "Call 911 now" not in [label for label, _ in links(at)]
    assert [p.proto.label for p in page_links(at)] == ["See flood alerts and roads to avoid"]


def test_ac_5_3_high_risk_no_shelters_message(rsps):
    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(("Flash Flood Warning", "Severe")))
    at = open_help()
    assert banner_kinds(at) == [("error", "High risk")]
    assert [i.value for i in at.info] == [NO_SHELTERS]
    assert len(call_links(at)) == 5


# --- AC 5.4: every fixed national number has a verified-on date -------------------------

def test_ac_5_4_fixed_help_has_iso_verified_on():
    assert len(config.FIXED_HELP) == 4
    for entry in config.FIXED_HELP:
        value = entry.get("verified_on")
        assert value, f"{entry['name']} has no verified_on"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), value
        date.fromisoformat(value)


def test_ac_5_4_fixed_help_numbers_match_directory():
    assert [h["number"] for h in config.FIXED_HELP] == list(NATIONAL)
