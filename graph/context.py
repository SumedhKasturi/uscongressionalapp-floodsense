"""Context nodes for the knowledge graph, built from the places record alone (no I/O).

    road:<slug>          a road named in a flood spot's remark   <-- on --            spot
    event:<YYYY-MM-DD>   a storm: 2+ reports on consecutive days <-- reported during -- spot
    disaster:<number>    a FEMA declaration for the county       <-- declared as --   event
    gauge:<lid>          the spot's nearest gauge within 5 mi    <-- monitored by --  spot

They add context for residents and judges (which roads flood, which storms did it, which
of them became federal disasters). The risk rules ignore them.
"""

from __future__ import annotations

import functools
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from data import places as places_mod
from data.places import slug
from data.sources import parse_time

LOCAL_TZ = ZoneInfo("America/New_York")
MIN_EVENT_REPORTS = 2  # a storm event needs at least this many reports
# FEMA records here have only a declaration date. A storm counts toward a declaration
# when it starts up to this many days before it, or the day after.
DECLARATION_LOOKBACK_DAYS = 7
FLOOD_INCIDENT_TYPES = {"flood", "hurricane", "severe storm", "coastal storm", "tropical storm"}
NOT_FLOOD_WORDS = ("SNOW", "WINTER", "BLIZZARD", "EVACUATION", "COVID")

# Route numbers that are US highways in New Jersey; a bare "Route N" with another number is NJ N.
US_ROUTES = {1, 9, 22, 30, 40, 46, 130, 202, 206, 322}

_INTERSTATE = r"(?P<i>\b(?:I|INTERSTATE)[\s-]*(?P<i_n>\d{1,3})\b)"
_STATE = r"(?P<nj>\b(?:{codes}STATE\s+(?:HIGHWAY|HWY|ROUTE|RTE|RT)|SR)[\s-]*(?:ROUTE|RTE|RT|HWY)?[\s-]*(?P<nj_n>\d{{1,3}})\b)"
_COUNTY = r"(?P<cr>\b(?:CR|C\.R\.|COUNTY\s+(?:ROUTE|ROAD|RD|RT))[\s-]*(?P<cr_n>\d{1,4})\b)"
_US = r"(?P<us>(?<![A-Z])(?:U\.S\.|US)[\s-]*(?:ROUTE|RTE|RT|HIGHWAY|HWY)?[\s-]*(?P<us_n>\d{1,3})\b)"
_GENERIC = r"(?P<gen>\b(?:ROUTE|RTE|RT|HIGHWAY|HWY)[\s-]*(?P<gen_n>\d{1,3})\b)"
# Postal codes that are also English words ("water IN 5 homes"); for these only "State Route N" counts.
_WORD_CODES = {"IN", "OR", "ME", "OK", "HI", "OH", "AS"}


@functools.lru_cache(maxsize=64)
def _numbered(state: str) -> re.Pattern:
    """The numbered-route pattern for one state: its postal code ("NM 371", "N.M. 371") is a state route."""
    codes = ""
    if state not in _WORD_CODES:
        dotted = r"\.".join(state) + r"\."
        codes = f"(?-i:{state})|{dotted}|"  # the bare code only in capitals
    return re.compile("|".join((_INTERSTATE, _STATE.format(codes=codes), _COUNTY, _US, _GENERIC)), re.IGNORECASE)


def state_of(places: dict) -> str:
    """The ZIP's state postal code ("NM"), from its county UGC or "Place, ST"; NJ when neither is known."""
    area = places.get("area") or {}
    ugc = area.get("county_ugc") or ""
    if re.fullmatch(r"[A-Z]{2}C\d{3}", ugc):
        return ugc[:2]
    m = re.search(r",\s*([A-Z]{2})$", area.get("place") or "")
    return m.group(1) if m else "NJ"


