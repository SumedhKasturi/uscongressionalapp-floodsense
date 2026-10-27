"""AC 3.8: every threshold comes from graph/thresholds.py; changing one changes the result."""

import inspect
import re

from graph import build, reason, thresholds
from tests.graph_helpers import MIDDLE, run


def test_ac_3_8_lower_rain_threshold_gives_elevated(monkeypatch):
    kw = {"rain_in_per_hr_max": 0.6, "rain_6h_in": 0.6}
    assert run(**kw)[1].area_level == "low"
    monkeypatch.setattr(thresholds, "RAIN_IN_PER_HR", 0.5)
    assert run(**kw)[1].area_level == "elevated"


def test_ac_3_8_six_hour_and_rise_thresholds(monkeypatch):
    assert run(rain_6h_in=1.5)[1].area_level == "low"
    monkeypatch.setattr(thresholds, "RAIN_6H_IN", 1.5)
    assert run(rain_6h_in=1.5)[1].area_level == "elevated"

    kw = {"gauge_levels": {"BDBN4": 8.0}, "gauge_rises": {"BDBN4": 0.6}}
    assert run(**kw)[1].waterway_levels[MIDDLE] == "low"
    monkeypatch.setattr(thresholds, "RISE_FT_PER_HR", 0.5)
    assert run(**kw)[1].waterway_levels[MIDDLE] == "elevated"


def test_ac_3_8_alert_events_threshold(monkeypatch):
    kw = {"alerts": [{"event": "Coastal Flood Advisory", "headline": "", "severity": "Minor"}]}
    assert run(**kw)[1].area_level == "low"
    monkeypatch.setattr(thresholds, "ALERT_EVENT_LEVELS", {**thresholds.ALERT_EVENT_LEVELS,
                                                           "Coastal Flood Advisory": "high"})
    assert run(**kw)[1].area_level == "high"
    monkeypatch.setattr(thresholds, "ALERT_EVENT_LEVELS", {"Flood Warning": "elevated"})
    assert run(alerts=[{"event": "Flood Warning", "headline": "", "severity": "Severe"}])[1].area_level == "elevated"


def test_ac_3_8_passed_thresholds_module_is_used():
    class Strict:
        LEVELS = thresholds.LEVELS
        ALERT_EVENT_LEVELS = {}
        RISE_FT_PER_HR = 1.0
        RAIN_IN_PER_HR = 0.05
        RAIN_6H_IN = 2.0
        RAIN_LOOKAHEAD_HOURS = 6
        AVOID_SPOT_MAX_AGE_YEARS = 10

    from tests.graph_helpers import make_inputs, make_places

    record, inputs = make_places(), make_inputs(rain_in_per_hr_max=0.1)
    G = build.build_graph(record, inputs)
    assert reason.assess(G, record, inputs).area_level == "low"
    assert reason.assess(G, record, inputs, thresholds=Strict).area_level == "elevated"


def test_ac_3_8_no_numeric_thresholds_in_the_rules():
    """The rules compare against thresholds.<NAME> only; no bare threshold numbers in reason.py."""
    src = inspect.getsource(reason)
    assert not re.search(r"[<>]=?\s*\d", src)
    for name in ("RISE_FT_PER_HR", "RAIN_IN_PER_HR", "RAIN_6H_IN", "AVOID_SPOT_MAX_AGE_YEARS", "ALERT_EVENT_LEVELS"):
        assert f"thresholds.{name}" in src
    assert thresholds.LEVELS == ("low", "elevated", "high")
    assert (thresholds.RISE_FT_PER_HR, thresholds.RAIN_IN_PER_HR, thresholds.RAIN_6H_IN) == (1.0, 1.0, 2.0)
    assert thresholds.AVOID_SPOT_MAX_AGE_YEARS == 10
