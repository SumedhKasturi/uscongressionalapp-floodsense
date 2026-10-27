"""Flood alerts: the ZIP's risk level, each active NWS flood alert with its roads, precautions and "Why this alert?"."""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from app_logging import log
from assessment_loader import load_assessment
from data.location import InvalidZipError, resolve_zip
from ui_shared import ASK_FOR_ZIP, ASK_ICON, entered_zip
from graph import build, reason, thresholds
from graph.explain import chain, node_name

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"  # same messages as the other pages
UNAVAILABLE = "Live data unavailable"
NO_ALERTS = "No active alerts"
NO_NWS_ALERTS = "No active NWS alerts"
NO_FLOOD_ALERTS = "No active flood alerts"
ALERTS_UNAVAILABLE_TITLE = "Alerts could not be checked"
ALERTS_UNAVAILABLE = "NWS alerts are unavailable right now. Risk shown is from river gauges and rain only."
STALE_UNKNOWN = "Some live data could not be refreshed; cached readings were used."
CONDITIONS_TITLE = "River and rain conditions"
WHY = "Why this alert?"
MAX_ROADS = 8
EASTERN = ZoneInfo("America/New_York")

# Banner element, title and icon for each risk level.
BANNERS = {
    "low": (st.success, "Low risk", ":material/check_circle:"),
    "elevated": (st.warning, "Elevated risk", ":material/warning:"),
    "high": (st.error, "High risk", ":material/emergency_home:"),
}
LEVEL_BADGES = {"low": ("green", ":material/check_circle:"), "elevated": ("orange", ":material/warning:"),
                "high": ("red", ":material/emergency_home:")}
SEVERITY_COLORS = {"extreme": "red", "severe": "orange", "moderate": "yellow", "minor": "blue"}
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|$~])")


