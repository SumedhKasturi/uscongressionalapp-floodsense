"""Local news page helpers: cached news per ZIP, link and time formatting, the NWS office link.

News is cached per ZIP so page reruns don't fetch Google News again.

Only a fresh feed is kept in ``st.cache_data`` (15 minutes). A failed or cache-fallback fetch is
returned but not cached, so the next rerun tries the network again. ``data/news.py`` stays free
of Streamlit.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import streamlit as st

from app_logging import log
from assessment_loader import load_places
from data import news

NEWS_TTL_SECONDS = 15 * 60
NEWS_MAX_ENTRIES = 100
FEMA_ALERTS_URL = "https://www.ready.gov/alerts"
NWS_OFFICE_URL = "https://www.weather.gov/{wfo}"
EASTERN = ZoneInfo("America/New_York")
URL_SAFE = ":/?&=#%~.-_+,;@!$'*"  # everything else (spaces, parens, <, >, quotes) is percent-encoded


def fmt_time(t) -> str:
    """Eastern time with the year and an "ET" suffix (a datetime or an ISO string).

    News spans 12 months, so the year is shown: "Nov 03, 2025, 04:10 PM ET".
    """
    try:
        if not isinstance(t, datetime):
            t = datetime.fromisoformat(str(t))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(EASTERN).strftime("%b %d, %Y, %I:%M %p ET")
    except (TypeError, ValueError, OverflowError):
        return str(t or "")


def safe_url(url: str) -> str:
    """Percent-encode everything outside an allow-list, so a link can't break out of Markdown's ``(...)``."""
    return quote(str(url or ""), safe=URL_SAFE)


def office_url(loc) -> str | None:
    """The ZIP's NWS forecast office page, from the cached places record (None if unknown)."""
    try:
        wfo = ((load_places(loc) or {}).get("area") or {}).get("wfo")
    except Exception:
        log.exception("Places record unavailable for the news page, %s", loc.zip)
        return None
    wfo = str(wfo or "").strip()
    return NWS_OFFICE_URL.format(wfo=wfo.lower()) if re.fullmatch(r"[A-Za-z]{3}", wfo) else None


class _NotCached(Exception):
    """Carries a result that must not be cached (unavailable or stale)."""

    def __init__(self, result):
        super().__init__("not cached")
        self.result = result


@st.cache_data(ttl=NEWS_TTL_SECONDS, max_entries=NEWS_MAX_ENTRIES, show_spinner=False)
def _cached_news(loc):
    result = news.fetch_news(loc)
    if result is None or result.stale:
        raise _NotCached(result)  # st.cache_data never stores a call that raised
    return result


def load_news(loc) -> news.NewsResult | None:
    """``news.fetch_news(loc)``, cached per ZIP for 15 minutes when the feed was fresh."""
    try:
        return _cached_news(loc)
    except _NotCached as exc:
        return exc.result
