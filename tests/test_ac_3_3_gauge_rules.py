"""AC 3.3 (rule 2): gauge stages and fast rises set the waterway's level."""

import pytest

from tests.graph_helpers import AREA, MIDDLE, PETERS, WEST, run


def _path_is_real(G, path):
    return all(G.has_edge(u, v) for u, v in zip(path, path[1:]))


def test_ac_3_3_gauge_at_action_is_elevated():
    G, a = run(gauge_levels={"BDBN4": 10.2})
    assert a.waterway_levels[MIDDLE] == "elevated"
    assert a.spot_levels["spot:s2025"] == a.spot_levels["spot:s2019"] == a.spot_levels["spot:s2009"] == "elevated"
    assert a.spot_levels["spot:swest"] == "low" and a.spot_levels["spot:snone"] == "low"
    assert a.area_level == "elevated"
    [r] = a.reasons
    assert r.rule == 2 and r.level == "elevated"
    assert r.path == ("gauge:BDBN4", MIDDLE, "spot:s2025", AREA)
    assert _path_is_real(G, r.path)
    assert r.label == ("Middle Brook at Bound Brook is 10.2 ft (action stage 10 ft) → Middle Brook elevated "
                       "→ US 22 at Mountain Ave closed may flood")
    for n in (MIDDLE, "spot:s2025", "spot:s2009", AREA):
        assert a.reasons_for(n), n


def test_ac_3_3_gauge_below_action_is_low():
    _, a = run(gauge_levels={"BDBN4": 9.99})
    assert a.waterway_levels[MIDDLE] == "low" and a.area_level == "low"


def test_ac_3_3_gauge_at_minor_is_high():
    _, a = run(gauge_levels={"BDBN4": 11.3})
    assert a.waterway_levels[MIDDLE] == "high"
    assert a.area_level == "high"
    assert a.reasons[0].level == "high" and "minor flood stage 11 ft" in a.reasons[0].label


def test_ac_3_3_fast_rise_raises_one_level():
    G, a = run(gauge_levels={"BDBN4": 9.0}, gauge_rises={"BDBN4": 1.2})  # 9.0 ft now, 7.8 ft an hour ago
    assert a.waterway_levels[MIDDLE] == "elevated"
    assert a.area_level == "elevated"
    [r] = a.reasons
    assert r.path[:2] == ("gauge:BDBN4", MIDDLE) and r.path[-1] == AREA
    assert "rose 1.2 ft in the last hour" in r.label and "raised to elevated" in r.label


def test_ac_3_3_rise_of_exactly_threshold_does_not_raise():
    _, a = run(gauge_levels={"BDBN4": 9.0}, gauge_rises={"BDBN4": 1.0})
    assert a.waterway_levels[MIDDLE] == "low"


def test_ac_3_3_rise_at_action_goes_to_high():
    _, a = run(gauge_levels={"BDBN4": 10.5}, gauge_rises={"BDBN4": 1.5})
    assert a.waterway_levels[MIDDLE] == "high"


def test_ac_3_3_rise_when_already_high_is_capped():
    _, a = run(gauge_levels={"BDBN4": 11.5}, gauge_rises={"BDBN4": 2.0})
    assert a.waterway_levels[MIDDLE] == "high"
    assert a.area_level == "high"
    assert all(r.level == "high" for r in a.reasons)


def test_ac_3_3_gauge_without_stages_only_rise_applies():
    # SMVN4 has no NWS stages: a huge level does nothing, but a fast rise still raises Peters Brook.
    _, a = run(gauge_levels={"SMVN4": 50.0})
    assert a.waterway_levels[PETERS] == "low"
    G, a = run(gauge_levels={"SMVN4": 50.0}, gauge_rises={"SMVN4": 1.4})
    assert a.waterway_levels[PETERS] == "elevated"
    # Peters Brook has no flood spots here: the path ends at the risk node.
    [r] = a.reasons
    assert r.path == ("gauge:SMVN4", PETERS, "risk:08807")
    assert _path_is_real(G, r.path)


@pytest.mark.parametrize("levels, rises", [({"BDBN4": None}, {}), ({}, {}), ({"BDBN4": None}, {"BDBN4": None})])
def test_ac_3_3_no_live_reading_does_not_trigger(levels, rises):
    _, a = run(gauge_levels=levels, gauge_rises=rises)
    assert a.waterway_levels[MIDDLE] == "low" and a.area_level == "low"
    assert not a.reasons


def test_ac_3_3_other_waterways_unaffected():
    _, a = run(gauge_levels={"MRTN4": 6.0})
    assert a.waterway_levels[WEST] == "high"
    assert a.waterway_levels[MIDDLE] == "low"
    assert a.spot_levels["spot:swest"] == "high" and a.spot_levels["spot:s2025"] == "low"


@pytest.mark.parametrize("stages, height, level", [
    ({"moderate": 12.0, "major": 15.0}, 12.5, "high"),  # no minor: moderate is the flood floor
    ({"moderate": 12.0, "major": 15.0}, 11.0, "low"),  # no action: nothing is elevated
    ({"major": 15.0}, 15.0, "high"),
    ({"action": 8.0, "major": 15.0}, 9.0, "elevated"),
    ({"action": 8.0, "major": 15.0}, 15.2, "high"),
    ({"action": 8.0}, 30.0, "elevated"),  # only action set
])
def test_ac_3_3_partial_stages(stages, height, level):
    from tests.graph_helpers import GAUGES, make_places

    gauges = [dict(g, stages=stages) if g["lid"] == "BDBN4" else g for g in GAUGES]
    _, a = run(make_places(gauges=gauges), gauge_levels={"BDBN4": height})
    assert a.waterway_levels[MIDDLE] == level
    if level == "high":
        assert "flood stage" in a.reasons[0].label


def test_ac_3_3_node_levels_for_drawing():
    _, a = run(gauge_levels={"BDBN4": 10.2})
    levels = a.node_levels()
    assert levels["gauge:BDBN4"] == levels[MIDDLE] == levels["spot:s2025"] == "elevated"
    assert levels[AREA] == "elevated"
    assert levels.get(WEST, "low") == "low" and levels.get("gauge:MRTN4", "low") == "low"
    assert levels["spot:swest"] == "low"
