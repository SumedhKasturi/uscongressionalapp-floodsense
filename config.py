"""FloodSense settings.

Secrets and per-deployment values are read from Streamlit secrets first, then
from the environment. Nothing secret is written here as a literal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent

# Every HTTP call uses this timeout (seconds).
HTTP_TIMEOUT = 10

# Network-first JSON cache. Overridable for Docker volumes and tests.
CACHE_DIR = Path(os.environ.get("FLOODSENSE_CACHE_DIR", APP_DIR / "data" / "cache"))

# Daily rolling app log: log/flood-appsense.log, rotated at midnight to
# log/flood-appsense-YYYY-MM-DD.log. Overridable for Docker and tests.
LOG_DIR = Path(os.environ.get("FLOODSENSE_LOG_DIR", APP_DIR / "log"))
LOG_FILE_NAME = "flood-appsense.log"
LOG_DAYS_KEPT = 30

DEFAULT_ZIP = "08807"

NWS_BASE = "https://api.weather.gov"
NWPS_BASE = "https://api.water.noaa.gov/nwps/v1"
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
USGS_GAUGE_HEIGHT = "00065"  # parameter code: gauge height, ft

# Middle Brook / Raritan gauges serving Bridgewater (stack.md).
GAUGES: dict[str, str] = {
    "01403200": "Middle Brook at Bound Brook",
    "01403190": "Middle Brook at Route 28, Bound Brook",
    "01403150": "West Branch Middle Brook near Martinsville",
    "01400500": "Raritan River at Manville",
    "01400000": "North Branch Raritan River near Raritan",
    "01403060": "Raritan River at Bound Brook",
}

# --- Per-ZIP places data (story 2) ------------------------------------------

# Local fallback copies of each ZIP's latest places record: <CACHE_DIR>/places/<zip>.json.
# A function, so tests (and Docker) that move CACHE_DIR move this too.
def places_cache_dir() -> Path:
    """Folder holding the local places copies, under the current CACHE_DIR."""
    return Path(CACHE_DIR) / "places"


PLACES_MAX_AGE_DAYS = 7  # a version this old (or older) is rebuilt
PLACES_RETRY_AFTER_MINUTES = 60  # a record with failed or carried sections is retried after this
STORM_REPORT_RADIUS_MI = 6  # flood storm reports (flood spots) within this many miles of the ZIP center
SHELTER_RADIUS_MI = 25  # FEMA shelters within this many miles of the ZIP center
SPOT_GAUGE_LINK_MI = 5  # a flood spot takes the waterway of the nearest gauge within this distance
PLACES_GAUGE_COUNT = 10  # how many of the nearest NWS gauges a places record keeps

# Public sources the places build reads (data/places.py).
IEM_BASE = "https://mesonet.agron.iastate.edu"
OPENFEMA_BASE = "https://www.fema.gov/api/open"
FEMA_SHELTERS_URL = "https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/MapServer/0/query"
HISTORY_START = "2005-01-01"  # history sections count events from this date on

# National help lines (help-directory.md). The same for every ZIP; the ZIP's NWS
# office is added per build. Numbers were checked against redcross.org and
# disasterassistance.gov (FEMA) on the date below.
FIXED_HELP: tuple[dict, ...] = (
    {"name": "Emergency", "number": "911", "kind": "emergency", "verified_on": "2026-10-25"},
    {"name": "211 (shelter, food, utility help)", "number": "211", "kind": "211", "verified_on": "2026-10-25"},
    {"name": "FEMA helpline", "number": "1-800-621-3362", "kind": "fema", "verified_on": "2026-10-25"},
    {"name": "American Red Cross", "number": "1-800-733-2767", "kind": "red_cross", "verified_on": "2026-10-25"},
)


def supabase_settings() -> tuple[str, str] | None:
    """``(SUPABASE_URL, SUPABASE_KEY)`` for the app (anon key), or None if either is missing.

    The trailing slash is removed so ``url + "/rest/v1/..."`` never has a double slash.
    """
    url = get_setting("SUPABASE_URL")
    key = get_setting("SUPABASE_KEY")
    if not url or not key:
        return None
    return url.rstrip("/"), key


# --- Phone alerts (story 6) --------------------------------------------------

NTFY_BASE = "https://ntfy.sh"
DEFAULT_APP_URL = "http://localhost:8501"
COOLDOWN_HOURS = 6  # the same level is not pushed again for a ZIP within this many hours


def ntfy_topic(zip_code: str) -> str | None:
    """The ZIP's ntfy topic, ``floodsense-{zip}-{h}``, or None with no secret configured.

    ``h`` is the first 16 characters of base64url(HMAC-SHA256(NTFY_TOPIC_SECRET, zip)), padding
    removed, so one ZIP's link never gives away the secret or another ZIP's topic. A topic
    without the secret would be guessable, so none is ever made without it.
    """
    secret = (get_setting("NTFY_TOPIC_SECRET") or "").strip()
    if not secret:
        return None
    digest = hmac.new(secret.encode(), str(zip_code).encode(), hashlib.sha256).digest()
    h = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()[:16]
    return f"floodsense-{zip_code}-{h}"


def ntfy_topic_url(zip_code: str) -> str | None:
    """The subscribe link for the ZIP's topic, or None with no secret configured."""
    topic = ntfy_topic(zip_code)
    return f"{NTFY_BASE}/{topic}" if topic else None


def app_url() -> str:
    """The public app link used in pushes (``APP_URL``), without a trailing slash."""
    return (get_setting("APP_URL") or DEFAULT_APP_URL).strip().rstrip("/")


# Sent only when NWS_USER_AGENT is not configured anywhere.
_FALLBACK_USER_AGENT = "FloodSense (contact not configured)"


def _from_st_secrets(name: str) -> str | None:
    """Return a value from st.secrets, or None if there is no secrets file/key."""
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets.toml, or Streamlit unavailable
        pass
    return None


def get_setting(name: str, default: str | None = None) -> str | None:
    value = _from_st_secrets(name)
    if value:
        return value
    return os.environ.get(name) or default


def nws_user_agent() -> str:
    ua = get_setting("NWS_USER_AGENT")
    if not ua:
        log.warning("NWS_USER_AGENT is not set; using a generic User-Agent")
        return _FALLBACK_USER_AGENT
    return ua