def md(text) -> str:
    """Escape Markdown so storm-report and alert text renders as plain words."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or ""))


def fmt_time(iso: str | None) -> str:
    """Eastern time with an "ET" suffix, like the forecast page."""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso or "")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(EASTERN).strftime("%b %d, %I:%M %p ET")


def _rank(level: str) -> int:
    return thresholds.LEVELS.index(level)


def level_badge(level: str) -> None:
    color, icon = LEVEL_BADGES[level]
    st.badge(f"Level: {level}", color=color, icon=icon)


def recent_spots(G, now) -> list[str]:
    """Flood spots reported within AVOID_SPOT_MAX_AGE_YEARS, newest first."""
    cutoff = reason._years_before(now, thresholds.AVOID_SPOT_MAX_AGE_YEARS)
    dated = [(reason._spot_time(G, n), n) for n, d in G.nodes(data=True) if d.get("type") == "spot"]
    return [n for t, n in sorted(((t, n) for t, n in dated if t is not None and t >= cutoff), reverse=True)]


def show_roads(items: list[str], any_recent: bool) -> None:
    st.markdown("**Affected roads**")
    if not items:
        st.caption("No roads to avoid for this alert." if any_recent else
                   f"No flood spots reported here in the last {thresholds.AVOID_SPOT_MAX_AGE_YEARS} years.")
        return
    st.markdown("\n".join(f"- {md(t)}" for t in items[:MAX_ROADS]))
    if len(items) > MAX_ROADS:
        st.caption(f"and {len(items) - MAX_ROADS} more")


def show_precautions(record: dict, hazard: str, level: str) -> None:
    texts = ((record.get("precautions") or {}).get(hazard) or {}).get(level) or []
    if texts:
        st.markdown("**What to do**")
        st.markdown("\n".join(f"- {md(t)}" for t in texts))


def show_why(G, reasons: list) -> None:
    with st.expander(WHY, icon=":material/help:"):
        for r in reasons:
            st.markdown(md(chain(G, r)))
            st.caption(md(r.label))


st.caption("Active flood alerts for your ZIP, the roads they affect, what to do, and why.")

# The ZIP comes from the sidebar box in the app shell (which also handles the push link's ?zip=).
zip_code = entered_zip()

try:
    loc = resolve_zip(zip_code)
except InvalidZipError:
    st.info(ASK_FOR_ZIP, icon=ASK_ICON)
    st.stop()
except Exception:
    st.error(LOOKUP_UNAVAILABLE)
    st.stop()

with st.spinner("Checking flood alerts..."):
    try:
        record, inputs, G, assessment = load_assessment(loc)
    except Exception:
        log.exception("Flood alerts could not be assessed for %s", loc.zip)
        st.error(UNAVAILABLE)
        st.stop()

st.subheader(f"{loc.place} ({loc.zip})")

# Risk banner: the area's level, with the top reason.
banner, title, icon = BANNERS[assessment.area_level]
top = assessment.reasons[0].label if assessment.reasons else f"Nothing is raising the flood risk for {loc.place}."
if inputs.alerts_unavailable:
    # With alerts unknown, never show a green all-clear; a raised level still gets its banner.
    if assessment.area_level != "low":
        banner(md(top), title=title, icon=icon)
    st.warning(ALERTS_UNAVAILABLE, title=ALERTS_UNAVAILABLE_TITLE, icon=":material/cloud_off:")
else:
    banner(md(top), title=title, icon=icon)

if inputs.stale:
    st.caption(f"Data as of {fmt_time(inputs.as_of)}" if inputs.as_of else STALE_UNKNOWN)

# Sort the live alerts into flood alerts (rule 1 gives them a level) and other alerts.
flood_alerts, other_alerts = [], []
for i, a in enumerate(inputs.alerts):
    node = build.alert_id(i, a.get("event") or "")
    level = reason.alert_level(a.get("event") or "")
    (flood_alerts if level else other_alerts).append((node, a, level))
flood_alerts.sort(key=lambda t: -_rank(t[2]))

avoid = [p for p in assessment.precautions if p["kind"] == "avoid"]
avoid_by_spot = {p["spot"]: p for p in avoid}
recent = recent_spots(G, inputs.now)
condition_reasons = [r for r in assessment.reasons if r.rule in (2, 3)]

for node, a, level in flood_alerts:
    event = a.get("event") or "Alert"
    card_reasons = [r for r in assessment.reasons if r.path[0] == node]
    spots = [r.path[1] for r in card_reasons if len(r.path) == 3]
    with st.container(border=True):
        st.markdown(f"**{md(event)}**")
        with st.container(horizontal=True):
            severity = a.get("severity") or "Unknown"
            st.badge(f"{severity} severity", color=SEVERITY_COLORS.get(severity.lower(), "gray"),
                     icon=":material/crisis_alert:")
            level_badge(level)
        if a.get("headline"):
            st.markdown(md(a["headline"]))
        show_roads([avoid_by_spot[s]["text"] for s in spots if s in avoid_by_spot], bool(recent))
        show_precautions(record, reason.alert_hazard(event), level)
        show_why(G, card_reasons)

if not flood_alerts and not inputs.alerts_unavailable:
    if condition_reasons:
        none = NO_FLOOD_ALERTS if other_alerts else NO_NWS_ALERTS
        st.info(f"{none}. River and rain conditions are raising the risk.", icon=":material/info:")
    elif other_alerts:
        st.info(NO_FLOOD_ALERTS, icon=":material/check_circle:")
    else:
        st.info(NO_ALERTS, icon=":material/check_circle:")

# River and rain conditions: what gauges and rain raised, with or without an alert.
if condition_reasons:
    low = thresholds.LEVELS[0]
    raised_water = [w for w, lv in assessment.waterway_levels.items() if lv != low]
    raised_water.sort(key=lambda w: -_rank(assessment.waterway_levels[w]))
    rain_raised = any(r.rule == 3 for r in condition_reasons)
    level = max((r.level for r in condition_reasons), key=_rank)
    with st.container(border=True):
        st.markdown(f"**{CONDITIONS_TITLE}**")
        level_badge(level)
        lines = [f"- {md(node_name(G, w))}: {assessment.waterway_levels[w]}" for w in raised_water]
        if rain_raised:
            lines.append(f"- Heavy rain forecast: {thresholds.LEVELS[1]}")
        st.markdown("\n".join(lines))
        if rain_raised:
            # Rule 3 raises every flood spot: list the recent ones, newest first.
            roads = [avoid_by_spot[s]["text"] if s in avoid_by_spot else
                     f"{node_name(G, s)} may flood in heavy rain "
                     f"(flooded {reason._spot_time(G, s).strftime('%b %Y')})" for s in recent]
        else:
            roads = [p["text"] for p in avoid if p.get("waterway") in raised_water]
        show_roads(roads, bool(recent))
        if raised_water:
            show_precautions(record, "flood", assessment.waterway_levels[raised_water[0]])
        if rain_raised:
            show_precautions(record, "heavy_rain", thresholds.LEVELS[1])
        show_why(G, condition_reasons)

if other_alerts:
    st.markdown("**Other alerts**")
    st.markdown("\n".join(
        f"- **{md(a.get('event') or 'Alert')}** · {md(a.get('severity') or 'Unknown')} severity"
        + (f": {md(a['headline'])}" if a.get("headline") else "")
        for _, a, _ in other_alerts
    ))
