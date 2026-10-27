"""Build the knowledge graph from a places record (story 2) and live data (story 1).

Node ids (``knowledge-graph.md``)::

    area:<zip>             the ZIP (where the resident lives)
    alert:<n>_<event>      an active NWS alert           -- covers   --> area
    rain:<zip>             the rain forecast             -- falls on --> area
    gauge:<lid>            an NWPS stream gauge          -- measures --> water
    water:<id>             a waterway                    -- floods   --> spot
                                                         -- sets     --> risk
    spot:<id>              a flood spot (storm report)   -- cuts off --> area
                                                         -- avoid    --> prec:avoid
    risk:<zip>             the area's risk level         -- triggers --> prec:<hazard>:<level>
    prec:<hazard>:<level>  precaution text; prec:avoid is "avoid these roads"
    help:<kind>            a help line or shelter        <-- served by -- area

Context from the places record (``graph/context.py``; the risk rules ignore it)::

    road:<slug>            a road named in a spot's remark       <-- on --              spot
    event:<YYYY-MM-DD>     a storm (2+ reports, consecutive days) <-- reported during -- spot
    disaster:<number>      a FEMA declaration                    <-- declared as --     event
    gauge:<lid>            the spot's nearest gauge (5 mi)       <-- monitored by --    spot

``live_inputs`` is the only function here that does I/O. ``build_graph`` and
``rain_metrics`` are pure, so tests can build graphs from fixtures.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import networkx as nx

import config
from data import sources
from data.location import Location
from data.places import slug
from graph import context, thresholds

MM_PER_IN = 25.4
NODE_TYPES = ("area", "alert", "rain", "gauge", "waterway", "spot", "risk", "precaution", "help",
              "road", "storm event", "disaster")
EDGE_RELS = ("measures", "falls on", "sets", "triggers", "floods", "avoid", "cuts off", "covers", "served by",
             "on", "reported during", "declared as", "monitored by")
AVOID_NODE = "prec:avoid"


@dataclass
class LiveInputs:
    """The live readings the rules use. Build one by hand in tests, or with ``live_inputs``."""

    now: datetime
    alerts: list[dict] = field(default_factory=list)  # alert_summaries: event, headline, severity
    gauge_levels: dict[str, float | None] = field(default_factory=dict)  # lid -> latest stage (ft), None if no reading
    gauge_rises: dict[str, float | None] = field(default_factory=dict)  # lid -> rise over the last hour (ft/h)
    gauge_times: dict[str, str | None] = field(default_factory=dict)  # lid -> time of the latest reading
    # lid -> NWPS flood category of that reading (as on the forecast page), None when the level isn't from NWPS
    gauge_categories: dict[str, str | None] = field(default_factory=dict)
    rain_in_per_hr_max: float | None = None  # highest forecast rain rate in the next hours, in/h
    rain_6h_in: float | None = None  # forecast rain total over the next 6 hours, in
    stale: bool = False  # some live data came from the cache, or could not be fetched
    alerts_unavailable: bool = False  # the alerts fetch failed with no cached copy, so alerts are unknown
    as_of: str | None = None  # oldest fetched_at among the sources served from the cache (ISO-8601)


# --- ids and labels ---------------------------------------------------------------

def area_id(zip_code: str) -> str:
    return f"area:{zip_code}"


def rain_id(zip_code: str) -> str:
    return f"rain:{zip_code}"


def risk_id(zip_code: str) -> str:
    return f"risk:{zip_code}"


def gauge_id(lid: str) -> str:
    return f"gauge:{lid}"


def water_id(name_or_id: str) -> str:
    return f"water:{slug(name_or_id)}"


def spot_node(spot_id: str) -> str:
    return f"spot:{spot_id}"


def prec_id(hazard: str, level: str) -> str:
    return f"prec:{hazard}:{level}"


def alert_id(index: int, event: str) -> str:
    return f"alert:{index}_{slug(event or 'alert') or 'alert'}"


# A sentence ends at ". " after a word of 2+ characters, so "U.S. 22" and initials like "N."
# aren't cut. Common street abbreviations ("St.", "Rd.", "Ave.") don't end a sentence either.
_SENTENCE_END = re.compile(r"(?<=[A-Za-z0-9)]{2}[.;!?])\s+")
_ABBREVIATIONS = {"st", "rd", "ave", "av", "blvd", "dr", "ln", "ct", "pl", "pkwy", "hwy", "rte", "rt", "mt",
                  "ft", "twp", "co", "no", "jr", "sr", "dept", "approx", "vs", "etc", "ext"}


def _first_sentence(text: str) -> str:
    for m in _SENTENCE_END.finditer(text):
        word = re.split(r"[\s(]", text[: m.start()].rstrip())[-1].rstrip(".;!?").lower()
        if text[m.start() - 1] == "." and word in _ABBREVIATIONS:
            continue
        return text[: m.start()]
    return text


def spot_label(spot: dict, max_len: int = 70) -> str:
    """A short name for a flood spot: the first sentence of its storm-report remark."""
    remark = (spot.get("remark") or "").strip()
    text = _first_sentence(remark).rstrip(".;!? ") if remark else ""
    if not text:
        kind = (spot.get("type") or "Flood").title()
        text = f"{kind} report {str(spot.get('date') or '')[:10]}".strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


# --- rain ----------------------------------------------------------------------------

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def _interval(valid_time: str) -> tuple[datetime, datetime] | None:
    """``"2026-10-25T06:00:00+00:00/PT6H"`` -> (start, end)."""
    if not isinstance(valid_time, str) or "/" not in valid_time:
        return None
    start_s, dur_s = valid_time.split("/", 1)
    start = sources.parse_time(start_s)
    m = _DURATION.match(dur_s.strip())
    if start is None or not m or not any(m.groups()):
        return None
    days, hours, minutes = (int(g or 0) for g in m.groups())
    length = timedelta(days=days, hours=hours, minutes=minutes)
    return (start, start + length) if length > timedelta(0) else None


def rain_metrics(qpf: list[dict], now: datetime) -> tuple[float | None, float | None]:
    """(next-hour maximum rain rate in in/h, total in inches over the next RAIN_LOOKAHEAD_HOURS).

    ``qpf`` is ``sources.qpf_values`` output: mm per interval. The rate of an interval is
    its amount over its length, so 30 mm in one hour is 1.18 in/h and 55 mm over six
    hours is 0.36 in/h; the highest rate of any interval overlapping the next hour is used.
    For the total, an interval that only partly overlaps the window counts in proportion.
    Returns (None, None) when no interval overlaps the window (e.g. no forecast data).
    """
    window_end = now + timedelta(hours=thresholds.RAIN_LOOKAHEAD_HOURS)
    hour_end = now + timedelta(hours=1)
    rate_max, total_mm, seen = 0.0, 0.0, False
    for v in qpf or []:
        span = _interval((v or {}).get("validTime"))
        try:
            mm = float((v or {}).get("value"))
        except (TypeError, ValueError):
            continue
        if span is None or mm < 0:
            continue
        start, end = span
        overlap = (min(end, window_end) - max(start, now)).total_seconds()
        if overlap <= 0:
            continue
        seen = True
        length_h = (end - start).total_seconds() / 3600
        if start < hour_end:
            rate_max = max(rate_max, mm / length_h / MM_PER_IN)
        total_mm += mm * overlap / (end - start).total_seconds()
    if not seen:
        return None, None
    return rate_max, total_mm / MM_PER_IN


# --- live data (the only I/O in graph/) ----------------------------------------------

def _recent(time: str | None, now: datetime) -> bool:
    """True when a reading time is no more than OLD_READING_AFTER before ``now``."""
    when = sources.parse_time(time)
    return when is not None and now - when <= sources.OLD_READING_AFTER


def live_inputs(loc: Location, places: dict, now: datetime | None = None) -> LiveInputs:
    """Fetch alerts, rain forecast and gauge readings for ``loc`` (cached JSON on failure).

    Gauges are the ones in the places record. Each gauge's level is its latest NWPS
    reading near the ZIP (or the newest reading of its recent series), and its rise
    comes from the recent series. A gauge with no reading within
    ``sources.OLD_READING_AFTER`` of ``now`` gets None, so an old cached reading can't trigger.
    """
    now = now or datetime.now(timezone.utc)
    stale = False
    stale_times: list[str] = []  # fetched_at of each source served from the cache

    def note(result) -> None:
        if result is not None and getattr(result, "stale", False) and getattr(result, "fetched_at", None):
            stale_times.append(result.fetched_at)

    alerts_res = sources.nws_alerts(loc.lat, loc.lon, loc.zip)
    stale |= alerts_res is None or alerts_res.stale
    note(alerts_res)
    alerts = sources.alert_summaries(alerts_res, now)

    point = sources.nws_point(loc)
    grid = sources.nws_gridpoint_qpf(loc, point)
    stale |= grid is None or grid.stale
    note(grid)
    rain_rate, rain_6h = rain_metrics(sources.qpf_values(grid), now)

    lids = [g.get("lid") for g in places.get("gauges") or [] if g.get("lid")]
    near = {}
    if lids:
        found = sources.gauges_near(loc, n=config.PLACES_GAUGE_COUNT)
        stale |= found is None or any(g.stale for g in found)
        for g in found or []:
            note(g)
        near = {g.site: g for g in found or []}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(lids)))) as pool:
        recents = dict(zip(lids, pool.map(sources.gauge_recent, lids)))
    stale |= any(r is None or r.stale for r in recents.values())
    for r in recents.values():
        note(r)

    levels, rises, times, categories = {}, {}, {}, {}
    for lid in lids:
        series = sources.stageflow_series(recents.get(lid))
        g = near.get(lid)
        if g is not None and g.has_reading and _recent(g.time, now):
            levels[lid], times[lid], categories[lid] = g.height_ft, g.time, g.flood_category
        else:
            latest = sources.latest_recent(series, now)
            levels[lid], times[lid] = latest if latest else (None, None)
            categories[lid] = None
        rises[lid] = sources.rise_ft_per_hr(series, now)

    return LiveInputs(
        now=now,
        alerts=alerts,
        gauge_levels=levels,
        gauge_rises=rises,
        gauge_times=times,
        gauge_categories=categories,
        rain_in_per_hr_max=rain_rate,
        rain_6h_in=rain_6h,
        stale=stale,
        alerts_unavailable=alerts_res is None,
        as_of=min(stale_times, key=lambda t: sources.parse_time(t) or now) if stale_times else None,
    )


# --- graph -----------------------------------------------------------------------------

def build_graph(places: dict, inputs: LiveInputs) -> nx.DiGraph:
    """The knowledge graph for one ZIP. Works with a degraded record (only area, help, precautions)."""
    G = nx.DiGraph()
    area = places.get("area") or {}
    zip_code = str(area.get("zip") or places.get("zip") or "")
    a = area_id(zip_code)
    place = area.get("place") or zip_code
    G.add_node(a, type="area", label=f"{place} ({zip_code})", zip=zip_code, place=place)

    risk = risk_id(zip_code)
    G.add_node(risk, type="risk", label="Risk level")

    # Live data: rain and alerts fall on / cover the area.
    r = rain_id(zip_code)
    G.add_node(r, type="rain", label="Rain forecast", in_per_hr_max=inputs.rain_in_per_hr_max,
               total_6h_in=inputs.rain_6h_in)
    G.add_edge(r, a, rel="falls on")
    for i, alert in enumerate(inputs.alerts):
        n = alert_id(i, alert.get("event") or "")
        G.add_node(n, type="alert", label=alert.get("event") or "Alert", event=alert.get("event") or "",
                   headline=alert.get("headline") or "", severity=alert.get("severity") or "")
        G.add_edge(n, a, rel="covers")

    # Waterways and their gauges.
    for w in places.get("waterways") or []:
        name = w.get("name") or w.get("id")
        if not name:
            continue
        wid = water_id(w.get("id") or name)
        G.add_node(wid, type="waterway", label=name, name=name)
        G.add_edge(wid, risk, rel="sets")
    for g in places.get("gauges") or []:
        lid, waterway = g.get("lid"), g.get("waterway")
        if not lid or not waterway:
            continue
        wid = water_id(waterway)
        if wid not in G:
            G.add_node(wid, type="waterway", label=waterway, name=waterway)
            G.add_edge(wid, risk, rel="sets")
        G.add_node(gauge_id(lid), type="gauge", label=g.get("name") or lid, lid=lid, name=g.get("name") or lid,
                   waterway=wid, stages=dict(g.get("stages") or {}), lat=g.get("lat"), lon=g.get("lon"),
                   level_ft=inputs.gauge_levels.get(lid), rise_ft_per_hr=inputs.gauge_rises.get(lid),
                   time=inputs.gauge_times.get(lid))
        G.add_edge(gauge_id(lid), wid, rel="measures")

    # Flood spots: each cuts off the area; a spot on a waterway is flooded by it.
    for s in places.get("flood_spots") or []:
        if not s.get("id"):
            continue
        sid = spot_node(s["id"])
        wid = water_id(s["waterway"]) if s.get("waterway") else None
        G.add_node(sid, type="spot", label=spot_label(s), date=s.get("date"), kind=s.get("type"),
                   remark=s.get("remark") or "", lat=s.get("lat"), lon=s.get("lon"), waterway=wid)
        G.add_edge(sid, a, rel="cuts off")
        if wid:
            if wid not in G:
                G.add_node(wid, type="waterway", label=s["waterway"], name=s["waterway"])
                G.add_edge(wid, risk, rel="sets")
            G.add_edge(wid, sid, rel="floods")
        if AVOID_NODE not in G:
            G.add_node(AVOID_NODE, type="precaution", label="Avoid flooded roads", hazard="avoid")
        G.add_edge(sid, AVOID_NODE, rel="avoid")

    # Precautions, triggered by the risk level.
    for hazard, by_level in (places.get("precautions") or {}).items():
        for level in thresholds.LEVELS:
            texts = list((by_level or {}).get(level) or [])
            if not texts:
                continue
            p = prec_id(hazard, level)
            G.add_node(p, type="precaution", label=f"{hazard.replace('_', ' ').capitalize()} ({level})",
                       hazard=hazard, level=level, texts=texts)
            G.add_edge(risk, p, rel="triggers", level=level)

    # Help: the area is served by each help line and shelter.
    for h in places.get("help") or []:
        kind = h.get("kind") or slug(h.get("name") or "help")
        n = f"help:{kind}"
        G.add_node(n, type="help", label=h.get("name") or kind, number=h.get("number"), kind=kind)
        G.add_edge(a, n, rel="served by")
    for i, sh in enumerate(places.get("shelters") or []):
        n = f"help:shelter_{sh.get('id') or i}"
        G.add_node(n, type="help", label=sh.get("name") or "Shelter", kind="shelter",
                   distance_mi=sh.get("distance_mi"), address=sh.get("address"))
        G.add_edge(a, n, rel="served by")

    # Context: roads, storm events, FEMA disasters and each spot's nearest gauge.
    context.add_context(G, places, spot_node, gauge_id)
    return G
