"""Local news: recent flood, storm and weather news for the ZIP's place, plus where to stay informed.

Items come from a Google News RSS search (data/news.py). Only the title, source, link and date
are shown, as escaped plain text; feed HTML and summaries are never rendered.
"""

import streamlit as st

from app_logging import log
from data.location import InvalidZipError, resolve_zip
from news_loader import FEMA_ALERTS_URL, fmt_time, load_news, office_url, safe_url
from ui_shared import ASK_FOR_ZIP, ASK_ICON, entered_zip, md

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"  # same messages as the other pages
NEWS_UNAVAILABLE = "News unavailable right now"
STAY_INFORMED = "Stay informed"


zip_code = entered_zip()

try:
    loc = resolve_zip(zip_code)
except InvalidZipError:
    st.info(ASK_FOR_ZIP, icon=ASK_ICON)
    st.stop()
except Exception:
    st.error(LOOKUP_UNAVAILABLE)
    st.stop()

st.subheader(f"{md(loc.place)} ({loc.zip})")
st.caption(f"Recent flood, storm and weather news for {md(loc.place)}, from Google News.")

with st.spinner("Loading local news..."):
    try:
        result = load_news(loc)
    except Exception:
        log.exception("Local news could not be loaded for %s", loc.zip)
        result = None

if result is None:
    st.info(NEWS_UNAVAILABLE, icon=":material/cloud_off:")
else:
    if result.stale:
        st.caption(f"News as of {fmt_time(result.fetched_at)}")
    if not result.items:
        st.info(f"No recent flood or storm news for {md(loc.place)}", icon=":material/info:")
    for item in result.items:
        with st.container(border=True):
            st.markdown(f"[{md(item.title)}]({safe_url(item.link)})")
            st.caption(" · ".join(p for p in (md(item.source), fmt_time(item.published)) if p))

st.subheader(STAY_INFORMED)
with st.container(horizontal=True):
    nws = office_url(loc)
    if nws:
        st.link_button("NWS forecast office", nws, icon=":material/thunderstorm:")
    st.link_button("FEMA emergency alerts", FEMA_ALERTS_URL, icon=":material/notifications_active:")
