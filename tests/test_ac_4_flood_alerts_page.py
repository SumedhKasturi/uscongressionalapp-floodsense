"""Story 4 (CAP-3): the Flood alerts page, its cards, "Why this alert?" chains and stale caption.

Every test runs the real app shell with AppTest; all HTTP is mocked (see conftest).
"""

import copy
from datetime import datetime

import pytest
import requests
import responses
import streamlit
from streamlit.testing.v1 import AppTest

import config
from data import cache, location, places
from data.location import resolve_zip
from graph import build
from tests.conftest import NWPS_GAUGES_URL, load_fixture, shift_nwps
from tests.test_ac_3_1_graph_schema import STAGEFLOW_URL, stageflow_callback
from tests.test_ac_3_9_graph_page import mock_all
from ui_shared import ASK_FOR_ZIP, BREAKING_ICON

APP = "../app.py"
PAGE = "app_pages/alerts.py"
FORECAST = "app_pages/forecast.py"
INTEL = "app_pages/intelligence.py"


# --- helpers -----------------------------------------------------------------------

def set_alerts(*events):
    """alerts_edit for mock_all: one feature per (event, severity)."""

    def edit(body):
        template = body["features"][0]
        body["features"] = []
        for i, (event, severity) in enumerate(events):
            f = copy.deepcopy(template)
            f["id"] = f"{template['id']}-{i}"
            f["properties"].update(event=event, severity=severity, headline=f"{event} until 9 PM EDT")
            body["features"].append(f)

    return edit


def mock_page(rsps, *events, gauge_ft=None):
    """Mock places, NWS (with the given alerts) and gauges. ``gauge_ft`` sets BDBN4's live level."""
    if events:
        mock_all(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(*events))
    else:
        mock_all(rsps)
    if gauge_ft is not None:
        rsps.remove(responses.GET, NWPS_GAUGES_URL)
        body = shift_nwps(load_fixture("nwps_near_08807.json"))
        for g in body["gauges"]:
            if g["lid"] == "BDBN4":
                g["status"]["observed"]["primary"] = gauge_ft
        rsps.get(NWPS_GAUGES_URL, json=body)


def open_page():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    at.switch_page(PAGE).run()
    assert not at.exception, at.exception
    return at


def banner(at):
    """(kind, title, body) of the one risk banner."""
    found = [(kind, el.proto.title, el.value) for kind in ("success", "warning", "error")
             for el in getattr(at, kind) if el.icon != BREAKING_ICON]  # not the shell's breaking-alert banner
    assert len(found) == 1, found
    return found[0]


def cards(at):
    """Bordered containers on the page, as lists of their Markdown/caption texts, in page order."""
    out = []

    def walk(node):
        for c in getattr(node, "children", {}).values():
            if type(c).__name__ == "Block" and c.proto.flex_container.border:
                out.append(c)
            else:
                walk(c)

    walk(at.main)
    return out


def texts(block) -> list[str]:
    """Every Markdown and caption string inside a block, depth first."""
    out = []

    def walk(node):
        for c in getattr(node, "children", {}).values():
            if type(c).__name__ in ("Markdown", "Caption"):
                out.append(c.value)
            walk(c)

    walk(block)
    return out


def expander_texts(block) -> list[str]:
    exp = [e for e in block.expander if e.label == "Why this alert?"]
    assert len(exp) == 1
    return [m.value for m in exp[0].markdown]


def road_items(block) -> list[str]:
    i = texts(block).index("**Affected roads**")
    items = texts(block)[i + 1]
    return [line.removeprefix("- ") for line in items.split("\n")]


def bullet_lines(block, heading="**What to do**", nth=0) -> list[str]:
    t = texts(block)
    idx = [i for i, x in enumerate(t) if x == heading][nth]
    return [line.removeprefix("- ") for line in t[idx + 1].split("\n")]


def flooded_month(item: str) -> datetime:
    return datetime.strptime(item.split("(flooded ")[1][:8], "%b %Y")


# --- AC 4.1: menu item and overall risk ---------------------------------------------

