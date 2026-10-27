"""AC 3.10: a places record whose sections failed still builds a graph from live data."""

from data import places
from data.location import resolve_zip
from graph import build, reason
from tests.graph_helpers import FLASH_FLOOD_WARNING, NOW, make_inputs


def _degraded():
    return places.fallback_record(resolve_zip("08807"), NOW)


def test_ac_3_10_degraded_record_builds_graph_and_runs_rules():
    record = _degraded()
    inputs = make_inputs(alerts=[FLASH_FLOOD_WARNING], gauge_levels={"BDBN4": 12.0}, rain_in_per_hr_max=1.2)
    G = build.build_graph(record, inputs)
    types = {d["type"] for _, d in G.nodes(data=True)}
    assert {"area", "alert", "rain", "help"} <= types
    assert not types & {"gauge", "waterway", "spot"}
    a = reason.assess(G, record, inputs)
    assert a.area_level == "high"
    assert a.waterway_levels == {} and a.spot_levels == {}
    assert [h["kind"] for h in a.help_order] == ["emergency", "211", "fema", "red_cross"]
    assert a.precautions


def test_ac_3_10_missing_sections_do_not_crash():
    record = {"zip": "08807", "area": {"zip": "08807", "place": "Bridgewater, NJ"}}
    G = build.build_graph(record, make_inputs())
    a = reason.assess(G, record, make_inputs())
    assert a.area_level == "low" and a.precautions == [] and a.help_order == []


def test_ac_3_10_live_inputs_with_degraded_record_fetches_no_gauges(rsps):
    from tests.conftest import mock_nws

    loc = resolve_zip("08807")
    mock_nws(rsps, loc)
    inputs = build.live_inputs(loc, _degraded())
    assert inputs.gauge_levels == {} and inputs.alerts == []
    assert not any("/nwps/" in c.request.url for c in rsps.calls)
