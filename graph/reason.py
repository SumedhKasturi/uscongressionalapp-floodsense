"""Risk rules over the knowledge graph (knowledge-graph.md, "Starting risk rules").

1. An active NWS flood alert covering the area sets its level by event type
   (thresholds.ALERT_EVENT_LEVELS: warnings and emergencies high, watches and advisories
   elevated), and raises every flood spot reported within AVOID_SPOT_MAX_AGE_YEARS to that level.
2. A gauge at or above its action stage makes its waterway elevated; at or above minor,
   high. A gauge rising more than RISE_FT_PER_HR raises its waterway one level (max high).
3. Forecast rain of RAIN_IN_PER_HR or more, or RAIN_6H_IN or more over the next 6 hours,
   makes the area and every flood spot elevated.
4. A flood spot whose waterway is elevated or high, reported within the last
   AVOID_SPOT_MAX_AGE_YEARS, gives the area an "avoid" precaution naming it (newest first).
5. When the area is high, open shelters are listed first, nearest first.

The area's level is the highest level from any rule. A spot's level is its waterway's
level, raised to at least the rain level (a spot with no waterway takes the rain level).

The rules are pure: no network, and ``inputs.now`` is the clock. Every threshold is read
from the ``thresholds`` module at call time, so changing a value there changes the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import networkx as nx

from data.sources import parse_time
from graph import build
from graph import thresholds as default_thresholds

HAZARD_ORDER = ("flash_flood", "flood", "heavy_rain")


@dataclass(frozen=True)
class Reason:
    """Why a level was raised: the node-id path from the trigger toward the area, in plain text."""

    rule: int
    level: str
    path: tuple[str, ...]
    label: str
    subjects: tuple[str, ...]  # node ids this reason raised (area, waterway, spots)


@dataclass
class Assessment:
    area_level: str
    waterway_levels: dict[str, str] = field(default_factory=dict)  # water:<id> -> level
    spot_levels: dict[str, str] = field(default_factory=dict)  # spot:<id> -> level
    reasons: list[Reason] = field(default_factory=list)  # highest level first
    precautions: list[dict] = field(default_factory=list)  # {"kind": "hazard"|"avoid", "text", ...}
    help_order: list[dict] = field(default_factory=list)  # help lines and shelters, in display order

    def reasons_for(self, node: str) -> list[Reason]:
        return [r for r in self.reasons if node in r.subjects]

    def node_levels(self) -> dict[str, str]:
        """Level of every node the rules raised (triggers included), for drawing the graph."""
        out: dict[str, str] = {}
        for r in self.reasons:
            for n in (r.path[0], *r.subjects):
                out[n] = _max(out.get(n, "low"), r.level)
        out.update({k: v for k, v in self.waterway_levels.items()})
        out.update({k: v for k, v in self.spot_levels.items()})
        return out


def alert_level(event: str, thresholds=default_thresholds) -> str | None:
    """Rule 1's level for an NWS event name (case-insensitive), or None for a non-flood event."""
    levels = {k.lower(): v for k, v in thresholds.ALERT_EVENT_LEVELS.items()}
    return levels.get((event or "").strip().lower())


def alert_hazard(event: str) -> str:
    """Precaution hazard key for a flood alert: "Flash Flood ..." is flash_flood, anything else flood."""
    return "flash_flood" if (event or "").strip().lower().startswith("flash flood") else "flood"


def _rank(level: str) -> int:
    return default_thresholds.LEVELS.index(level)


def _max(*levels: str) -> str:
    return max(levels, key=_rank)


