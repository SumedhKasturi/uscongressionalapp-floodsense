"""Flood map: the ZIP's flood spots colored by current risk, its river gauges and open shelters.

Everything comes from the cached places record and assessment (assessment_loader), so the map
adds no fetch. Each layer can be switched off with the pills above the map; every marker has a
plain-text hover tooltip naming the place.
"""

import math

import pandas as pd
import pydeck as pdk
import streamlit as st

import assessment_loader
from app_logging import log
from data import places
from data.location import InvalidZipError, resolve_zip
from data.sources import parse_time
from graph import build, explain
from ui_shared import (ASK_FOR_ZIP, ASK_ICON, FLOOD_BADGES, NO_FLOOD_STATUS, RISK_STYLES, entered_zip, is_open,
                       low_unconfirmed, md)

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"  # same messages as the other pages
UNAVAILABLE = "Live data unavailable"
HISTORY_UNAVAILABLE = "Local flood history is unavailable for this ZIP."
SHELTERS_UNAVAILABLE = "Shelter data is unavailable right now"
LAYERS = ("Flood spots", "Gauges", "Shelters")
LAYER_ICONS = {"Flood spots": ":material/water_drop:", "Gauges": ":material/straighten:",
               "Shelters": ":material/home:"}
MAP_HEIGHT = 560
MAP_ZOOM = 10.5
TOOLTIP_MAX = 80  # each tooltip line shows at most this many characters

# Spot style per risk level; "unknown" when the spot has no assessed level, or the rules say
# low while NWS alerts could not be checked (like the area risk badge).
UNKNOWN = "unknown"
SPOT_RGB = {"low": [96, 125, 139], "elevated": [245, 124, 0], "high": [211, 47, 47], UNKNOWN: [176, 176, 176]}
SPOT_STYLES = {**RISK_STYLES, UNKNOWN: ("Risk unknown", "gray", ":material/help:")}
SPOT_LEGEND = {"low": "blue", "elevated": "orange", "high": "red", UNKNOWN: "gray"}
# Gauge fill per badge color (ui_shared.FLOOD_BADGES, the forecast page's palette).
BADGE_RGB = {"green": [46, 160, 67], "orange": [245, 124, 0], "red": [211, 47, 47], "gray": [140, 140, 140]}
SHELTER_RGB = [123, 31, 162]
GAUGE_LEGEND = (FLOOD_BADGES["no_flooding"], FLOOD_BADGES["action"],
                ("Minor flooding or worse", "red", ":material/emergency_home:"), NO_FLOOD_STATUS)
COLUMNS = ["lat", "lon", "color", "tooltip"]


def number(value) -> float | None:
    """A finite float, or None for anything else (None, NaN, infinity, text, a bool)."""
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def point(item) -> tuple[float, float] | None:
    """(lat, lon) when both are finite and in range, else None ("not mapped")."""
    if not isinstance(item, dict):
        return None
    lat, lon = number(item.get("lat")), number(item.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def has_point(item) -> bool:
    return point(item) is not None


def clip(text) -> str:
    text = str(text or "").strip()
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1].rstrip() + "…"


def tooltip(*lines) -> str:
    """Tooltip text, one line per non-empty part."""
    return "\n".join(t for t in (clip(x) for x in lines) if t)


def spot_date(spot: dict) -> str:
    when = parse_time(spot.get("date")) if isinstance(spot.get("date"), str) else None
    return f"{when:%b} {when.day}, {when.year}" if when else str(spot.get("date") or "")[:10]


def spot_level(spot: dict, spot_levels: dict, inputs) -> str:
    level = spot_levels.get(build.spot_node(str(spot.get("id") or "")))
    if level not in RISK_STYLES or low_unconfirmed(inputs, level):
        return UNKNOWN
    return level


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def spot_rows(spots: list, spot_levels: dict, inputs) -> list[dict]:
    rows = []
    for s in spots:
        try:
            lat, lon = point(s) or (None, None)
            if lat is None:
                continue
            level = spot_level(s, spot_levels, inputs)
            name = build.spot_label({**s, "remark": str(s.get("remark") or "")}, TOOLTIP_MAX)
            rows.append({"lat": lat, "lon": lon, "color": SPOT_RGB[level],
                         "tooltip": tooltip(name, spot_date(s), SPOT_STYLES[level][0])})
        except Exception:  # one malformed spot must not break the map
            log.warning("Flood spot %r skipped on the map", s.get("id") if isinstance(s, dict) else s)
    return rows


def gauge_status(g: dict, inputs) -> tuple[float | None, tuple[str, str, str]]:
    """(level in ft, badge): NWPS's flood category for the reading, as on the forecast page, else the
    level against the gauge's stages."""
    return explain.gauge_status(g.get("lid"), g.get("stages"), inputs)


def gauge_rows(gauges: list, inputs) -> list[dict]:
    rows = []
    for g in gauges:
        try:
            lat, lon = point(g) or (None, None)
            if lat is None:
                continue
            level_ft, (label, color, _) = gauge_status(g, inputs)
            reading = "No reading" if level_ft is None else f"{level_ft:.2f} ft"
            rows.append({"lat": lat, "lon": lon, "color": BADGE_RGB[color],
                         "tooltip": tooltip(g.get("name") or g.get("lid") or "Gauge", f"Level: {reading}", label)})
        except Exception:  # one malformed gauge must not break the map
            log.warning("Gauge %r skipped on the map", g.get("lid") if isinstance(g, dict) else g)
    return rows


