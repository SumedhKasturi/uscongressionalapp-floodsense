"""AC 3.1: the graph built from the places record and the live cache has every node type and edge name."""

import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
import responses

import config
from data import places, sources
from data.location import resolve_zip
from graph import build, reason
from tests.conftest import NWPS_GAUGES_URL, PLACES_NOW, load_fixture, mock_live, mock_nws, mock_places_sources

STAGEFLOW_URL = re.compile(re.escape(config.NWPS_BASE) + r"/gauges/(\w+)/stageflow/observed")
# The recorded BDBN4 series ends at 15:30Z.
LIVE_NOW = datetime(2026, 10, 25, 15, 40, tzinfo=timezone.utc)


def stageflow_callback(overrides=None):
    """Serve the recorded BDBN4 series for every gauge, or ``overrides[lid]`` (a body, or None for a 500)."""
    base = load_fixture("nwps_stageflow_BDBN4.json")
    overrides = overrides or {}

    def cb(request):
        lid = STAGEFLOW_URL.search(request.url).group(1)
        if lid in overrides:
            body = overrides[lid]
            return (500, {}, "down") if body is None else (200, {}, json.dumps(body))
        return (200, {}, json.dumps(base))

    return cb


def ffw(body):
    body["features"][0]["properties"]["event"] = "Flash Flood Warning"


@pytest.fixture
def record(rsps):
    loc = resolve_zip("08807")
    mock_places_sources(rsps, loc)
    return places.build_places(loc, now=PLACES_NOW)


def live(rsps, record, overrides=None, alerts_edit=ffw):
    loc = resolve_zip("08807")
    rsps.reset()
    mock_live(rsps, loc, alerts="nws_alerts_one.json", alerts_edit=alerts_edit)
    rsps.add_callback(responses.GET, STAGEFLOW_URL, callback=stageflow_callback(overrides),
                      content_type="application/json")
    return build.live_inputs(loc, record, LIVE_NOW)


def test_ac_3_1_graph_has_every_node_type_and_edge_name(rsps, record):
    inputs = live(rsps, record)
    G = build.build_graph(record, inputs)
    types = {d["type"] for _, d in G.nodes(data=True)}
    assert types == set(build.NODE_TYPES)
    rels = {d["rel"] for *_, d in G.edges(data=True)}
    assert rels == {"measures", "falls on", "sets", "triggers", "floods", "avoid", "cuts off", "covers", "served by",
                    "on", "reported during", "declared as", "monitored by"}
    assert rels == set(build.EDGE_RELS)
    # Node ids use the schema prefixes, and each edge joins the types the schema names.
    for n, d in G.nodes(data=True):
        prefix = {"waterway": "water", "precaution": "prec", "storm event": "event"}.get(d["type"], d["type"])
        assert n.startswith(prefix + ":"), n
    allowed = {
        "measures": ("gauge", "waterway"), "falls on": ("rain", "area"), "sets": ("waterway", "risk"),
        "triggers": ("risk", "precaution"), "floods": ("waterway", "spot"), "avoid": ("spot", "precaution"),
        "cuts off": ("spot", "area"), "covers": ("alert", "area"), "served by": ("area", "help"),
        "on": ("spot", "road"), "reported during": ("spot", "storm event"), "declared as": ("storm event", "disaster"),
        "monitored by": ("spot", "gauge"),
    }
    for u, v, d in G.edges(data=True):
        assert (G.nodes[u]["type"], G.nodes[v]["type"]) == allowed[d["rel"]], (u, v, d)


def test_ac_3_1_graph_nodes_come_from_places_and_live_data(rsps, record):
    inputs = live(rsps, record)
    G = build.build_graph(record, inputs)
    assert G.has_edge("gauge:BDBN4", "water:middle_brook")
    assert G.nodes["gauge:BDBN4"]["stages"] == {"action": 10.0, "minor": 11.0}
    assert G.nodes["gauge:BDBN4"]["level_ft"] == pytest.approx(4.01)  # NWPS near-ZIP reading
    assert G.has_edge("area:08807", "help:emergency") and G.has_edge("area:08807", "help:211")
    assert G.has_edge("alert:0_flash_flood_warning", "area:08807")
    assert G.has_edge("rain:08807", "area:08807")
    assert len([n for n in G if n.startswith("spot:")]) == len(record["flood_spots"])
    # Every spot on a waterway is flooded by it; every spot cuts off the area.
    for s in record["flood_spots"]:
        sid = f"spot:{s['id']}"
        assert G.edges[sid, "area:08807"]["rel"] == "cuts off"
        if s["waterway"]:
            assert G.has_edge(build.water_id(s["waterway"]), sid)
    # The live Flash Flood Warning makes the area high.
    assert reason.assess(G, record, inputs).area_level == "high"


