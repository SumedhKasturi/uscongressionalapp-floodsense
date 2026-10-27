"""AC 3.6 (rule 5): when the area is high, open shelters are listed first, nearest first."""

from tests.graph_helpers import FLASH_FLOOD_WARNING, run


def _order(a):
    return [h.get("id") or h["kind"] for h in a.help_order]


def test_ac_3_6_high_lists_shelters_first_nearest_first():
    _, a = run(alerts=[FLASH_FLOOD_WARNING])
    assert a.area_level == "high"
    assert _order(a)[:2] == ["near", "far"]
    assert [h["entry"] for h in a.help_order] == ["shelter", "shelter", "line", "line", "line", "line"]


def test_ac_3_6_not_high_lists_help_lines_first():
    _, a = run(gauge_levels={"BDBN4": 10.2})
    assert a.area_level == "elevated"
    assert _order(a) == ["emergency", "211", "fema", "red_cross", "near", "far"]


def test_ac_3_6_high_with_no_shelters():
    from tests.graph_helpers import make_places

    _, a = run(make_places(shelters=[]), alerts=[FLASH_FLOOD_WARNING])
    assert [h["entry"] for h in a.help_order] == ["line"] * 4