def test_ac_4_1_menu_order_and_default_page(rsps, monkeypatch):
    seen = []
    real = streamlit.navigation

    def spy(pages, **kw):
        seen.append(list(pages))
        return real(pages, **kw)

    monkeypatch.setattr(streamlit, "navigation", spy)
    mock_page(rsps)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    pages = seen[-1]
    assert [p.title for p in pages] == ["View environmental intelligence", "Search weather forecast",
                                        "Flood map", "Flood alerts", "Local news", "Get phone alerts",
                                        "Get help"]
    assert [p._default for p in pages] == [True, False, False, False, False, False, False]  # intelligence
    assert pages[3].url_path == "alerts" and pages[3].icon == ":material/warning:"
    assert pages[1].url_path == "forecast"  # the default page is served at "/"
    # The environmental intelligence page opens by default.
    assert at.text_input(key="zip").value == "08807"
    assert "Graph statistics" in [h.value for h in at.subheader]
    assert "Next 12 hours" not in [h.value for h in at.header]


def test_ac_4_1_page_shows_overall_risk(rsps):
    mock_page(rsps, ("Flash Flood Warning", "Severe"))
    at = open_page()
    assert at.text_input(key="zip").value == "08807"
    assert "Bridgewater, NJ (08807)" in [s.value for s in at.subheader]
    kind, title, body = banner(at)
    assert (kind, title) == ("error", "High risk")
    assert body.startswith("Flash Flood Warning covers Bridgewater, NJ")


def test_ac_4_1_zip_shared_with_other_pages(rsps):
    mock_page(rsps)
    mock_all(rsps, "10001")
    at = open_page()
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert "New York, NY (10001)" in [s.value for s in at.subheader]
    assert banner(at)[1].endswith("risk")
    assert at.session_state["current_zip"] == "10001"
    at.switch_page(FORECAST).run()
    assert at.text_input(key="zip").value == "10001"
    at.text_input(key="zip").set_value("08807").run()
    at.switch_page(PAGE).run()
    assert at.text_input(key="zip").value == "08807"
    at.switch_page(INTEL).run()
    assert at.text_input(key="zip").value == "08807"


@pytest.mark.parametrize("bad", ["abcde", "00000"])
def test_ac_4_1_invalid_zip_message_not_shared(rsps, bad):
    mock_page(rsps)
    at = open_page()
    at.text_input(key="zip").set_value(bad).run()
    assert not at.exception, at.exception
    assert [e.value for e in at.sidebar.error] == ["Enter a valid 5-digit US ZIP"]
    assert [e.value for e in at.error] == ["Enter a valid 5-digit US ZIP"]
    assert [i.value for i in at.main.info] == [ASK_FOR_ZIP]
    assert not cards(at)
    assert at.session_state["current_zip"] == "08807"
    # The shared box keeps the bad entry on the forecast page too, and nothing is fetched for it.
    at.switch_page(FORECAST).run()
    assert at.text_input(key="zip").value == bad and [i.value for i in at.main.info] == [ASK_FOR_ZIP]
    at.text_input(key="zip").set_value("08807").run()
    assert not at.error and at.session_state["current_zip"] == "08807"


def test_ac_4_1_lookup_unavailable_message(rsps, monkeypatch):
    mock_page(rsps)
    at = open_page()

    def broken():
        raise OSError("pgeocode data unavailable")

    monkeypatch.setattr(location, "_nominatim", broken)
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == ["ZIP lookup unavailable"]


def test_ac_4_1_load_failure_shows_message(rsps, monkeypatch):
    mock_page(rsps, ("Flash Flood Warning", "Severe"))

    def boom(*args, **kwargs):
        raise RuntimeError("graph bug")

    monkeypatch.setattr(build, "build_graph", boom)
    at = open_page()
    assert [e.value for e in at.error] == ["Live data unavailable"]
    assert not at.success and not at.warning and not cards(at)


# --- AC 4.2: alert cards, roads, precautions; conditions card -----------------------

