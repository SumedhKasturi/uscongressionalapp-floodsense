"""Readable "Why" chains for an assessment's reasons, shared by the Flood alerts page and the push job.

Also the pure status helpers the pages (through ui_shared) and the graph query agent share:
node type names, gauge flood status, shelter status and the "low but unconfirmed" check.
"""

from __future__ import annotations

import math

import networkx as nx

from graph import reason

# Display name of each node type (the knowledge graph page's legend).
TYPE_NAMES = {
    "area": "Area", "alert": "NWS alert", "rain": "Rain forecast", "gauge": "Stream gauge", "waterway": "Waterway",
    "spot": "Flood spot", "road": "Road", "storm event": "Storm event", "disaster": "FEMA disaster",
    "risk": "Risk level", "precaution": "Precaution", "help": "Help",
}

# Gauge badge per NWS flood category, in the app's risk colors (action raises a waterway
# to elevated, minor and above to high). Shared by the forecast page's gauge cards and the flood map.
FLOOD_BADGES = {
    "no_flooding": ("No flooding", "green", ":material/check_circle:"),
    "action": ("Action stage", "orange", ":material/warning:"),
    "minor": ("Minor flooding", "red", ":material/emergency_home:"),
    "moderate": ("Moderate flooding", "red", ":material/emergency_home:"),
    "major": ("Major flooding", "red", ":material/emergency_home:"),
}
NO_FLOOD_STATUS = ("No flood status", "gray", ":material/remove:")


def number(value) -> float | None:
    """A finite float, or None for anything else (None, NaN, infinity, text, a bool)."""
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def flood_category(level_ft, stages) -> str | None:
    """NWS flood category of a reading against the gauge's official stages, or None when unknown."""
    level_ft = number(level_ft)
    stages = {k: v for k, v in ((k, number(v)) for k, v in stages.items()) if v is not None} \
        if isinstance(stages, dict) else {}
    if level_ft is None or not stages:
        return None
    for name in ("major", "moderate", "minor", "action"):
        if stages.get(name) is not None and level_ft >= stages[name]:
            return name
    return "no_flooding"


def gauge_status(lid, stages, inputs) -> tuple[float | None, tuple[str, str, str]]:
    """(level in ft, badge): NWPS's flood category for the reading, as on the forecast page, else the
    level against the gauge's stages."""
    level_ft = number((getattr(inputs, "gauge_levels", None) or {}).get(lid))
    category = (getattr(inputs, "gauge_categories", None) or {}).get(lid)
    if level_ft is None:
        category = None
    elif category not in FLOOD_BADGES:
        category = flood_category(level_ft, stages)
    return level_ft, FLOOD_BADGES.get(category, NO_FLOOD_STATUS)


def is_open(shelter: dict) -> bool:
    """FEMA status OPEN (any case); a shelter with no status is treated as open."""
    status = shelter.get("status")
    return status is None or str(status).strip() == "" or str(status).strip().upper() == "OPEN"


def low_unconfirmed(inputs, level: str | None) -> bool:
    """True when the rules say low but NWS alerts could not be checked: never shown as an all-clear."""
    return level == "low" and bool(getattr(inputs, "alerts_unavailable", False))


def _ft(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def node_name(G: nx.DiGraph, n: str) -> str:
    """Readable name of a node in a "Why" chain: gauges carry their reading, the area its place name."""
    d = G.nodes[n] if n in G else {}
    if d.get("type") == "gauge" and d.get("level_ft") is not None:
        return f"{d.get('label') or n} {_ft(d['level_ft'])} ft"
    if d.get("type") == "area":
        return d.get("place") or d.get("label") or n
    return d.get("label") or n


def chain(G: nx.DiGraph, r: reason.Reason) -> str:
    """The reason's graph path as readable names joined by arrows, e.g. "Flash Flood Warning → Bridgewater, NJ"."""
    return " → ".join(node_name(G, n) for n in r.path)
