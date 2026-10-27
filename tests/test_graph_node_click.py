"""Clicking a node in the knowledge graph shows every connection it has.

AppTest can't click inside a chart, so the tests check the chart's click selection in the spec and
then set what the click callback sets (the highlighted node, or the single-item focus).
"""

from graph import build
from tests.test_ac_3_9_graph_page import chart_frames, edge_frame, mock_all, node_frame, open_page

GAUGE = build.gauge_id("BDBN4")
COLUMNS = ["Direction", "Relationship", "Connected node", "Type", "Level"]


def connection_tables(at):
    return [df.value for df in at.dataframe if "Connected node" in df.value.columns]


def click_selection(spec):
    # Altair hoists layer params to the top level of a layered spec.
    params = spec.get("params", []) + [p for layer in spec["layer"] for p in layer.get("params", [])]
    return next(p for p in params if p["name"] == "node_pick")


def test_full_graph_chart_has_click_selection(rsps):
    mock_all(rsps)
    at = open_page()
    pick = click_selection(chart_frames(at)[0])
    assert pick["select"]["on"] == "click" and pick["select"]["fields"] == ["id"]
    assert not connection_tables(at)  # nothing clicked yet
    assert any("click a node to see all its connections" in c.value for c in at.caption)


def test_clicked_node_shows_every_connection(rsps):
    mock_all(rsps)
    at = open_page()
    label = node_frame(at).loc[GAUGE, "label"]
    edges = edge_frame(at)
    expected = edges[(edges["source"] == label) | (edges["target"] == label)]  # full graph shows every edge

    at.session_state["intel_highlight"] = GAUGE  # what a click on the gauge sets
    at.run()
    assert not at.exception, at.exception
    tables = connection_tables(at)
    assert len(tables) == 1
    table = tables[0]
    assert list(table.columns) == COLUMNS
    assert len(table) == len(expected)
    assert set(table["Relationship"]) == set(expected["relationship"])
    assert set(table["Direction"]) <= {"→ out", "← in"}
    assert any(f"{len(table)} connections" == c.value for c in at.caption)

    at.button(key="intel_clear_highlight").click().run()
    assert not at.exception, at.exception
    assert not connection_tables(at)


def test_single_item_view_lists_connections_and_is_clickable(rsps):
    mock_all(rsps)
    at = open_page()
    at.button_group(key="intel_view").set_value("Single item").run()
    # focus_clicked() sets these two keys when a gauge neighbour is clicked.
    at.selectbox(key="intel_item_type").set_value("Stream gauge").run()
    at.selectbox(key="intel_item").set_value(GAUGE).run()
    assert not at.exception, at.exception
    assert click_selection(chart_frames(at)[0])["select"]["on"] == "click"
    tables = connection_tables(at)
    assert len(tables) == 1 and "measures" in set(tables[0]["Relationship"])
    assert any("Click a connected node to explore it." == c.value for c in at.caption)
