"""AC 3.4 (rule 3): heavy forecast rain makes the area and its flood spots elevated."""

from datetime import datetime, timezone

from graph import build
from tests.graph_helpers import AREA, MIDDLE, run

NOW = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)


def _rain(qpf):
    rate, total = build.rain_metrics(qpf, NOW)
    return {"now": NOW, "rain_in_per_hr_max": rate, "rain_6h_in": total}


def _all_spots(a, level):
    return all(v == level for v in a.spot_levels.values())


def test_ac_3_4_heavy_rain_rate_is_elevated():
    kw = _rain([{"validTime": "2026-10-25T06:00:00+00:00/PT1H", "value": 30.0}])  # 1.18 in/h
    G, a = run(**kw)
    assert a.area_level == "elevated"
    assert _all_spots(a, "elevated")  # including the spot with no waterway
    assert a.waterway_levels[MIDDLE] == "low"
    [r] = a.reasons
    assert r.rule == 3 and r.path == ("rain:08807", AREA) and G.has_edge(*r.path)
    assert "1.18 in/h" in r.label
    assert a.reasons_for("spot:snone")
    assert any(p["hazard"] == "heavy_rain" and p["level"] == "elevated" for p in a.precautions)


def test_ac_3_4_six_hour_total_is_elevated():
    kw = _rain([{"validTime": "2026-10-25T06:00:00+00:00/PT6H", "value": 55.0}])  # 2.17 in, 0.36 in/h
    assert kw["rain_in_per_hr_max"] < 1.0
    _, a = run(**kw)
    assert a.area_level == "elevated" and _all_spots(a, "elevated")
    assert "2.17 in of rain" in a.reasons[0].label


def test_ac_3_4_light_rain_is_low():
    _, a = run(**_rain([{"validTime": "2026-10-25T06:00:00+00:00/PT1H", "value": 20.0}]))  # 0.79 in/h
    assert a.area_level == "low" and _all_spots(a, "low")


def test_ac_3_4_spot_takes_higher_of_waterway_and_rain():
    _, a = run(gauge_levels={"BDBN4": 11.2}, rain_in_per_hr_max=1.5, rain_6h_in=0.0)
    assert a.spot_levels["spot:s2025"] == "high"
    assert a.spot_levels["spot:swest"] == "elevated"
    assert a.spot_levels["spot:snone"] == "elevated"


def test_ac_3_4_no_rain_data_does_not_trigger():
    _, a = run(rain_in_per_hr_max=None, rain_6h_in=None)
    assert a.area_level == "low"
