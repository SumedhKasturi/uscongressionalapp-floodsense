"""AC 3.9: the environmental intelligence page renders the Altair knowledge graph and shares its ZIP."""

import json

import pyarrow as pa
import pytest
import requests
import responses
from streamlit.testing.v1 import AppTest

import config
from data import location
from data.location import resolve_zip
from graph import build
from tests.conftest import mock_live, mock_places_sources
from tests.test_ac_3_1_graph_schema import STAGEFLOW_URL, stageflow_callback
from ui_shared import ASK_FOR_ZIP

APP = "../app.py"
PAGE = "app_pages/intelligence.py"
FORECAST = "app_pages/forecast.py"


def mock_all(rsps, zip_code="08807", **live_kw):
    loc = resolve_zip(zip_code)
    if zip_code == "08807":
        mock_places_sources(rsps, loc)
    mock_live(rsps, loc, **live_kw)
    rsps.add_callback(responses.GET, STAGEFLOW_URL, callback=stageflow_callback(), content_type="application/json")


def open_page():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    at.switch_page(PAGE).run()
    assert not at.exception, at.exception
    return at


def charts(at):
    return at.get("vega_lite_chart")


def chart_frames(at, index=0):
    """(spec, {dataset name: DataFrame}) of one Altair chart on the page."""
    proto = charts(at)[index].proto
    frames = {d.name: pa.ipc.open_stream(d.data.data).read_pandas() for d in proto.datasets}
    return json.loads(proto.spec), frames


def node_frame(at, index=0):
    _, frames = chart_frames(at, index)
    return next(df for df in frames.values() if "raised" in df.columns).set_index("id")


def edge_frame(at, index=0):
    _, frames = chart_frames(at, index)
    return next(df for df in frames.values() if "relationship" in df.columns)


def test_ac_3_9_graph_page_renders_altair_graph(rsps):
    mock_all(rsps)
    at = open_page()
    assert not at.title  # the shell's banner is the only title
    assert at.text_input(key="zip").value == "08807"
    assert "Bridgewater, NJ (08807)" in [s.value for s in at.subheader]
    assert any("Area risk: low" in m.value for m in at.markdown)
    assert len(charts(at)) == 1
    spec, _ = chart_frames(at)
    assert [layer["mark"]["type"] for layer in spec["layer"]] == ["rule", "circle", "text"]
    nodes = node_frame(at)
    assert {"area:08807", "gauge:BDBN4", "water:middle_brook", "risk:08807"} <= set(nodes.index)
    assert set(nodes["type"]) == {"Area", "Rain forecast", "Stream gauge", "Waterway", "Flood spot", "Risk level",
                                  "Precaution", "Help", "Road", "Storm event", "FEMA disaster"}
    assert nodes.loc["road:us_22", "label"] == "US 22"
    assert nodes.loc["disaster:4614", "label"] == "Remnants of Hurricane Ida (DR-4614)"
    assert nodes.loc["gauge:BDBN4", "type"] == "Stream gauge"
    assert set(edge_frame(at)["relationship"]) == set(build.EDGE_RELS) - {"covers"}  # no alerts in this fixture
    assert not at.error


def test_ac_3_9_graph_statistics_and_legend(rsps):
    mock_all(rsps)
    at = open_page()
    metrics = {m.label: m.value for m in at.metric}
    assert list(metrics) == ["Stream gauges", "Waterways", "Flood spots", "Roads", "Storm events", "Alerts",
                             "Help resources", "Connections"]
    assert int(metrics["Roads"]) == (nodes_types := node_frame(at)["type"]).eq("Road").sum() > 0
    assert int(metrics["Storm events"]) == nodes_types.eq("Storm event").sum() > 0
    nodes = node_frame(at)
    assert int(metrics["Stream gauges"]) == (nodes["type"] == "Stream gauge").sum()
    assert metrics["Alerts"] == "0"
    spec, _ = chart_frames(at)
    color = spec["layer"][1]["encoding"]["color"]
    assert color["field"] == "type" and "Stream gauge" in color["scale"]["domain"]
    assert len(color["scale"]["domain"]) == len(color["scale"]["range"])
    params = spec.get("params", []) + [p for layer in spec["layer"] for p in layer.get("params", [])]
    assert any(p.get("bind") == "legend" for p in params)
    tooltip = [t["field"] for t in spec["layer"][1]["encoding"]["tooltip"]]
    assert tooltip[:3] == ["label", "type", "level"]