def _ft(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _nodes(G: nx.DiGraph, kind: str) -> list[str]:
    return [n for n, d in G.nodes(data=True) if d.get("type") == kind]


def _label(G: nx.DiGraph, n: str) -> str:
    return G.nodes[n].get("label") or n


def _spot_time(G: nx.DiGraph, spot: str) -> datetime | None:
    return parse_time(G.nodes[spot].get("date"))


def _spots_newest_first(G: nx.DiGraph, water: str) -> list[str]:
    spots = [s for s in G.successors(water) if G.edges[water, s].get("rel") == "floods"]
    return sorted(spots, key=lambda s: (_spot_time(G, s) is not None, _spot_time(G, s) or datetime.min),
                  reverse=True)


def _years_before(now: datetime, years: int) -> datetime:
    try:
        return now.replace(year=now.year - years)
    except ValueError:  # Feb 29
        return now.replace(year=now.year - years, day=28)


def help_order(places: dict, area_level: str, thresholds=default_thresholds) -> list[dict]:
    """Rule 5: help lines and shelters (nearest first, no distance last), each tagged with ``entry``.

    Shelters come above the help lines when ``area_level`` is high. Also used by the help page
    (with level low) when the live assessment fails.
    """
    lines = [dict(h, entry="line") for h in places.get("help") or []]
    shelters = sorted((dict(s, entry="shelter") for s in places.get("shelters") or []),
                      key=lambda s: (s.get("distance_mi") is None, s.get("distance_mi") or 0.0))
    return shelters + lines if area_level == thresholds.LEVELS[2] else lines + shelters


def assess(G: nx.DiGraph, places: dict, inputs: build.LiveInputs, thresholds=default_thresholds) -> "Assessment":
    """Apply the five rules to the graph and return the levels, reasons, precautions and help order."""
    low, elevated, high = thresholds.LEVELS
    areas = _nodes(G, "area")
    area = areas[0] if areas else build.area_id(str((places.get("area") or {}).get("zip") or ""))
    area_name = G.nodes[area].get("place", area) if area in G else area
    zip_code = G.nodes[area].get("zip", "") if area in G else ""
    risk = build.risk_id(zip_code)
    reasons: list[Reason] = []
    area_level = low

    # Rule 1: an NWS flood alert covering the area raises it to the alert's level, and also every
    # flood spot reported within AVOID_SPOT_MAX_AGE_YEARS (newest first); older spots keep their level.
    cutoff = _years_before(inputs.now, thresholds.AVOID_SPOT_MAX_AGE_YEARS)
    all_spots = _nodes(G, "spot")
    recent_spots = sorted((s for s in all_spots if (_spot_time(G, s) or datetime.min.replace(tzinfo=cutoff.tzinfo))
                           >= cutoff), key=lambda s: _spot_time(G, s), reverse=True)
    alert_hazards = {"flash_flood": low, "flood": low}
    alert_spots: dict[str, str] = {}  # spot -> highest level any alert raised it to
    alert_causes: dict[str, list[str]] = {}  # spot -> the events that raised it
    for a in _nodes(G, "alert"):
        event = G.nodes[a].get("event") or ""
        a_level = alert_level(event, thresholds)
        if a_level is None or a_level == low or not G.has_edge(a, area):
            continue
        hazard = alert_hazard(event)
        alert_hazards[hazard] = _max(alert_hazards[hazard], a_level)
        reasons.append(Reason(1, a_level, (a, area), f"{event} covers {area_name} → {area_name} {a_level}", (area,)))
        for s in recent_spots:
            reasons.append(Reason(1, a_level, (a, s, area),
                                  f"{event} covers {area_name} → {_label(G, s)} may flood", (s, area)))
            alert_spots[s] = _max(alert_spots.get(s, low), a_level)
            if event not in alert_causes.setdefault(s, []):
                alert_causes[s].append(event)
    flash_flood = alert_hazards["flash_flood"]
    area_level = _max(area_level, *alert_hazards.values())

    # Rule 2: gauge stages and fast rises set each waterway's level.
    waterway_levels = {w: low for w in _nodes(G, "waterway")}
    for w in waterway_levels:
        spots = _spots_newest_first(G, w)
        # The path runs gauge -> waterway -> its newest flood spot -> area (or -> risk with no spots).
        tail = (spots[0], area) if spots else ((risk,) if risk in G else ())
        spot_text = f" → {_label(G, spots[0])} may flood" if spots else ""
        w_name = _label(G, w)
        subjects = (w, *spots, area)
        level, stage_reasons, rise_reasons = low, [], []
        for g in G.predecessors(w):
            gd = G.nodes[g]
            if gd.get("type") != "gauge" or G.edges[g, w].get("rel") != "measures":
                continue
            height, stages = gd.get("level_ft"), gd.get("stages") or {}
            if height is not None:
                # High from the lowest flood stage the NWS set (minor, else moderate, else major);
                # elevated from action, when it is set.
                flood_key = next((k for k in ("minor", "moderate", "major") if stages.get(k) is not None), None)
                g_level, key = low, None
                if flood_key and height >= stages[flood_key]:
                    g_level, key = high, flood_key
                elif stages.get("action") is not None and height >= stages["action"]:
                    g_level, key = elevated, "action"
                if g_level != low:
                    level = _max(level, g_level)
                    stage_name = "action" if key == "action" else f"{key} flood"
                    stage_reasons.append((g_level, g, f"{gd['label']} is {_ft(height)} ft ({stage_name} stage "
                                                      f"{_ft(stages[key])} ft)"))
            rise = gd.get("rise_ft_per_hr")
            if rise is not None and rise > thresholds.RISE_FT_PER_HR:
                rise_reasons.append((g, f"{gd['label']} rose {_ft(rise)} ft in the last hour "
                                        f"(more than {_ft(thresholds.RISE_FT_PER_HR)} ft/h)"))
        for g_level, g, text in stage_reasons:
            reasons.append(Reason(2, g_level, (g, w, *tail), f"{text} → {w_name} {g_level}{spot_text}", subjects))
        if rise_reasons:
            level = thresholds.LEVELS[min(_rank(level) + 1, len(thresholds.LEVELS) - 1)]
            for g, text in rise_reasons:
                reasons.append(Reason(2, level, (g, w, *tail), f"{text} → {w_name} raised to {level}{spot_text}",
                                      subjects))
        waterway_levels[w] = level
        area_level = _max(area_level, level)
    flood = _max(low, alert_hazards["flood"], *waterway_levels.values())

    # Rule 3: heavy rain makes the area and every flood spot elevated.
    rain_level = low
    rain_nodes = _nodes(G, "rain")
    rate, total = inputs.rain_in_per_hr_max, inputs.rain_6h_in
    rain_texts = []
    if rate is not None and rate >= thresholds.RAIN_IN_PER_HR:
        rain_texts.append(f"forecast rain up to {_ft(rate)} in/h ({_ft(thresholds.RAIN_IN_PER_HR)} in/h or more)")
    if total is not None and total >= thresholds.RAIN_6H_IN:
        rain_texts.append(f"{_ft(total)} in of rain forecast in the next {thresholds.RAIN_LOOKAHEAD_HOURS} hours "
                          f"({_ft(thresholds.RAIN_6H_IN)} in or more)")
    if rain_texts:
        rain_level = elevated
        trigger = rain_nodes[0] if rain_nodes else build.rain_id(zip_code)
        text = "; ".join(rain_texts)
        reasons.append(Reason(3, elevated, (trigger, area),
                              f"{text[0].upper()}{text[1:]} → {area_name} {elevated}; flood spots may flood",
                              (area, *all_spots)))
    area_level = _max(area_level, rain_level)

    # Spot levels: the waterway's level, raised to at least the rain level.
    spot_levels = {}
    for s in all_spots:
        w = G.nodes[s].get("waterway")
        spot_levels[s] = _max(waterway_levels.get(w, low) if w else low, rain_level)
    for s, level in alert_spots.items():
        spot_levels[s] = _max(spot_levels[s], level)

    # Precautions: hazard text for each raised hazard's level, then rule 4's avoid list.
    hazards = {"flash_flood": flash_flood, "flood": flood, "heavy_rain": rain_level}
    texts = places.get("precautions") or {}
    raised = [h for h in HAZARD_ORDER if hazards[h] != low]
    raised.sort(key=lambda h: -_rank(hazards[h]))
    precautions = []
    for h in raised or ["flood"]:
        for t in (texts.get(h) or {}).get(hazards[h], []):
            precautions.append({"kind": "hazard", "hazard": h, "level": hazards[h], "text": t})
    # Avoid list: recent spots on an elevated or high waterway (rule 4), and every recent
    # spot under an NWS flood alert (rule 1). Each spot once, newest first.
    avoid = []
    for s in recent_spots:
        w = G.nodes[s].get("waterway")
        on_raised_water = bool(w) and waterway_levels.get(w, low) != low
        if (on_raised_water or s in alert_spots) and G.has_edge(s, build.AVOID_NODE):
            avoid.append(s)
    for s in avoid:
        w, when = G.nodes[s].get("waterway"), _spot_time(G, s)
        causes = list(alert_causes.get(s, []))
        if w and waterway_levels.get(w, low) != low:
            causes.append(f"{_label(G, w)} is {waterway_levels[w]}")
        precautions.append({
            "kind": "avoid", "spot": s, "waterway": w, "level": spot_levels[s],
            "date": G.nodes[s].get("date"),
            "text": f"Avoid {_label(G, s)} (flooded {when.strftime('%b %Y')}; {'; '.join(causes)})",
        })

    # Rule 5: help order. Shelters nearest first; above the help lines when the area is high.
    ordered_help = help_order(places, area_level, thresholds)

    reasons.sort(key=lambda r: -_rank(r.level))
    return Assessment(
        area_level=area_level,
        waterway_levels=waterway_levels,
        spot_levels=spot_levels,
        reasons=reasons,
        precautions=precautions,
        help_order=ordered_help,
    )