def us_routes_in(remarks) -> set[int]:
    """Route numbers that some remark names explicitly as a US highway ("U.S. Highway 64")."""
    return {int(m.group("us_n")) for r in remarks for m in _numbered("NJ").finditer(r or "") if m.group("us")}

_SUFFIXES = {"road": "Rd", "rd": "Rd", "avenue": "Ave", "ave": "Ave", "street": "St", "st": "St",
             "drive": "Dr", "dr": "Dr", "lane": "Ln", "ln": "Ln"}
_NAMED = re.compile(r"\b(road|rd|avenue|ave|street|st|drive|dr|lane|ln)\b\.?", re.IGNORECASE)
# "St" followed by a name and then a road word is "Saint" inside a name: "Old St Augustine Rd".
_SAINT_AHEAD = re.compile(r"\.?\s+([A-Za-z']+)(?:\s+[A-Za-z']+)?\s+(?:road|rd|avenue|ave|street|drive|dr|lane|ln)\b",
                          re.IGNORECASE)
# Words that can't be part of a road's name: verbs, prepositions, lane words and the like.
_STOP = {
    "a", "about", "across", "all", "along", "an", "and", "approx", "area", "at", "basement", "between", "both",
    "bridge", "by", "car", "cars", "center", "closed", "closing", "closure", "deep", "due", "eastbound",
    "entire", "exit", "feet", "few", "flood", "flooded", "flooding", "floods", "for", "from", "has", "high", "in",
    "inches", "intersection", "is", "lane", "lanes", "left", "local", "middle", "multiple", "near",
    "northbound", "of", "off", "on", "one", "onto", "or", "over", "passable", "ramp", "reported", "reports",
    "right", "road", "roads", "shoulder", "several", "southbound", "the", "three", "through", "to",
    "township", "travel", "two", "under", "up", "was", "water", "were", "westbound", "with", "impassable",
    "submerged", "stranded", "vehicle", "vehicles", "street", "streets", "report", "from", "via", "including",
    "nb", "sb", "eb", "wb", "twp", "times", "time", "rescue", "rescues", "residents", "minor", "major",
    "heavy", "rain", "caused", "causing", "secondary", "numerous", "many", "some", "covered",
}
# "Right lane" and similar are lane positions, not roads named "... Lane".
_LANE_WORDS = {"right", "left", "all", "one", "two", "three", "both", "travel", "center", "middle", "shoulder",
               "turning", "turn", "inside", "outside", "passing", "single"}


def _road_name_before(text: str, end: int) -> list[str]:
    """Up to three name words just before ``end`` (walking back until a stop word)."""
    words = []
    for token in reversed(re.findall(r"[A-Za-z'\.]+|[^A-Za-z'\.\s]+", text[:end])):
        word = token.strip(".")
        # A sentence ended ("JUNCTION. LANE RESTRICTIONS"), unless it's "St." inside a name ("Old St. Augustine Rd").
        if token.endswith(".") and len(word) > 1 and word.lower() != "st":
            break
        if not word or not re.fullmatch(r"[A-Za-z']+", word) or word.lower() in _STOP or len(words) == 3:
            break
        words.append(word)
    return list(reversed(words))


