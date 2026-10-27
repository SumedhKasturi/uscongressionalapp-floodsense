"""AC 3.7: with no triggers, the area and every waterway are low."""

from tests.graph_helpers import make_places, run


def test_ac_3_7_calm_day_is_low():
    _, a = run(gauge_levels={"BDBN4": 4.0, "MRTN4": 2.0, "SMVN4": 1.8},
               gauge_rises={"BDBN4": 0.02, "MRTN4": -0.1, "SMVN4": 0.0},
               rain_in_per_hr_max=0.1, rain_6h_in=0.3,
               alerts=[{"event": "Coastal Flood Advisory", "headline": "", "severity": "Minor"}])
    assert a.area_level == "low"
    assert set(a.waterway_levels.values()) == {"low"}
    assert set(a.spot_levels.values()) == {"low"}
    assert a.reasons == []
    assert all(p["kind"] == "hazard" and p["level"] == "low" for p in a.precautions)
    assert [p["text"] for p in a.precautions] == make_places()["precautions"]["flood"]["low"]