def test_ac_4_2_flash_flood_warning_card(rsps):
    mock_page(rsps, ("Flash Flood Warning", "Severe"))
    at = open_page()
    [card] = cards(at)
    t = texts(card)
    assert t[0] == "**Flash Flood Warning**"
    assert any("Severe severity" in x and "badge" in x for x in t)
    assert any("Level: high" in x and ":red-badge" in x for x in t)
    assert "Flash Flood Warning until 9 PM EDT" in t
    roads = road_items(card)
    assert 0 < len(roads) <= 8
    assert all(r.startswith("Avoid ") and "Flash Flood Warning" in r for r in roads)
    months = [flooded_month(r) for r in roads]
    assert months == sorted(months, reverse=True)  # newest first
    assert bullet_lines(card) == places.PRECAUTIONS["flash_flood"]["high"]
    chains = expander_texts(card)
    assert chains[0] == "Flash Flood Warning → Bridgewater, NJ"
    assert all(c.startswith("Flash Flood Warning → ") and c.endswith(" → Bridgewater, NJ") for c in chains[1:])


def test_ac_4_2_roads_capped_at_eight_with_more(rsps):
    mock_page(rsps, ("Flash Flood Warning", "Severe"))
    at = open_page()
    [card] = cards(at)
    spots_in_why = len(expander_texts(card)) - 1  # one chain per recent spot, plus the area chain
    assert spots_in_why > 8
    assert len(road_items(card)) == 8
    assert f"and {spots_in_why - 8} more" in texts(card)


def test_ac_4_2_flood_watch_is_elevated(rsps):
    mock_page(rsps, ("Flood Watch", "Moderate"))
    at = open_page()
    kind, title, _ = banner(at)
    assert (kind, title) == ("warning", "Elevated risk")
    [card] = cards(at)
    t = texts(card)
    assert t[0] == "**Flood Watch**"
    assert any("Level: elevated" in x and ":orange-badge" in x for x in t)
    assert bullet_lines(card) == places.PRECAUTIONS["flood"]["elevated"]
    assert all("Flood Watch" in r for r in road_items(card))


def test_ac_4_2_two_alerts_highest_level_first(rsps):
    mock_page(rsps, ("Flood Watch", "Moderate"), ("Flash Flood Warning", "Severe"))
    at = open_page()
    kind, title, _ = banner(at)
    assert (kind, title) == ("error", "High risk")
    first, second = cards(at)
    assert texts(first)[0] == "**Flash Flood Warning**" and any("Level: high" in x for x in texts(first))
    assert texts(second)[0] == "**Flood Watch**" and any("Level: elevated" in x for x in texts(second))
    # Each card's "Why" chains start at its own alert.
    assert all(c.startswith("Flash Flood Warning → ") for c in expander_texts(first))
    assert all(c.startswith("Flood Watch → ") for c in expander_texts(second))
    assert bullet_lines(second) == places.PRECAUTIONS["flood"]["elevated"]


def test_ac_4_2_gauge_only_conditions_card(rsps):
    mock_page(rsps, gauge_ft=10.2)
    at = open_page()
    kind, title, body = banner(at)
    assert (kind, title) == ("warning", "Elevated risk")
    assert body.startswith("Middle Brook at Bound Brook is 10.2 ft (action stage 10 ft) → Middle Brook elevated")
    assert any(i.value.startswith("No active NWS alerts") for i in at.info)
    assert not any(i.value == "No active alerts" for i in at.info)
    [card] = cards(at)
    t = texts(card)
    assert t[0] == "**River and rain conditions**"
    assert any("Level: elevated" in x for x in t)
    assert "- Middle Brook: elevated" in t
    roads = road_items(card)
    assert roads and all("Middle Brook is elevated" in r for r in roads)
    assert bullet_lines(card) == places.PRECAUTIONS["flood"]["elevated"]
    chains = expander_texts(card)
    assert chains and chains[0].startswith("Middle Brook at Bound Brook 10.2 ft → Middle Brook → ")


def test_ac_4_2_non_flood_alert_in_other_list(rsps):
    mock_page(rsps, ("Wind Advisory", "Moderate"))
    at = open_page()
    kind, title, _ = banner(at)
    assert (kind, title) == ("success", "Low risk")  # the rules, not the advisory, set the level
    assert not cards(at)
    md = [m.value for m in at.markdown]
    i = md.index("**Other alerts**")
    assert md[i + 1].startswith("- **Wind Advisory** · Moderate severity")
    assert not any("Level:" in m for m in md)
    assert any(x.value == "No active flood alerts" for x in at.info)