def roads_in(remark: str | None, state: str = "NJ", us_seen: set[int] = frozenset()) -> list[str]:
    """Road names in a storm-report remark, normalized: "US 22", "NJ 28", "I-287", "CR 622", "Old York Rd".

    ``state`` names state routes ("NM 371"). A bare "Route/Highway N" is US N when ``us_seen`` (US numbers
    named elsewhere for this ZIP) has it; in NJ the known US highways decide; elsewhere it stays "Hwy N".
    """
    text = remark or ""
    found: list[str] = []
    taken: list[tuple[int, int]] = []
    for m in _numbered(state).finditer(text):
        n = int(m.group("i_n") or m.group("nj_n") or m.group("cr_n") or m.group("us_n") or m.group("gen_n"))
        if m.group("i"):
            name = f"I-{n}"
        elif m.group("nj"):
            name = f"{state} {n}"
        elif m.group("cr"):
            name = f"CR {n}"
        elif m.group("us"):
            name = f"US {n}"
        else:
            if n in us_seen or (state == "NJ" and n in US_ROUTES):
                name = f"US {n}"
            else:
                name = f"NJ {n}" if state == "NJ" else f"Hwy {n}"
        taken.append(m.span())
        found.append(name)
    for m in _NAMED.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        words = _road_name_before(text, m.start())
        suffix = m.group(1).lower()
        ahead = _SAINT_AHEAD.match(text, m.end() - (1 if m.group(0).endswith(".") else 0))
        if suffix == "st" and ahead and ahead.group(1).lower() not in _STOP:
            continue
        if not words or (suffix in ("lane", "ln") and words[-1].lower() in _LANE_WORDS):
            continue
        # "St" right after a number ("1ST") or a lone initial isn't a street.
        if all(len(w) == 1 for w in words):
            continue
        # "Saint" and "St" name the same road.
        found.append(" ".join("St" if w.lower() in ("saint", "st") else w.capitalize() for w in words)
                     + " " + _SUFFIXES[suffix])
    out: list[str] = []
    for name in found:
        if name not in out:
            out.append(name)
    return out


def road_id(name: str) -> str:
    return f"road:{slug(name)}"


# --- storm events ----------------------------------------------------------------------

def _local_day(when: str | None) -> date | None:
    t = parse_time(when)
    return t.astimezone(LOCAL_TZ).date() if t else None


def _day_span(start: date, end: date) -> str:
    if start == end:
        return f"{start:%b} {start.day}, {start.year}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start:%b} {start.day}–{end.day}, {start.year}"
    if start.year == end.year:
        return f"{start:%b} {start.day}–{end:%b} {end.day}, {start.year}"
    return f"{start:%b} {start.day}, {start.year}–{end:%b} {end.day}, {end.year}"


def storm_events(spots: list[dict]) -> list[dict]:
    """Group flood spots by local report day, merging consecutive days; keep groups of 2+ reports.

    Returns ``[{"id", "start", "end", "label", "spots": [spot ids]}]``, oldest first.
    """
    by_day: dict[date, list[str]] = {}
    for s in spots:
        day = _local_day(s.get("date"))
        if day is not None and s.get("id"):
            by_day.setdefault(day, []).append(s["id"])
    groups: list[list[date]] = []
    for day in sorted(by_day):
        if groups and day - groups[-1][-1] == timedelta(days=1):
            groups[-1].append(day)
        else:
            groups.append([day])
    events = []
    for days in groups:
        ids = [i for d in days for i in by_day[d]]
        if len(ids) < MIN_EVENT_REPORTS:
            continue
        start, end = days[0], days[-1]
        events.append({
            "id": f"event:{start.isoformat()}", "start": start, "end": end, "spots": ids,
            "label": f"Storm of {_day_span(start, end)} · {len(ids)} reports",
        })
    return events


# --- FEMA disasters ----------------------------------------------------------------------

_SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"}


def _title(text: str) -> str:
    words = (text or "").lower().split()
    return " ".join(w if (i and w in _SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words))


def _declaration_code(item: dict) -> str:
    """"DR-4614-NJ" -> "DR-4614"."""
    code = str(item.get("id") or "")
    m = re.match(r"([A-Z]{2})-(\d+)", code)
    return f"{m.group(1)}-{m.group(2)}" if m else f"#{item.get('number')}"


def _period(item: dict) -> tuple[date, date] | None:
    """Incident period: the record's own begin/end if present, else the lookback before declaration."""
    begin = parse_time(item.get("incident_begin") or item.get("incidentBeginDate"))
    end = parse_time(item.get("incident_end") or item.get("incidentEndDate"))
    if begin:
        return begin.date(), (end or begin).date()
    try:
        declared = date.fromisoformat(str(item.get("declared") or "")[:10])
    except ValueError:
        return None
    return declared - timedelta(days=DECLARATION_LOOKBACK_DAYS), declared