def test_ac_3_1_live_inputs_rise_and_missing_readings(rsps, record):
    series = load_fixture("nwps_stageflow_BDBN4.json")
    rising = copy.deepcopy(series)
    for i, v in enumerate(rising["data"]):  # +0.4 ft every 15 minutes = 1.6 ft/h
        v["primary"] = 7.0 + 0.4 * i
    no_reading = copy.deepcopy(series)
    for v in no_reading["data"]:
        v["primary"] = -999
    inputs = live(rsps, record, overrides={"MRTN4": rising, "BDKN4": no_reading, "MILN4": None},
                  alerts_edit=None)
    assert inputs.gauge_rises["MRTN4"] == pytest.approx(1.6)
    assert abs(inputs.gauge_rises["BDBN4"]) < 0.1  # recorded series is flat
    # BDKN4: -999 in NWPS and in its series, so no level and no rise.
    assert inputs.gauge_levels["BDKN4"] is None and inputs.gauge_rises["BDKN4"] is None
    # MILN4: series fetch failed (no cache), so no rise; its near-ZIP level still counts.
    assert inputs.gauge_rises["MILN4"] is None and inputs.gauge_levels["MILN4"] == pytest.approx(4.74)
    assert inputs.alerts and inputs.alerts[0]["event"] == "Coastal Flood Advisory"
    assert inputs.rain_in_per_hr_max == pytest.approx(0.0) and inputs.rain_6h_in == pytest.approx(0.0)
    G = build.build_graph(record, inputs)
    a = reason.assess(G, record, inputs)
    assert a.waterway_levels["water:west_branch_middle_brook"] == "elevated"  # fast rise, low -> elevated
    assert a.area_level == "elevated"


def test_ac_3_1_gauge_recent_uses_the_cache(rsps):
    url = f"{config.NWPS_BASE}/gauges/BDBN4/stageflow/observed"
    rsps.get(url, json=load_fixture("nwps_stageflow_BDBN4.json"))
    first = sources.gauge_recent("BDBN4")
    assert first is not None and not first.stale and first.source_url == url
    rsps.replace(responses.GET, url, body=__import__("requests").exceptions.ConnectionError("offline"))
    again = sources.gauge_recent("BDBN4")
    assert again.stale and sources.stageflow_series(again) == sources.stageflow_series(first)


def _series(points):
    return [{"validTime": t.isoformat(), "primary": h} for t, h in points]


def test_ac_3_1_rise_ft_per_hr_window():
    now = LIVE_NOW
    s = _series([(now - timedelta(minutes=m), h) for m, h in [(70, 7.7), (60, 7.8), (30, 8.4), (0, 9.0)]])
    assert sources.rise_ft_per_hr(s, now) == pytest.approx(1.2)
    # NWPS runs about an hour behind: the window is measured back from the newest reading.
    lagged = _series([(now - timedelta(minutes=m), h) for m, h in [(100, 7.8), (70, 8.4), (40, 9.0)]])
    assert sources.rise_ft_per_hr(lagged, now) == pytest.approx(1.2)
    # Newest reading older than 2 hours -> None.
    old = _series([(now - timedelta(hours=3), 5.0), (now - timedelta(minutes=121), 6.0)])
    assert sources.rise_ft_per_hr(old, now) is None
    # A single reading, or no base reading 30+ minutes before the newest -> None.
    assert sources.rise_ft_per_hr(_series([(now, 5.0)]), now) is None
    assert sources.rise_ft_per_hr(_series([(now - timedelta(minutes=15), 5.0), (now, 6.0)]), now) is None
    # A base reading more than 75 minutes before the newest is outside the window.
    assert sources.rise_ft_per_hr(_series([(now - timedelta(minutes=90), 5.0), (now, 6.0)]), now) is None
    # -999 is skipped: the newest valid reading is the base for nothing.
    s = _series([(now - timedelta(minutes=60), 4.0), (now - timedelta(minutes=30), -999), (now, -999)])
    assert sources.rise_ft_per_hr(s, now) is None
    s = _series([(now - timedelta(minutes=70), 4.0), (now - timedelta(minutes=10), 5.0), (now, -999)])
    assert sources.rise_ft_per_hr(s, now) == pytest.approx(1.0)
    # Readings after now are ignored.
    assert sources.rise_ft_per_hr(_series([(now + timedelta(minutes=30), 9.0), (now - timedelta(minutes=60), 8.0)]),
                                  now) is None
    assert sources.rise_ft_per_hr([], now) is None


def test_ac_3_1_latest_recent_window_and_no_data():
    now = LIVE_NOW
    s = _series([(now - timedelta(minutes=50), 4.0), (now - timedelta(minutes=40), 4.2), (now, -999)])
    assert sources.latest_recent(s, now) == (4.2, (now - timedelta(minutes=40)).isoformat(timespec="seconds"))
    assert sources.latest_recent(_series([(now - timedelta(minutes=121), 4.0)]), now) is None
    assert sources.latest_recent(_series([(now, -999), (now - timedelta(minutes=5), -999.0)]), now) is None
    assert sources.latest_recent([{"validTime": "bad", "primary": 3}, {"primary": 3}], now) is None
    assert sources.latest_recent([], now) is None


