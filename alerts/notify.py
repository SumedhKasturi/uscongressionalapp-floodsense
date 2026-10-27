"""Build and send one ntfy push for a ZIP whose flood risk rose.

The message reuses the assessment the rules produced: the level, the top reason's
graph path, the top precaution and the app link. ``send`` is one HTTP POST to
``https://ntfy.sh/<topic>`` with the ntfy headers; it always has a timeout.
"""

from __future__ import annotations

import base64

import networkx as nx
import requests

import config
from graph import explain, reason
from graph import thresholds

# ntfy priorities: "urgent" (5) for high, "high" (4) for elevated (and anything lower, e.g. a forced test).
PRIORITIES = {"high": "urgent", "elevated": "high", "low": "high"}
TAGS = "warning,ocean"


class NotifyError(RuntimeError):
    """ntfy could not be reached, timed out, or did not accept the push."""


def _top_precaution(assessment: reason.Assessment, places: dict, level: str) -> str | None:
    """The first precaution for ``level``: the assessment's own when it is at that level, else the places text."""
    if assessment.area_level == level and assessment.precautions:
        return assessment.precautions[0]["text"]
    texts = ((places.get("precautions") or {}).get("flood") or {}).get(level) or []
    if texts:
        return texts[0]
    return assessment.precautions[0]["text"] if assessment.precautions else None


def build_message(zip_code: str, place: str, assessment: reason.Assessment, places: dict,
                  G: nx.DiGraph | None = None, level: str | None = None) -> dict:
    """The push for ``zip_code``: ``{"title", "body", "priority", "tags", "click"}``.

    ``G`` (the graph the assessment came from) turns the top reason into a readable chain;
    without it the reason's label is used. ``level`` overrides the assessment's level
    (the forced test push).
    """
    level = level or assessment.area_level
    if level not in thresholds.LEVELS:
        raise ValueError(f"Unknown risk level {level!r}")
    app = config.app_url()
    where = place or zip_code
    lines = []
    if assessment.reasons:
        top = assessment.reasons[0]
        lines.append(explain.chain(G, top) if G is not None else top.label)
    else:
        lines.append(f"Flood risk for {where} is {level}.")
    precaution = _top_precaution(assessment, places, level)
    if precaution:
        lines.append(precaution)
    lines.append(f"Open FloodSense: {app}")
    return {
        "title": f"FloodSense: {level.capitalize()} flood risk in {where}",
        "body": " · ".join(lines),
        "priority": PRIORITIES[level],
        "tags": TAGS,
        "click": f"{app}/alerts?zip={zip_code}",
    }


def _header_text(text: str) -> str:
    """HTTP headers are Latin-1; ntfy accepts RFC 2047 encoded words for anything else."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send(topic: str, message: dict) -> None:
    """POST ``message`` to the ntfy ``topic``. Raises NotifyError on a timeout, network error or non-2xx."""
    if not topic:
        raise NotifyError("No ntfy topic")
    headers = {
        "Title": _header_text(message["title"]),
        "Priority": message["priority"],
        "Tags": message["tags"],
        "Click": message["click"],
    }
    try:
        resp = requests.post(f"{config.NTFY_BASE}/{topic}", data=message["body"].encode("utf-8"),
                             headers=headers, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise NotifyError(f"ntfy request failed: {exc.__class__.__name__}") from exc
    if resp.status_code >= 300:
        raise NotifyError(f"ntfy returned HTTP {resp.status_code}")
