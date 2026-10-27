"""AC 3.5 (rule 4): flood spots on an elevated or high waterway give "avoid" precautions."""

from graph import thresholds
from tests.graph_helpers import run


def _avoid(a):
    return [p for p in a.precautions if p["kind"] == "avoid"]


def test_ac_3_5_avoid_names_recent_spots_newest_first():
    G, a = run(gauge_levels={"BDBN4": 10.2})
    avoid = _avoid(a)
    assert [p["spot"] for p in avoid] == ["spot:s2025", "spot:s2019"]  # 2009 is over 10 years old
    assert avoid[0]["text"].startswith("Avoid US 22 at Mountain Ave closed")
    assert "Middle Brook is elevated" in avoid[0]["text"]
    assert "spot:s2009" in G  # the old spot stays in the graph
    assert all(G.edges[p["spot"], "prec:avoid"]["rel"] == "avoid" for p in avoid)


def test_ac_3_5_no_avoid_when_waterway_low():
    _, a = run(rain_in_per_hr_max=1.5)  # rain raises spots, but no waterway is elevated
    assert _avoid(a) == []
    _, a = run()
    assert _avoid(a) == []


def test_ac_3_5_high_waterway_avoid_and_hazard_text():
    _, a = run(gauge_levels={"MRTN4": 6.5})
    assert [p["spot"] for p in _avoid(a)] == ["spot:swest"]
    hazard = [p for p in a.precautions if p["kind"] == "hazard"]
    assert hazard and all(p["hazard"] == "flood" and p["level"] == "high" for p in hazard)
    # Hazard text comes before the avoid items.
    assert a.precautions[0]["kind"] == "hazard"


def test_ac_3_5_avoid_age_is_a_threshold(monkeypatch):
    monkeypatch.setattr(thresholds, "AVOID_SPOT_MAX_AGE_YEARS", 20)
    _, a = run(gauge_levels={"BDBN4": 10.2})
    assert [p["spot"] for p in _avoid(a)] == ["spot:s2025", "spot:s2019", "spot:s2009"]
