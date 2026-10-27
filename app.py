"""FloodSense app shell: page config, header banner, breaking-alert banner, navigation, the shared sidebar ZIP, logging and the places prebuild."""

import re

import streamlit as st

import app_logging
import config
import ui_shared
from data import places
from data.location import INVALID_ZIP_MESSAGE, InvalidZipError, resolve_zip

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"

st.set_page_config(page_title="FloodSense", page_icon=":material/flood:", layout="wide")

# Header banner. No native element draws a colored band, so this is a small CSS-only block (no JavaScript).
st.html(
    """
<style>
.fs-banner { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap;
  padding: 1.1rem 1.5rem; border-radius: 0.75rem; color: #ffffff;
  background: linear-gradient(100deg, #0b3d91 0%, #0e6ba8 55%, #0f9d9a 100%); }
.fs-brand { display: flex; align-items: center; gap: 0.85rem; }
.fs-icon { font-size: 2.4rem; line-height: 1; }
.fs-name { font-size: 1.9rem; font-weight: 700; letter-spacing: 0.01em; line-height: 1.1; }
.fs-tagline { font-size: 1rem; opacity: 0.92; }
.fs-badge { font-size: 0.8rem; padding: 0.3rem 0.7rem; border-radius: 999px;
  background: rgba(255, 255, 255, 0.16); border: 1px solid rgba(255, 255, 255, 0.35); white-space: nowrap; }
</style>
<div class="fs-banner" role="banner">
  <div class="fs-brand">
    <span class="fs-icon" aria-hidden="true">&#127754;</span>
    <div>
      <div class="fs-name">FloodSense</div>
      <div class="fs-tagline">Live flood risk, explained, for any US ZIP</div>
    </div>
  </div>
  <div class="fs-badge">Congressional App Challenge &middot; NJ-07</div>
</div>
"""
)

page = st.navigation(
    [
        st.Page("app_pages/intelligence.py", title="View environmental intelligence", icon=":material/hub:",
                url_path="intelligence", default=True),
        st.Page("app_pages/forecast.py", title="Search weather forecast", icon=":material/search:",
                url_path="forecast"),
        st.Page("app_pages/map.py", title="Flood map", icon=":material/map:", url_path="map"),
        st.Page("app_pages/alerts.py", title="Flood alerts", icon=":material/warning:", url_path="alerts"),
        st.Page("app_pages/news.py", title="Local news", icon=":material/newspaper:", url_path="news"),
        st.Page("app_pages/phone_alerts.py", title="Get phone alerts", icon=":material/notifications:",
                url_path="phone-alerts"),
        st.Page("app_pages/help.py", title="Get help", icon=":material/call:", url_path="help"),
    ],
    position="sidebar",
)

# The sidebar ZIP box sits right under the navigation; it is filled in below, once logging is set up.
zip_area = st.sidebar.container()

# Debug log lines are written to the daily log file only when this sidebar switch is on (off by default).
show_debug = st.sidebar.toggle("Show debug messages", value=True, key="debug")
log = app_logging.configure(show_debug)


@st.cache_resource(show_spinner=False)
def prebuild_default_places():
    """Build or refresh the default ZIP's places data once per process, in the background."""
    try:
        return places.start_prebuild(config.DEFAULT_ZIP)
    except Exception:  # a failed prebuild must never block the page
        log.exception("Places prebuild could not start")
        return None


prebuild_default_places()

# One ZIP for every page: st.session_state["current_zip"] holds the last ZIP that resolved.
st.session_state.setdefault("current_zip", config.DEFAULT_ZIP)

# A phone push opens /alerts?zip=<zip>: use that ZIP once (per link value), if it resolves.
_query_zip = str(st.query_params.get("zip") or "").strip()
if _query_zip and st.session_state.get("query_zip_seen") != _query_zip:
    st.session_state["query_zip_seen"] = _query_zip
    if re.fullmatch(r"\d{5}", _query_zip):
        try:
            st.session_state["current_zip"] = resolve_zip(_query_zip).zip
        except InvalidZipError:
            pass
        except Exception:
            log.exception("ZIP lookup failed for the link ZIP %r", _query_zip)

# The box follows current_zip whenever something else changed it (a push link, a phone registration).
# This runs before the box is drawn, the only time its value may be set from code.
if st.session_state.get("zip_synced") != st.session_state["current_zip"]:
    st.session_state[ui_shared.ZIP_KEY] = st.session_state["zip_synced"] = st.session_state["current_zip"]

banner_loc = shell_loaded = None  # the resolved sidebar ZIP and its assessment, for the breaking-alert banner
with zip_area:
    zip_code = st.text_input("ZIP code", max_chars=5, key=ui_shared.ZIP_KEY, icon=":material/location_on:",
                             placeholder="e.g. 08807")
    try:
        loc = resolve_zip(zip_code)
    except InvalidZipError:
        st.error(INVALID_ZIP_MESSAGE)
    except Exception:
        log.exception("ZIP lookup failed for %r", zip_code)
        st.caption(LOOKUP_UNAVAILABLE)
    else:
        # Only a ZIP that resolves is saved, so no page opens into an error.
        st.session_state["current_zip"] = st.session_state["zip_synced"] = loc.zip
        banner_loc = loc
        with st.spinner("Checking flood risk..."):
            # One cached assessment per rerun, shared by the risk badge and the breaking-alert banner.
            shell_loaded = ui_shared.load_area(loc)  # never raises; None when unavailable
            level = ui_shared.area_risk(loc, shell_loaded)  # None shows "Risk unavailable"
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"**{ui_shared.md(loc.place)}**")
            ui_shared.risk_badge(level)

# Breaking-alert banner: under the header on every page, from the cached assessment (never raises).
if banner_loc is not None:
    ui_shared.breaking_alert(banner_loc, shell_loaded, current_page=page.url_path)

page.run()
