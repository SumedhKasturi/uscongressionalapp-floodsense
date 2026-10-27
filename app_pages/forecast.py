"""Search weather forecast: a dashboard of the ZIP's flood risk, next 12 hours, active alerts and nearby river gauges."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from app_logging import log
from data import sources
from data.location import InvalidZipError, Location, resolve_zip
from ui_shared import (ASK_FOR_ZIP, ASK_ICON, FLOOD_BADGES, NO_FLOOD_STATUS, RISK_STYLES, area_risk, entered_zip,
                       md)

UNAVAILABLE = "Live data unavailable"
NO_ALERTS = "No active alerts"
NO_GAUGES = "No active stream gauges within about 35 miles"
LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"
EASTERN = ZoneInfo("America/New_York")
FLOOD_LABELS = {
    "no_flooding": "No flooding",
    "action": "Action",
    "minor": "Minor",
    "moderate": "Moderate",
    "major": "Major",
}
GAUGE_CARD_WIDTH = 270
TEMP_COLOR = "#e8833a"
RAIN_COLOR = "#0b5cad"


def _fresh(result) -> bool:
    fresh = result is not None and not result.stale
    if fresh:
        log.debug("Data is fresh")
    return fresh


@st.cache_data(ttl=300, show_spinner=False)
def load_forecast(loc):
    point = sources.nws_point(loc)
    hourly = sources.nws_hourly(loc, point)
    # Raw rain amounts (mm) are cached now for the risk rules in a later story.
    sources.nws_gridpoint_qpf(loc, point)
    log.info("Forecast data loaded")
    return point, hourly


@st.cache_data(ttl=300, show_spinner=False)
def load_alerts(lat, lon, zip_code):
    log.info("Alerts data loaded")
    return sources.nws_alerts(lat, lon, zip_code)


@st.cache_data(ttl=300, show_spinner=False)
def load_gauges(lat, lon, zip_code):
    log.info("Gauges data loaded")
    return sources.gauges_near(Location(zip=zip_code, lat=lat, lon=lon, place=""))


def fmt_time(iso: str | None) -> str:
    """Every displayed time is shown in Eastern time with an "ET" suffix."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        log.error("Failed to parse time")
        return str(iso)    
    log.info("Time parsed successfully")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    log.info("Time parsed successfully")
    return t.astimezone(EASTERN).strftime("%b %d, %I:%M %p ET")


def stale_notice(result) -> None:
    if result is not None and result.stale:
        st.warning(f"Data as of {fmt_time(result.fetched_at)}", icon=":material/history:")
        log.debug("Stale data notice displayed")


def gauge_status(g) -> str:
    status = g.status()
    if status == "cached":
        return f"cached (as of {fmt_time(g.fetched_at)})"
    log.info("Gauge status checked")
    return status


def rain_chance(p: dict):
    """The period's rain chance (%), or None when unknown (never read as 0%)."""
    return (p.get("probabilityOfPrecipitation") or {}).get("value")


def hour_label(p: dict, fallback: str) -> str:
    """Short Eastern hour for the chart axis, e.g. "3 PM"."""
    try:
        t = datetime.fromisoformat(p.get("startTime"))
    except (TypeError, ValueError):
        return fallback
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(EASTERN)
    return f"{t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"


