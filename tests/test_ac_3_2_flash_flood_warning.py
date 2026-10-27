"""AC 3.2 (rule 1): an active Flash Flood Warning covering the area makes it high.

Story 4 widened rule 1 to every NWS flood alert, mapped by type (thresholds.ALERT_EVENT_LEVELS).
"""

import pytest

from graph import thresholds
from tests.graph_helpers import AREA, FLASH_FLOOD_WARNING, MIDDLE, run

RECENT = ("spot:s2025", "spot:swest", "spot:snone", "spot:s2019")  # newest first; s2009 is too old


def alert(event, severity="Moderate"):
    return {"event": event, "headline": f"{event} until 9 PM", "severity": severity}


def test_ac_3_2_flash_flood_warning_is_high():
    G, a = run(alerts=[FLASH_FLOOD_WARNING])
    assert a.area_level == "high"
    [r] = [r for r in a.reasons if r.rule == 1 and len(r.path) == 2]
    assert r.level == "high"
    assert r.path == ("alert:0_flash_flood_warning", AREA)
    assert all(G.has_edge(u, v) for u, v in zip(r.path, r.path[1:]))
    assert "Flash Flood Warning" in r.label and "Bridgewater" in r.label
    assert a.reasons_for(AREA)
    # The warning raises the area, not the waterways.
    assert a.waterway_levels[MIDDLE] == "low"
    assert any(p["hazard"] == "flash_flood" and p["level"] == "high" for p in a.precautions)


def test_ac_3_2_non_flood_alerts_do_not_raise_the_area():
    _, a = run(alerts=[alert("Wind Advisory"), alert("Coastal Flood Advisory", "Minor")])
    assert a.area_level == "low"
    assert not a.reasons


def test_ac_3_2_alert_event_levels_mapping():
    assert thresholds.ALERT_EVENT_LEVELS == {
        "Flash Flood Warning": "high", "Flood Warning": "high", "Flash Flood Emergency": "high",
        "Flash Flood Watch": "elevated", "Flood Watch": "elevated", "Flood Advisory": "elevated",
    }


@pytest.mark.parametrize("event, level, hazard", [
    ("Flash Flood Warning", "high", "flash_flood"),
    ("Flood Warning", "high", "flood"),
    ("Flash Flood Emergency", "high", "flash_flood"),
    ("Flash Flood Watch", "elevated", "flash_flood"),
    ("Flood Watch", "elevated", "flood"),
    ("Flood Advisory", "elevated", "flood"),
    ("FLOOD WATCH", "elevated", "flood"),  # case-insensitive
    ("flash flood warning", "high", "flash_flood"),
])
def test_ac_3_2_flood_alert_types_set_the_level(event, level, hazard):
    _, a = run(alerts=[alert(event)])
    assert a.area_level == level
    [r] = [r for r in a.reasons if r.rule == 1 and len(r.path) == 2]
    assert r.level == level and event in r.label
    for s in RECENT:
        assert a.spot_levels[s] == level, s
    assert a.spot_levels["spot:s2009"] == "low"
    avoid = [p for p in a.precautions if p["kind"] == "avoid"]
    assert [p["spot"] for p in avoid] == list(RECENT)
    assert all(p["level"] == level and event in p["text"] for p in avoid)
    assert any(p["kind"] == "hazard" and p["hazard"] == hazard and p["level"] == level for p in a.precautions)


def test_ac_3_2_alerts_never_lower_a_spot():
    # A gauge at minor stage makes Middle Brook's spots high; a Flood Watch must not lower them.
    _, a = run(alerts=[alert("Flood Watch")], gauge_levels={"BDBN4": 11.5})
    assert a.area_level == "high"
    assert a.spot_levels["spot:s2025"] == "high"
    assert a.spot_levels["spot:swest"] == "elevated"  # other waterway: the watch's level
    # Two alerts: each spot takes the higher one.
    _, a = run(alerts=[alert("Flood Watch"), FLASH_FLOOD_WARNING])
    assert all(a.spot_levels[s] == "high" for s in RECENT)
    item = next(p for p in a.precautions if p.get("spot") == "spot:s2025")
    assert "Flood Watch" in item["text"] and "Flash Flood Warning" in item["text"]
    assert [p["spot"] for p in a.precautions if p["kind"] == "avoid"] == list(RECENT)  # each spot once


def test_ac_3_2_flash_flood_warning_raises_recent_spots_high():
    G, a = run(alerts=[FLASH_FLOOD_WARNING])
    # Spots from the last 10 years go high, whatever their waterway (or none); 2009 keeps its level.
    for s in ("spot:s2025", "spot:swest", "spot:snone", "spot:s2019"):
        assert a.spot_levels[s] == "high", s
    assert a.spot_levels["spot:s2009"] == "low"
    avoid = [p for p in a.precautions if p["kind"] == "avoid"]
    assert [p["spot"] for p in avoid] == ["spot:s2025", "spot:swest", "spot:snone", "spot:s2019"]  # newest first
    assert all(p["level"] == "high" and "Flash Flood Warning" in p["text"] for p in avoid)
    spot_paths = [r.path for r in a.reasons if r.rule == 1 and len(r.path) == 3]
    assert spot_paths == [("alert:0_flash_flood_warning", s, AREA) for s in [p["spot"] for p in avoid]]
    assert a.reasons_for("spot:s2025") and not a.reasons_for("spot:s2009")


def test_ac_3_2_flash_flood_warning_avoid_items_not_duplicated():
    _, a = run(alerts=[FLASH_FLOOD_WARNING], gauge_levels={"BDBN4": 10.2})
    avoid = [p["spot"] for p in a.precautions if p["kind"] == "avoid"]
    assert avoid == ["spot:s2025", "spot:swest", "spot:snone", "spot:s2019"]
    item = next(p for p in a.precautions if p.get("spot") == "spot:s2025")
    assert "Flash Flood Warning" in item["text"] and "Middle Brook is elevated" in item["text"]
    assert a.spot_levels["spot:s2009"] == "elevated"  # old spot: its waterway's level