# --- AC 4.3: readable "Why this alert?" chains --------------------------------------

def test_ac_4_3_why_chain_is_readable(rsps):
    mock_page(rsps, gauge_ft=10.2)
    at = open_page()
    [card] = cards(at)
    exp = card.expander[0]
    assert exp.label == "Why this alert?"
    chain = exp.markdown[0].value
    parts = chain.split(" → ")
    assert parts[:2] == ["Middle Brook at Bound Brook 10.2 ft", "Middle Brook"]
    assert parts[-1] == "Bridgewater, NJ" and len(parts) == 4
    assert not any(p.split(":")[0] in ("gauge", "water", "spot", "area", "alert") for p in parts)  # no node ids
    # Each chain is followed by the rule's explanation sentence.
    assert exp.caption[0].value.startswith("Middle Brook at Bound Brook is 10.2 ft")


# --- AC 4.4: calm day ---------------------------------------------------------------

def test_ac_4_4_calm_day(rsps):
    mock_page(rsps)
    at = open_page()
    kind, title, _ = banner(at)
    assert (kind, title) == ("success", "Low risk")
    assert [i.value for i in at.info] == ["No active alerts"]
    assert not cards(at)
    assert "**Other alerts**" not in [m.value for m in at.markdown]


# --- AC 4.5: stale data caption -----------------------------------------------------

def write_cache(key, data, fetched_at):
    cache._write_atomic(cache.cache_path(key), {"fetched_at": fetched_at, "source_url": "test", "data": data})


def alerts_url():
    loc = resolve_zip("08807")
    return f"{config.NWS_BASE}/alerts/active?point={loc.lat:.4f},{loc.lon:.4f}"


def test_ac_4_5_stale_alerts_caption(rsps):
    mock_page(rsps)
    body = load_fixture("nws_alerts_one.json")
    set_alerts(("Flood Watch", "Moderate"))(body)
    body["features"][0]["properties"]["expires"] = "2099-01-01T00:00:00+00:00"
    write_cache("nws_alerts_08807", body, "2026-10-25T14:00:00+00:00")
    rsps.replace(responses.GET, alerts_url(), body=requests.exceptions.ConnectionError("offline"))
    at = open_page()
    assert "Data as of Oct 25, 10:00 AM ET" in [c.value for c in at.caption]
    assert texts(cards(at)[0])[0] == "**Flood Watch**"  # the cached alert is still shown


def test_ac_4_5_stale_gauge_series_shows_oldest_time(rsps):
    mock_page(rsps)
    write_cache("nws_alerts_08807", load_fixture("nws_alerts_empty.json"), "2026-10-25T14:00:00+00:00")
    write_cache("nwps_stageflow_BDBN4", load_fixture("nwps_stageflow_BDBN4.json"), "2026-10-24T12:30:00+00:00")
    rsps.replace(responses.GET, alerts_url(), body=requests.exceptions.ConnectionError("offline"))
    rsps.remove(responses.GET, STAGEFLOW_URL)
    rsps.add_callback(responses.GET, STAGEFLOW_URL, callback=stageflow_callback({"BDBN4": None}),
                      content_type="application/json")
    at = open_page()
    captions = [c.value for c in at.caption]
    assert "Data as of Oct 24, 08:30 AM ET" in captions
    assert not any(c == "Data as of Oct 25, 10:00 AM ET" for c in captions)


def test_ac_4_5_no_caption_when_fresh(rsps):
    mock_page(rsps)
    at = open_page()
    assert not any(c.value.startswith("Data as of") for c in at.caption)


def test_ac_4_5_live_inputs_as_of_is_oldest_stale_fetch(rsps):
    loc = resolve_zip("08807")
    mock_all(rsps)
    record = places.get_places(loc)
    fresh = build.live_inputs(loc, record)
    assert fresh.stale is False and fresh.as_of is None
    write_cache("nws_alerts_08807", load_fixture("nws_alerts_empty.json"), "2026-10-25T14:00:00+00:00")
    rsps.replace(responses.GET, alerts_url(), body=requests.exceptions.ConnectionError("offline"))
    stale = build.live_inputs(loc, record)
    assert stale.stale is True and stale.as_of == "2026-10-25T14:00:00+00:00"