def shelter_rows(shelters: list) -> list[dict]:
    rows = []
    for s in shelters:
        try:
            lat, lon = point(s) or (None, None)
            if lat is None or not is_open(s):
                continue
            address = ", ".join(str(p) for p in (s.get("address"), s.get("city"), s.get("state")) if p)
            rows.append({"lat": lat, "lon": lon, "color": SHELTER_RGB,
                         "tooltip": tooltip(s.get("name") or "Shelter", address)})
        except Exception:  # one malformed shelter must not break the map
            log.warning("Shelter %r skipped on the map", s.get("id") if isinstance(s, dict) else s)
    return rows


def layer(layer_id: str, data: pd.DataFrame, radius: int) -> pdk.Layer:
    return pdk.Layer("ScatterplotLayer", id=layer_id, data=data, get_position=["lon", "lat"],
                     get_fill_color="color", get_radius=radius, radius_min_pixels=4, radius_max_pixels=14,
                     stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1, pickable=True)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def section_failed(record: dict, section: str) -> bool:
    return bool(record.get("all_sources_failed")) or (record.get("status") or {}).get(section) == places.FAILED


def items(record: dict, section: str) -> list:
    value = record.get(section)
    return value if isinstance(value, list) else []


st.caption("Where flooding has happened near your ZIP, how high the rivers are now, and open shelters.")

# The ZIP comes from the sidebar box in the app shell, shared by every page.
zip_code = entered_zip()

try:
    loc = resolve_zip(zip_code)
except InvalidZipError:
    st.info(ASK_FOR_ZIP, icon=ASK_ICON)
    st.stop()
except Exception:
    log.exception("ZIP lookup failed for %r", zip_code)
    st.error(LOOKUP_UNAVAILABLE)
    st.stop()

st.subheader(f"{md(loc.place)} ({loc.zip})")

with st.spinner("Loading the flood map..."):
    try:
        record = assessment_loader.load_places(loc)
        inputs = assessment = None
        # With only the fallback record (nothing could be fetched), skip the live assessment:
        # it would retry every source, and there are no spots or gauges to color.
        if not record.get("all_sources_failed"):
            _, inputs, _, assessment = assessment_loader.load_assessment(loc)
    except Exception:
        log.exception("Flood map data could not be loaded for %s", loc.zip)
        st.error(UNAVAILABLE, icon=":material/cloud_off:")
        st.stop()

spots, gauges = items(record, "flood_spots"), items(record, "gauges")
open_shelters = [s for s in items(record, "shelters") if isinstance(s, dict) and is_open(s)]
spot_levels = assessment.spot_levels if assessment is not None else {}
rows = {"Flood spots": spot_rows(spots, spot_levels, inputs), "Gauges": gauge_rows(gauges, inputs),
        "Shelters": shelter_rows(open_shelters)}
not_mapped = len(spots) + len(gauges) + len(open_shelters) - sum(len(r) for r in rows.values())

shown = st.pills("Layers", LAYERS, selection_mode="multi", default=list(LAYERS), key="map_layers",
                 format_func=lambda name: f"{LAYER_ICONS[name]} {name}") or []
layer_ids = {"Flood spots": ("flood_spots", 120), "Gauges": ("gauges", 220), "Shelters": ("shelters", 220)}
layers = [layer(layer_ids[name][0], frame(rows[name]), layer_ids[name][1]) for name in LAYERS if name in shown]

deck = pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(latitude=loc.lat, longitude=loc.lon, zoom=MAP_ZOOM),
    tooltip={"text": "{tooltip}"},
)
st.pydeck_chart(deck, height=MAP_HEIGHT,
                alt=f"Map of flood spots, river gauges and open shelters around {loc.place} ({loc.zip})")
log.info("Flood map displayed for %s", loc.zip)

shelters_failed = section_failed(record, "shelters")
counts = [plural(len(rows["Flood spots"]), "flood spot"), plural(len(rows["Gauges"]), "gauge"),
          "shelters unavailable" if shelters_failed else plural(len(rows["Shelters"]), "open shelter")]
if not_mapped:
    counts.append(f"{not_mapped} not mapped (no location)")
st.caption(" · ".join(counts))
if section_failed(record, "flood_spots"):
    st.caption(HISTORY_UNAVAILABLE)
if shelters_failed:
    st.caption(SHELTERS_UNAVAILABLE)

# Legend: one badge per marker color.
with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.caption("Flood spots")
    for level, color in SPOT_LEGEND.items():
        label, _, icon = SPOT_STYLES[level]
        st.badge(label, color=color, icon=icon)
with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.caption("Gauges")
    for label, color, icon in GAUGE_LEGEND:
        st.badge(label, color=color, icon=icon)
with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.caption("Shelters")
    st.badge("Open shelter", color="violet", icon=":material/home:")