def _near_body(age_minutes):
    """The recorded near-ZIP gauges, each reading ``age_minutes`` before LIVE_NOW."""
    body = load_fixture("nwps_near_08807.json")
    for g in body["gauges"]:
        g["status"]["observed"]["validTime"] = (LIVE_NOW - timedelta(minutes=age_minutes)).isoformat()
    return body


def live_custom(rsps, record, near_body, overrides=None):
    loc = resolve_zip("08807")
    rsps.reset()
    mock_nws(rsps, loc)
    rsps.get(NWPS_GAUGES_URL, json=near_body)
    rsps.add_callback(responses.GET, STAGEFLOW_URL, callback=stageflow_callback(overrides),
                      content_type="application/json")
    return build.live_inputs(loc, record, LIVE_NOW)


def test_ac_3_1_live_inputs_fresh_is_not_stale(rsps, record):
    assert live(rsps, record).stale is False


def test_ac_3_1_live_inputs_failed_series_is_stale(rsps, record):
    assert live(rsps, record, overrides={"MILN4": None}).stale is True


def test_ac_3_1_old_near_reading_does_not_trigger(rsps, record):
    old_series = load_fixture("nwps_stageflow_BDBN4.json")
    for v in old_series["data"]:
        v["validTime"] = "2026-10-20T00:00:00Z"
    body = _near_body(180)
    for g in body["gauges"]:
        if g["lid"] == "BDBN4":
            g["status"]["observed"]["primary"] = 12.0  # above minor, but 3 hours old
    inputs = live_custom(rsps, record, body, overrides={"BDBN4": old_series})
    assert inputs.gauge_levels["BDBN4"] is None
    G = build.build_graph(record, inputs)
    assert reason.assess(G, record, inputs).waterway_levels["water:middle_brook"] == "low"


def test_ac_3_1_level_falls_back_to_recent_series(rsps, record):
    body = _near_body(15)
    for g in body["gauges"]:
        if g["lid"] == "BDBN4":
            g["status"]["observed"]["primary"] = -999  # no near-ZIP reading
    inputs = live_custom(rsps, record, body)
    series = load_fixture("nwps_stageflow_BDBN4.json")["data"]
    assert inputs.gauge_levels["BDBN4"] == pytest.approx(series[-1]["primary"])
    assert sources.parse_time(inputs.gauge_times["BDBN4"]) == sources.parse_time(series[-1]["validTime"])
    assert inputs.gauge_levels["MRTN4"] == pytest.approx(2.03)  # near-ZIP reading still wins


def test_ac_3_1_rain_metrics_converts_mm_and_prorates():
    now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
    qpf = [
        {"validTime": "2026-10-25T00:00:00+00:00/PT6H", "value": 50.0},  # already over
        {"validTime": "2026-10-25T06:00:00+00:00/PT1H", "value": 30.0},  # 1.18 in/h
        {"validTime": "2026-10-25T07:00:00+00:00/PT3H", "value": 6.0},
        {"validTime": "2026-10-25T10:00:00+00:00/PT6H", "value": 24.0},  # 2 of its 6 hours in the window
        {"validTime": "2026-10-26T00:00:00+00:00/P1D", "value": 99.0},  # after the window
    ]
    rate, total = build.rain_metrics(qpf, now)
    assert rate == pytest.approx(30 / 25.4)
    assert total == pytest.approx((30 + 6 + 24 * 2 / 6) / 25.4)
    assert build.rain_metrics([], now) == (None, None)
    assert build.rain_metrics([{"validTime": "bad", "value": 3}], now) == (None, None)


def test_ac_3_1_rain_rate_is_next_hour_maximum():
    now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
    qpf = [
        {"validTime": "2026-10-25T06:00:00+00:00/PT1H", "value": 5.0},
        {"validTime": "2026-10-25T09:00:00+00:00/PT1H", "value": 40.0},  # heavy, but 3 hours out
    ]
    rate, total = build.rain_metrics(qpf, now)
    assert rate == pytest.approx(5 / 25.4)
    assert total == pytest.approx(45 / 25.4)
    # An interval already under way that runs into the next hour counts.
    rate, _ = build.rain_metrics([{"validTime": "2026-10-25T05:00:00+00:00/PT2H", "value": 60.0}], now)
    assert rate == pytest.approx(30 / 25.4)


@pytest.mark.parametrize("remark, label", [
    ("Flooding on Main St. near N. Bridge Rd. was reported. More text.",
     "Flooding on Main St. near N. Bridge Rd. was reported"),
    ("US 22 at Mountain Ave. closed. Water over road.", "US 22 at Mountain Ave. closed"),
    ("FLOODING ON U.S. 22 WB. CLOSED.", "FLOODING ON U.S. 22 WB"),
    ("Old York Rd.", "Old York Rd"),
])
def test_ac_3_1_spot_label_keeps_abbreviations(remark, label):
    assert build.spot_label({"remark": remark}) == label
