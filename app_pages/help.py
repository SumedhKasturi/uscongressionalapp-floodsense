"""Get help: one-tap emergency lines, the ZIP's NWS office and open FEMA shelters, nearest first.

Contacts and shelters come from the cached places record alone, so the page still renders
when live data or the network is down. The live assessment only sets the risk banner and
the order (shelters first when the area is high).
"""

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

import config
from app_logging import log
from assessment_loader import load_assessment, load_places
from data import places
from data.location import InvalidZipError, resolve_zip
from graph import reason
from ui_shared import ASK_FOR_ZIP, ASK_ICON, entered_zip, is_open

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"  # same messages as the other pages
RISK_UNAVAILABLE = "Live risk unavailable"
ALERTS_UNCHECKED = "NWS alerts could not be checked"
SHELTERS_STALE_TITLE = "Shelter list may be out of date"
SHELTERS_STALE = "Shelters could not be refreshed, so this list may have changed. Call 211 to confirm."
SHELTERS_FAILED_TITLE = "Shelter list unavailable"
SHELTERS_FAILED = "Shelters could not be checked. Call 211 for shelter information."
ALERTS_LINK = "See flood alerts and roads to avoid"
EASTERN = ZoneInfo("America/New_York")
CALL_TITLE = "Call for help"
SHELTERS_TITLE = "Open shelters"
NO_SHELTERS = f"No open shelters within {config.SHELTER_RADIUS_MI} miles right now"
MAPS_URL = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"

# Banner element, title and icon for each raised risk level (no banner when low).
BANNERS = {
    "elevated": (st.warning, "Elevated risk", ":material/warning:"),
    "high": (st.error, "High risk", ":material/emergency_home:"),
}
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|$~])")


def md(text) -> str:
    """Escape Markdown so place and shelter names render as plain words."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or ""))


def tel_url(number) -> str:
    """One-tap call link: digits only, e.g. ``tel:18006213362``."""
    return "tel:" + re.sub(r"\D", "", str(number or ""))


def maps_url(shelter: dict) -> str:
    return MAPS_URL.format(lat=shelter["lat"], lon=shelter["lon"])


def verified_text(iso) -> str | None:
    """'Verified Oct 25, 2026' for an ISO date, or None when there is none."""
    try:
        d = date.fromisoformat(str(iso))
    except ValueError:
        return None
    return f"Verified {d:%b} {d.day}, {d.year}"


def fmt_time(iso) -> str:
    """Eastern time with an "ET" suffix, like the other pages."""
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso or "")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(EASTERN).strftime("%b %d, %I:%M %p ET")


def show_lines(lines: list[dict]) -> None:
    st.subheader(CALL_TITLE)
    for i, h in enumerate(lines):
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                with st.container():
                    st.markdown(f"**{md(h.get('name'))}**")
                    st.markdown(md(h.get("number")))
                    verified = verified_text(h.get("verified_on")) if h.get("verified_on") else None
                    if verified:
                        st.caption(verified)
                st.link_button(f"Call {h.get('number')}", tel_url(h.get("number")), icon=":material/call:",
                               type="primary" if h.get("kind") == "emergency" else "secondary",
                               key=f"help_call_{i}")


def show_shelters(shelters: list[dict], record: dict) -> None:
    st.subheader(SHELTERS_TITLE)
    status = (record.get("status") or {}).get("shelters")
    if status == places.FAILED:
        st.warning(SHELTERS_FAILED, title=SHELTERS_FAILED_TITLE, icon=":material/cloud_off:")
        return
    if record.get("built_at"):
        st.caption(f"Shelter list as of {fmt_time(record['built_at'])}")
    if record.get("stale") or status == places.CARRIED:
        st.warning(SHELTERS_STALE, title=SHELTERS_STALE_TITLE, icon=":material/history:")
    shelters = [s for s in shelters if is_open(s)]
    if not shelters:
        st.info(NO_SHELTERS, icon=":material/info:")
        return
    for i, s in enumerate(shelters):
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                with st.container():
                    st.markdown(f"**{md(s.get('name') or 'Shelter')}**")
                    address = ", ".join(str(p) for p in (s.get("address"), s.get("city"), s.get("state")) if p)
                    if address:
                        st.markdown(md(address))
                    if s.get("status"):
                        st.caption(f"Status: {str(s['status']).strip().capitalize()}")
                    if s.get("distance_mi") is not None:
                        st.caption(f"{s['distance_mi']:.1f} mi away")
                if s.get("lat") is not None and s.get("lon") is not None:
                    st.link_button("Open in maps", maps_url(s), icon=":material/map:", key=f"help_map_{i}")


st.caption("Emergency phone numbers and open shelters near your ZIP, one tap away.")

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

with st.spinner("Loading help..."):
    # Places first: contacts and shelters must show even when live data fails.
    try:
        record = load_places(loc)
    except Exception:
        log.exception("Places could not be loaded for %s; using the fixed help lines", loc.zip)
        record = places.fallback_record(loc)
    assessment = inputs = None
    # With only the fallback record (nothing could be fetched), skip the live assessment:
    # it would retry every source and double the wait offline.
    if not record.get("all_sources_failed"):
        try:
            _, inputs, _, assessment = load_assessment(loc)
        except Exception:
            log.exception("Live risk could not be assessed for %s on the help page", loc.zip)

st.subheader(f"{loc.place} ({loc.zip})")

if assessment is not None:
    order = assessment.help_order
    if assessment.area_level in BANNERS:
        banner, title, icon = BANNERS[assessment.area_level]
        top = assessment.reasons[0].label if assessment.reasons else title
        banner(md(top), title=title, icon=icon)
        with st.container(horizontal=True):
            if assessment.area_level == "high":
                st.link_button("Call 911 now", "tel:911", type="primary", icon=":material/call:",
                               key="help_call_911_now")
            st.page_link("app_pages/alerts.py", label=ALERTS_LINK, icon=":material/warning:")
    if inputs is not None and inputs.alerts_unavailable:
        st.caption(ALERTS_UNCHECKED)
else:
    order = reason.help_order(record, "low")
    st.caption(RISK_UNAVAILABLE)

lines = [h for h in order if h.get("entry") == "line"]
shelters = [h for h in order if h.get("entry") == "shelter"]
shelters_first = bool(order) and order[0].get("entry") == "shelter"

if shelters_first:
    show_shelters(shelters, record)
    show_lines(lines)
else:
    show_lines(lines)
    show_shelters(shelters, record)