def hourly_chart(periods: list[dict], rows: list[dict]) -> alt.LayerChart:
    """Rain-chance bars (right axis, 0-100%) under a temperature line (left axis), with tooltips."""
    data = pd.DataFrame([
        {"Hour": hour_label(p, r["Time"]), "Time": r["Time"], "Temp": r["Temp (°F)"],
         "Rain": r["Rain chance (%)"], "Forecast": r["Forecast"]}
        for p, r in zip(periods, rows)
    ])
    tooltip = [alt.Tooltip("Time:N", title="Time"), alt.Tooltip("Temp:Q", title="Temp (°F)"),
               alt.Tooltip("Rain:Q", title="Rain chance (%)"), alt.Tooltip("Forecast:N", title="Forecast")]
    base = alt.Chart(data).encode(
        x=alt.X("Hour:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)), tooltip=tooltip
    )
    bars = base.mark_bar(color=RAIN_COLOR, opacity=0.35, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        y=alt.Y("Rain:Q", title="Rain chance (%)", scale=alt.Scale(domain=[0, 100]),
                axis=alt.Axis(orient="right", titleColor=RAIN_COLOR))
    )
    line = base.mark_line(color=TEMP_COLOR, strokeWidth=3, point=alt.OverlayMarkDef(color=TEMP_COLOR, size=50)).encode(
        y=alt.Y("Temp:Q", title="Temperature (°F)", scale=alt.Scale(zero=False),
                axis=alt.Axis(orient="left", titleColor=TEMP_COLOR))
    )
    return alt.layer(bars, line).resolve_scale(y="independent").properties(height=300)


def gauge_cards(gauges, table: pd.DataFrame) -> None:
    """One bordered card per gauge, nearest first, in a grid that wraps to the screen width."""
    with st.container(horizontal=True, gap="small"):
        for g, row in zip(gauges, table.to_dict("records")):
            label, color, icon = FLOOD_BADGES.get(g.flood_category or "", NO_FLOOD_STATUS)
            with st.container(border=True, width=GAUGE_CARD_WIDTH, height="stretch", gap="small"):
                st.markdown(f"**{md(g.name or g.site)}**")
                st.metric("Level", row["Level"])
                st.badge(label, color=color, icon=icon)
                distance = row["Distance (mi)"]
                st.caption(" · ".join(x for x in (
                    None if distance is None or pd.isna(distance) else f"{distance:.1f} mi away",
                    f"Status: {row['Status']}",
                ) if x))
                if row["Reading time"]:
                    st.caption(f"Reading {row['Reading time']}")


st.caption("Live weather, alerts and stream levels for your ZIP.")

# The ZIP comes from the sidebar box in the app shell, shared by every page through
# st.session_state["current_zip"]; the sidebar shows the invalid-ZIP message itself.
zip_code = entered_zip()

try:
    loc = resolve_zip(zip_code)
    log.info("ZIP code resolved successfully")
except InvalidZipError:
    st.info(ASK_FOR_ZIP, icon=ASK_ICON)
    log.error("Invalid ZIP code entered")
    st.stop()
except Exception:
    log.error("ZIP code lookup unavailable")
    st.error(LOOKUP_UNAVAILABLE)
    st.stop()

st.subheader(f"{loc.place} ({loc.zip})")
st.caption(f"{loc.lat:.4f}, {loc.lon:.4f}")
log.info("Location details displayed")

# Top row of metric cards; filled in at the end, once every section below has its data.
kpi_row = st.container(horizontal=True, gap="small")

with st.spinner("Loading live data..."):
    point, hourly = load_forecast(loc)
    log.info("Forecast loaded successfully")
    alerts = load_alerts(loc.lat, loc.lon, loc.zip)
    log.info("Alerts loaded successfully")
    gauges = load_gauges(loc.lat, loc.lon, loc.zip)
    log.info("Gauges loaded successfully")

# Only fresh results stay memoized, so the page recovers as soon as the network does.
if not (_fresh(point) and _fresh(hourly)):
    load_forecast.clear()
    log.debug("Forecast cache cleared")
if not _fresh(alerts):
    load_alerts.clear()
    log.debug("Alerts cache cleared")
if gauges is None or any(g.stale or g.fetch_failed for g in gauges):
    load_gauges.clear()
    log.debug("Gauges cache cleared")

st.header("Active alerts")
if alerts is None:
    st.error(UNAVAILABLE)
else:
    stale_notice(alerts)
    summaries = sources.alert_summaries(alerts)
    log.info("Alert summaries generated")
    if not summaries:
        if alerts.stale:
            st.info(f"{NO_ALERTS} as of {fmt_time(alerts.fetched_at)} (could not refresh)")
        else:
            st.success(NO_ALERTS, icon=":material/check_circle:")
    if summaries:
        with st.container(gap="small"):
            for a in summaries:
                with st.container(border=True, gap="xsmall"):
                    st.markdown(f"**{a['event'] or 'Alert'}** · {a['severity'] or 'Unknown'} severity")
                    st.write(a["headline"])

st.header("Next 12 hours")
periods = sources.hourly_periods(hourly, 12)
if hourly is None or not periods:
    st.error(UNAVAILABLE)
else:
    stale_notice(hourly)
    log.info("Hourly periods generated")
    rows = [
        {
            "Time": fmt_time(p.get("startTime")),
            "Temp (°F)": p.get("temperature"),
            # Unknown rain chance stays blank; it must never read as 0%.
            "Rain chance (%)": (p.get("probabilityOfPrecipitation") or {}).get("value"),
            "Forecast": p.get("shortForecast", ""),
        }
        for p in periods
    ]
    st.altair_chart(hourly_chart(periods, rows), width="stretch",
                    alt="Temperature and rain chance for each of the next 12 hours")
    with st.expander("Hourly details", icon=":material/table:"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, alt="Hourly forecast for the next 12 hours")
    log.info("Hourly forecast displayed")

st.header(f"Stream gauges near {loc.place}")
if gauges is None:
    st.error(UNAVAILABLE)
elif not gauges:
    st.info(NO_GAUGES)
else:
    stale = [g for g in gauges if g.stale]
    if stale:
        st.warning(f"Data as of {fmt_time(min(g.fetched_at for g in stale))}", icon=":material/history:")
    gauge_table = pd.DataFrame(
        [
            {
                "Gauge": g.name,
                "Site": g.site,
                "Distance (mi)": None if g.distance_mi is None else round(g.distance_mi, 1),
                "Level": g.display,
                "Reading time": fmt_time(g.time),
                "Flood status": FLOOD_LABELS.get(g.flood_category or "", ""),
                "Status": gauge_status(g),
            }
            for g in gauges
        ]
    )
    gauge_cards(gauges, gauge_table)
    with st.expander("All gauges as a table", icon=":material/table:"):
        st.dataframe(
            gauge_table,
            hide_index=True,
            column_config={"Distance (mi)": st.column_config.NumberColumn(format="%.1f")},
            alt="Nearest river gauges and their latest levels",
        )
    log.info("Gauge data displayed")

# --- Top row: four metric cards, all without a delta so they line up ---------------------------
level = area_risk(loc)  # cached assessment; never raises, None when unavailable
temp_now = periods[0].get("temperature") if hourly is not None and periods else None
rain_known = [v for v in (rain_chance(p) for p in periods) if v is not None] if hourly is not None else []
with kpi_row:
    st.metric("Flood risk", RISK_STYLES[level][0].removesuffix(" risk") if level else "Unavailable",
              icon=RISK_STYLES[level][2] if level else ":material/help:", border=True)
    st.metric("Temperature now", "Unavailable" if temp_now is None else f"{temp_now}°F",
              icon=":material/thermostat:", border=True)
    st.metric("Max rain chance next 12 h", f"{round(max(rain_known))}%" if rain_known else "Unavailable",
              icon=":material/rainy:", border=True)
    st.metric("Active alerts", "Unavailable" if alerts is None else len(summaries),
              icon=":material/notifications_active:", border=True)