def test_ac_3_9_raised_nodes_outlined_in_red(rsps):
    def ffw(body):
        body["features"][0]["properties"]["event"] = "Flash Flood Warning"

    mock_all(rsps, alerts="nws_alerts_one.json", alerts_edit=ffw)
    at = open_page()
    assert any("Area risk: high" in m.value for m in at.markdown)
    assert any("Flash Flood Warning covers Bridgewater, NJ" in m.value for m in at.markdown)
    nodes = node_frame(at)
    for n in ("area:08807", "risk:08807", "alert:0_flash_flood_warning"):
        assert nodes.loc[n, "raised"] and nodes.loc[n, "level"] == "high", n
    assert not nodes.loc["help:emergency", "raised"]
    # Raised nodes are drawn larger and with a red outline.
    assert nodes.loc["alert:0_flash_flood_warning", "size"] > nodes.loc["alert:0_flash_flood_warning", "connections"] + 1
    spec, _ = chart_frames(at)
    stroke = spec["layer"][1]["encoding"]["stroke"]
    assert stroke["condition"] == {"test": "datum.raised", "value": "#d62728"}


def test_ac_3_9_single_item_view(rsps):
    mock_all(rsps)
    at = open_page()
    at.button_group(key="intel_view").set_value("Single item").run()
    assert not at.exception, at.exception
    at.selectbox(key="intel_item_type").set_value("Stream gauge").run()
    at.selectbox(key="intel_item").set_value("gauge:BDBN4").run()
    nodes = node_frame(at)
    assert {"gauge:BDBN4", "water:middle_brook"} <= set(nodes.index)
    assert set(nodes["type"]) == {"Stream gauge", "Waterway", "Flood spot"}  # spots it monitors
    assert any("Middle Brook at Bound Brook" in m.value for m in at.markdown)


@pytest.mark.parametrize("kind, item, neighbour_types", [
    ("Road", "road:us_22", {"Flood spot"}),
    ("Storm event", "event:2021-09-01", {"Flood spot", "FEMA disaster"}),  # Ida's reports and DR-4614
    ("FEMA disaster", "disaster:4614", {"Storm event"}),
])
def test_ac_3_9_single_item_context_nodes(rsps, kind, item, neighbour_types):
    mock_all(rsps)
    at = open_page()
    at.button_group(key="intel_view").set_value("Single item").run()
    at.selectbox(key="intel_item_type").set_value(kind).run()
    at.selectbox(key="intel_item").set_value(item).run()
    assert not at.exception, at.exception
    nodes = node_frame(at)
    assert item in nodes.index and set(nodes.drop(index=item)["type"]) == neighbour_types


def test_ac_3_9_filters_and_facts(rsps):
    mock_all(rsps)
    at = open_page()
    at.multiselect(key="intel_types").set_value(["Stream gauge", "Waterway"]).run()
    nodes = node_frame(at)
    assert set(nodes["type"]) == {"Stream gauge", "Waterway"}
    assert set(edge_frame(at)["relationship"]) == {"measures"}
    at.button(key="intel_all_types").click().run()
    assert "Flood spot" in set(node_frame(at)["type"])
    at.multiselect(key="intel_rels").set_value([]).run()
    assert not charts(at)
    assert any("Select at least one" in i.value for i in at.info)
    at.text_input(key="facts_filter").set_value("Bound Brook").run()
    facts = at.dataframe[0].value
    assert list(facts.columns) == ["Source", "Relationship", "Target"]
    assert len(facts) and facts.apply(lambda r: r.str.contains("Bound Brook", case=False).any(), axis=1).all()


def test_ac_3_9_highlight_and_label_modes(rsps):
    mock_all(rsps)
    at = open_page()
    at.selectbox(key="intel_highlight").set_value("water:middle_brook").run()
    edges = edge_frame(at)
    assert edges["highlighted"].any() and not edges["highlighted"].all()
    nodes = node_frame(at)
    assert nodes.loc["gauge:BDBN4", "highlighted"] and nodes.loc["gauge:BDBN4", "text"]
    at.button_group(key="intel_labels").set_value("None").run()
    nodes = node_frame(at)
    assert (nodes.loc[~nodes["highlighted"], "text"] == "").all()
    at.button_group(key="intel_labels").set_value("All").run()
    assert (node_frame(at)["text"] != "").all()


def test_ac_3_9_zip_shared_with_home_page(rsps):
    mock_all(rsps)
    mock_all(rsps, "10001")
    at = open_page()
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert "New York, NY (10001)" in [s.value for s in at.subheader]
    assert "area:10001" in node_frame(at).index
    assert any("Area risk:" in m.value for m in at.markdown)
    at.switch_page(FORECAST).run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == "10001"
    assert "Stream gauges near New York, NY" in [h.value for h in at.header]
    # And back: the home page's ZIP shows on the graph page.
    at.text_input(key="zip").set_value("08807").run()
    at.switch_page(PAGE).run()
    assert at.text_input(key="zip").value == "08807"