# --- review follow-ups: rain-only card, mixed alerts, alerts unavailable ------------

def mock_heavy_rain(rsps, mm=30.0):
    """Serve a gridpoint forecast with ``mm`` of rain in the current hour (30 mm is 1.18 in/h)."""
    from datetime import timezone

    url = load_fixture("nws_point_08807.json")["properties"]["forecastGridData"]
    body = load_fixture("nws_gridpoint_08807.json")
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    body["properties"]["quantitativePrecipitation"]["values"] = [
        {"validTime": f"{start.isoformat(timespec='seconds')}/PT1H", "value": mm}]
    rsps.replace(responses.GET, url, json=body)


def test_ac_4_2_rain_only_conditions_card(rsps):
    mock_page(rsps)
    mock_heavy_rain(rsps)
    at = open_page()
    kind, title, _ = banner(at)
    assert (kind, title) == ("warning", "Elevated risk")
    [card] = cards(at)
    t = texts(card)
    assert t[0] == "**River and rain conditions**"
    assert any("Level: elevated" in x and ":orange-badge" in x for x in t)
    assert "- Heavy rain forecast: elevated" in t
    assert bullet_lines(card) == places.PRECAUTIONS["heavy_rain"]["elevated"]
    roads = road_items(card)
    assert len(roads) == 8 and all("may flood in heavy rain" in r for r in roads)
    months = [flooded_month(r) for r in roads]
    assert months == sorted(months, reverse=True)
    assert any(x.startswith("and ") and x.endswith(" more") for x in t)
    assert not any(x.startswith("No flood spots reported") or x.startswith("No roads to avoid") for x in t)


def test_ac_4_2_non_flood_alert_with_raised_gauge(rsps):
    mock_page(rsps, ("Wind Advisory", "Moderate"), gauge_ft=10.2)
    at = open_page()
    assert banner(at)[:2] == ("warning", "Elevated risk")
    assert [i.value for i in at.info] == ["No active flood alerts. River and rain conditions are raising the risk."]
    md = [m.value for m in at.markdown]
    assert md[md.index("**Other alerts**") + 1].startswith("- **Wind Advisory**")
    [card] = cards(at)
    assert texts(card)[0] == "**River and rain conditions**"


def fail_alerts_no_cache(rsps):
    rsps.replace(responses.GET, alerts_url(), body=requests.exceptions.ConnectionError("offline"))


def test_ac_4_5_alerts_fail_without_cache_shows_generic_caption(rsps):
    mock_page(rsps)
    fail_alerts_no_cache(rsps)
    at = open_page()
    captions = [c.value for c in at.caption]
    assert "Some live data could not be refreshed; cached readings were used." in captions
    assert not any(c.startswith("Data as of") for c in captions)


def test_ac_4_4_alerts_unavailable_is_never_all_clear(rsps):
    mock_page(rsps)
    fail_alerts_no_cache(rsps)
    at = open_page()
    assert not at.success
    assert [(w.proto.title, w.value) for w in at.warning] == [
        ("Alerts could not be checked",
         "NWS alerts are unavailable right now. Risk shown is from river gauges and rain only.")]
    assert not any(i.value == "No active alerts" for i in at.info)


def test_ac_4_4_alerts_unavailable_with_raised_gauge(rsps):
    mock_page(rsps, gauge_ft=10.2)
    fail_alerts_no_cache(rsps)
    at = open_page()
    assert not at.success
    assert [w.proto.title for w in at.warning] == ["Elevated risk", "Alerts could not be checked"]
    [card] = cards(at)
    assert texts(card)[0] == "**River and rain conditions**"
    assert not at.info


def test_ac_4_1_zip_lookup_outage_is_logged(rsps, monkeypatch, caplog):
    mock_page(rsps)
    at = open_page()

    def broken():
        raise OSError("pgeocode data unavailable")

    monkeypatch.setattr(location, "_nominatim", broken)
    with caplog.at_level("ERROR"):
        at.text_input(key="zip").set_value("10001").run()
    assert at.session_state["current_zip"] == "08807"
    assert any("ZIP lookup failed" in r.getMessage() for r in caplog.records)
