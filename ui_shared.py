"""Shared UI pieces: the one sidebar ZIP box every page reads, and the area risk badge.

The app shell (app.py) draws the sidebar ZIP box (widget key ``zip``) and keeps
``st.session_state["current_zip"]`` as the single source of truth: only a ZIP that resolves is
saved there. Pages call ``entered_zip()`` and resolve it themselves, so an invalid entry stops
each page with a short ``ASK_FOR_ZIP`` note while the sidebar shows the invalid-ZIP message.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

import assessment_loader
import config
from app_logging import log
# Pure helpers shared with the graph query agent; pages keep importing them from here.
from graph.explain import FLOOD_BADGES, NO_FLOOD_STATUS, is_open, low_unconfirmed  # noqa: F401

ZIP_KEY = "zip"
ASK_FOR_ZIP = "Enter a valid 5-digit ZIP code in the sidebar to see this page."
ASK_ICON = ":material/edit_location_alt:"
RISK_UNAVAILABLE = "Risk unavailable"

# One title, color and icon per risk level, shared by banners, badges and metrics on every page.
RISK_STYLES = {
    "low": ("Low risk", "green", ":material/check_circle:"),
    "elevated": ("Elevated risk", "orange", ":material/warning:"),
    "high": ("High risk", "red", ":material/emergency_home:"),
}

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|$~])")


def md(text) -> str:
    """Escape Markdown so place and gauge names render as plain words."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or ""))


def entered_zip() -> str:
    """What the resident typed in the sidebar ZIP box (the current ZIP when the box isn't drawn)."""
    st.session_state.setdefault("current_zip", config.DEFAULT_ZIP)
    return str(st.session_state.get(ZIP_KEY, st.session_state["current_zip"]))


_LOAD = object()  # "load it here": the default for helpers that can take the shell's loaded assessment


def load_area(loc):
    """``(inputs, assessment)`` for ``loc`` from the cached assessment, or None. Never raises.

    None when anything fails, or when no places source answered (the assessment would only
    retry every source). The shell calls this once per rerun and passes the result to
    ``area_risk`` and ``breaking_alert``.
    """
    try:
        record = assessment_loader.load_places(loc)
        if record.get("all_sources_failed"):
            return None
        _, inputs, _, assessment = assessment_loader.load_assessment(loc)
    except Exception:
        log.exception("Area risk could not be assessed for %s", getattr(loc, "zip", loc))
        return None
    return inputs, assessment


def area_risk(loc, loaded=_LOAD) -> str | None:
    """The area's risk level ("low", "elevated" or "high") from the cached assessment, or None.

    None when anything fails, when no places source answered (the assessment would only retry
    every source), or when NWS alerts are unknown and the rules say low: the app never shows a
    green all-clear while alerts could not be checked. ``loaded`` is ``load_area(loc)``'s result,
    when the caller already has it.
    """
    if loaded is _LOAD:
        loaded = load_area(loc)
    if loaded is None:
        return None
    inputs, assessment = loaded
    level = assessment.area_level
    if level not in RISK_STYLES or low_unconfirmed(inputs, level):
        return None
    return level


def risk_badge(level: str | None) -> None:
    """A small colored badge: "Low risk" (green), "Elevated risk" (orange), "High risk" (red)."""
    if level in RISK_STYLES:
        label, color, icon = RISK_STYLES[level]
        st.badge(label, color=color, icon=icon)
    else:
        st.badge(RISK_UNAVAILABLE, color="gray", icon=":material/help:")


# --- Breaking-alert banner (story 8) -------------------------------------------------------

BREAKING_ICON = ":material/campaign:"
ALERTS_PAGE = "app_pages/alerts.py"
ALERTS_URL_PATH = "alerts"
SEE_ALERTS = "See flood alerts"
ALERT_DATA_AS_OF = "Live data as of"  # inputs.stale covers every source, not only alerts
ALERT_DATA_STALE = "Live data may be out of date"
EASTERN = ZoneInfo("America/New_York")
_SEVERITY_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1}  # anything else is Unknown (0)
_RED_SEVERITIES = ("extreme", "severe")


def _severity_rank(alert: dict) -> int:
    return _SEVERITY_RANK.get(str(alert.get("severity") or "").strip().lower(), 0)


def _level_rank(alert: dict) -> int:
    from graph import reason, thresholds

    level = reason.alert_level(alert.get("event") or "")
    return thresholds.LEVELS.index(level) if level in thresholds.LEVELS else -1


def pick_alert(alerts: list[dict]) -> dict | None:
    """The banner's alert: highest rule level, then highest NWS severity; the first among equals."""
    best, best_key = None, None
    for a in alerts or []:
        key = (_level_rank(a), _severity_rank(a))
        if best_key is None or key > best_key:
            best, best_key = a, key
    return best


def is_red(alert: dict) -> bool:
    """Red for a high-level alert or an Extreme/Severe one; orange otherwise."""
    from graph import reason

    return (reason.alert_level(alert.get("event") or "") == "high"
            or str(alert.get("severity") or "").strip().lower() in _RED_SEVERITIES)


def fmt_time(iso) -> str:
    """Eastern time with an "ET" suffix, like the pages."""
    try:
        t = datetime.fromisoformat(str(iso))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(EASTERN).strftime("%b %d, %I:%M %p ET")
    except (TypeError, ValueError, OverflowError):
        return str(iso or "")


def breaking_alert(loc, loaded=_LOAD, current_page: str | None = None) -> dict | None:
    """Draw the slim breaking-alert banner for ``loc`` and return its alert, or None (no banner).

    ``loaded`` is the shell's ``load_area(loc)`` result, so the banner adds no fetch or second
    assessment. ``current_page`` is the open page's url_path: on the Flood alerts page itself the
    "See flood alerts" link is left out. Never raises: with no alert, no places source, or a
    failed assessment it draws nothing.
    """
    try:
        if loaded is _LOAD:
            loaded = load_area(loc)
        if loaded is None:
            return None
        inputs, _ = loaded
        alert = pick_alert(inputs.alerts)
        if alert is None:
            return None
        event = str(alert.get("event") or "").strip() or "NWS alert"
        headline = str(alert.get("headline") or "").strip()
        text = f"**{md(event)}**: {md(headline)}" if headline else f"**{md(event)}**"
        (st.error if is_red(alert) else st.warning)(text, icon=BREAKING_ICON)
        if inputs.stale:
            st.caption(f"{ALERT_DATA_AS_OF} {fmt_time(inputs.as_of)}" if inputs.as_of else ALERT_DATA_STALE)
        if current_page != ALERTS_URL_PATH:
            st.page_link(ALERTS_PAGE, label=SEE_ALERTS, icon=":material/arrow_forward:")
        return alert
    except Exception:
        log.exception("Breaking-alert banner could not be shown for %s", getattr(loc, "zip", loc))
        return None