@pytest.mark.parametrize("bad", ["abcde", "00000"])
def test_ac_3_9_invalid_zip_message(rsps, bad):
    mock_all(rsps)
    at = open_page()
    at.text_input(key="zip").set_value(bad).run()
    assert not at.exception, at.exception
    # The sidebar box shows the invalid-ZIP message; the page asks for a valid ZIP and stops.
    assert [e.value for e in at.sidebar.error] == ["Enter a valid 5-digit US ZIP"]
    assert [e.value for e in at.error] == ["Enter a valid 5-digit US ZIP"]
    assert [i.value for i in at.main.info] == [ASK_FOR_ZIP]
    assert not charts(at)


def test_ac_3_9_lookup_unavailable_message(rsps, monkeypatch):
    mock_all(rsps)
    at = open_page()

    def broken():
        raise OSError("pgeocode data unavailable")

    monkeypatch.setattr(location, "_nominatim", broken)
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == ["ZIP lookup unavailable"]


def test_ac_3_9_stale_caption_when_alerts_fetch_fails(rsps):
    mock_all(rsps)
    loc = resolve_zip("08807")
    rsps.replace(responses.GET, f"{config.NWS_BASE}/alerts/active?point={loc.lat:.4f},{loc.lon:.4f}",
                 body=requests.exceptions.ConnectionError("offline"))
    at = open_page()
    assert any(c.value == "Some live data could not be refreshed; cached readings were used." for c in at.caption)


def test_ac_3_9_no_stale_caption_when_fresh(rsps):
    mock_all(rsps)
    at = open_page()
    assert not any("could not be refreshed" in c.value for c in at.caption)


def test_ac_3_9_build_failure_shows_message(rsps, monkeypatch):
    mock_all(rsps)

    def boom(*args, **kwargs):
        raise RuntimeError("graph bug")

    monkeypatch.setattr(build, "build_graph", boom)
    at = open_page()
    assert [e.value for e in at.error] == ["Live data unavailable"]
    assert not charts(at)


def test_ac_3_9_invalid_zip_is_not_shared(rsps):
    mock_all(rsps)
    at = open_page()
    at.text_input(key="zip").set_value("abcde").run()
    assert at.session_state["current_zip"] == "08807"
    assert at.text_input(key="zip").value == "abcde"  # the box keeps what was typed
    # The one sidebar box keeps the bad entry on every page, and current_zip never takes it.
    at.switch_page(FORECAST).run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == "abcde"
    assert [i.value for i in at.main.info] == [ASK_FOR_ZIP] and not at.dataframe
    at.text_input(key="zip").set_value("0000").run()
    assert at.session_state["current_zip"] == "08807"
    at.text_input(key="zip").set_value("08807").run()
    assert not at.error and "Next 12 hours" in [h.value for h in at.header]
    at.switch_page(PAGE).run()
    assert at.text_input(key="zip").value == "08807" and not at.error
    assert len(charts(at)) == 1


def test_ac_3_9_labels_are_plain_text(rsps, monkeypatch):
    from graph import build as graph_build

    real = graph_build.build_graph

    def with_odd_label(record, inputs):
        G = real(record, inputs)
        spot = next(n for n in G if n.startswith("spot:"))
        G.nodes[spot]["label"] = "Rd </script> & co"
        return G

    mock_all(rsps)
    monkeypatch.setattr(graph_build, "build_graph", with_odd_label)
    nodes = node_frame(open_page())
    assert "Rd </script> & co" in set(nodes["label"])  # Altair gets the text as data, unescaped


def test_ac_3_9_intelligence_is_the_default_page(rsps):
    mock_all(rsps)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert at.text_input(key="zip").value == "08807"
    assert "Graph statistics" in [s.value for s in at.subheader]
    assert len(charts(at)) == 1
    assert "Next 12 hours" not in [h.value for h in at.header]

def test_ac_3_9_shell_renders_banner_and_navigation(rsps):
    mock_live(rsps, resolve_zip("08807"))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    banner = " ".join(h.proto.body for h in at.get("html"))
    assert "FloodSense" in banner and "Live flood risk, explained, for any US ZIP" in banner
    assert "Congressional App Challenge" in banner and "NJ-07" in banner
    assert "<script" not in banner.lower()
    assert at.toggle(key="debug").value is True
    for page in (PAGE, FORECAST):
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