def flood_disasters(items: list[dict], events: list[dict]) -> list[dict]:
    """FEMA declarations linked to storm events, or flood-related; DR preferred over a matching EM.

    Returns ``[{"id", "label", "code", "incident_type", "events": [event ids]}]``.
    """
    # Dedupe: a DR and an EM declared within the lookback with the same title (or the same
    # incident type, e.g. "Hurricane Floyd" DR and EM titles differ) are one disaster.
    kept: list[dict] = []
    for item in sorted(items or [], key=lambda d: not _declaration_code(d).startswith("DR")):
        p = _period(item)
        if p is None or not item.get("number"):
            continue
        is_em = _declaration_code(item).startswith("EM")
        dup = next((k for k in kept if abs((k["period"][1] - p[1]).days) <= DECLARATION_LOOKBACK_DAYS and (
            (k["title"] or "").upper() == (item.get("title") or "").upper()
            or (is_em and k["item"].get("incident_type") == item.get("incident_type")))), None)
        if dup is None:
            kept.append({"item": item, "title": item.get("title"), "period": p})
    out = []
    for k in kept:
        item, (begin, end) = k["item"], k["period"]
        linked = [e["id"] for e in events
                  if e["start"] <= end + timedelta(days=1) and e["end"] >= begin - timedelta(days=1)]
        title = (item.get("title") or "").upper()
        flood_type = (str(item.get("incident_type") or "").lower() in FLOOD_INCIDENT_TYPES
                      and not any(w in title for w in NOT_FLOOD_WORDS))
        if not linked and not flood_type:
            continue
        code = _declaration_code(item)
        out.append({"id": f"disaster:{item['number']}", "code": code, "incident_type": item.get("incident_type"),
                    "declared": item.get("declared"), "label": f"{_title(item.get('title') or 'Disaster')} ({code})",
                    "events": linked})
    return out


# --- adding it all to the graph ---------------------------------------------------------------

def add_context(G, places: dict, spot_node, gauge_id) -> None:
    """Add road, storm-event, disaster and spot -> gauge context to ``G`` (spots and gauges already in it)."""
    spots = [s for s in places.get("flood_spots") or [] if s.get("id") and spot_node(s["id"]) in G]
    state = state_of(places)
    us_seen = us_routes_in(s.get("remark") for s in spots)
    for s in spots:
        sid = spot_node(s["id"])
        for name in roads_in(s.get("remark"), state, us_seen):
            rid = road_id(name)
            if rid not in G:
                G.add_node(rid, type="road", label=name, name=name)
            G.add_edge(sid, rid, rel="on")

    events = storm_events(spots)
    for e in events:
        G.add_node(e["id"], type="storm event", label=e["label"], start=e["start"].isoformat(),
                   end=e["end"].isoformat(), reports=len(e["spots"]))
        for spot_id in e["spots"]:
            G.add_edge(spot_node(spot_id), e["id"], rel="reported during")

    items = (((places.get("history") or {}).get("disasters") or {}).get("items")) or []
    for d in flood_disasters(items, events):
        G.add_node(d["id"], type="disaster", label=d["label"], code=d["code"], incident_type=d["incident_type"],
                   declared=d["declared"])
        for event_id in d["events"]:
            G.add_edge(event_id, d["id"], rel="declared as")

    gauges = [g for g in places.get("gauges") or []
              if g.get("lid") and g.get("lat") is not None and g.get("lon") is not None and gauge_id(g["lid"]) in G]
    for s in spots:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        g = places_mod.nearest_gauge(s["lat"], s["lon"], gauges, config.SPOT_GAUGE_LINK_MI)
        if g is not None:
            G.add_edge(spot_node(s["id"]), gauge_id(g["lid"]), rel="monitored by")
