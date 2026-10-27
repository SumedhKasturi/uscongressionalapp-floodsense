"""Ask the graph: a rule-based agent that answers a resident's question from the knowledge graph.

``answer(question, G, assessment, places, inputs)`` matches the normalized question (NFKC,
casefolded, punctuation as spaces) to one intent (story 13), checked in this order:

    entity    the question names a graph node (waterway, road, gauge, storm event, place);
              the longest label wins
    roads     raised flood spots and their roads, or the roads with the most past reports
    why       the assessment's reasons as graph chains, highest level first
    risk      the area level and the top reason
    gauges    each gauge's level and flood status (as on the forecast page)
    alerts    the active NWS alerts
    help      help lines and shelters in rule 5's order
    history   storm events, newest first
    fallback  what the agent can answer, with suggestions

Answers other than risk and why open with the area's risk when it is raised or unknown, and any
question with emergency wording opens with "In an emergency, call 911." whatever its intent.

Every answer lists the graph edges it used as (source, relationship, target) facts. The agent
only reads the graph, the assessment and the live inputs: no Streamlit, no network, no AI model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import networkx as nx

from data.places import slug
from graph import build, explain, reason

MAX_QUESTION = 200  # characters; longer questions are cut
TOP_ROADS = 5  # calm roads answer: this many roads with the most past flood reports
NEWEST_EVENTS = 3  # history answer: this many of the newest storm events
MAX_HELP = 6  # help answer: the first entries of rule 5's order
MAX_REASONS = 8  # why answer: chains shown before "...and N more"
MAX_NAMES = 5  # entity answer: names shown per relationship group
RAISED = ("elevated", "high")
ENTITY_TYPES = ("waterway", "road", "gauge", "storm event", "area")

STALE_NOTE = "Some live data could not be refreshed; cached readings were used."  # the page's caption
RISK_UNKNOWN = "Risk unknown: NWS alerts could not be checked."
EMERGENCY_LINE = "In an emergency, call 911."
ALERTS_UNCHECKED = "NWS alerts could not be checked right now."
FALLBACK = "I can answer questions about risk, roads, gauges, alerts, shelters and past floods."
SUGGESTIONS = (
    "Am I safe?", "Which roads should I avoid?", "Why is the risk at this level?", "What are the river levels?",
    "Are there any flood alerts?", "Where can I go for help?", "Has it flooded here before?",
)

# Keywords and synonyms per intent, in matching order. Each matches whole words only, so
# inflections are listed ("roads", "warnings"); "pasta" isn't "past" and "causeway" isn't "cause".
INTENTS = (
    ("roads", ("road", "roads", "street", "streets", "avoid", "drive", "driving", "route", "routes", "highway",
               "highways")),
    ("why", ("why", "reason", "reasons", "cause", "caused", "because", "explain")),
    ("risk", ("risk", "safe", "unsafe", "danger", "dangerous", "flood today", "should i worry", "worried", "worry",
              "coming", "tonight", "tomorrow", "forecast")),
    ("gauges", ("gauge", "gauges", "river", "rivers", "stream", "streams", "water level", "water levels", "creek",
                "creeks")),
    ("alerts", ("alert", "alerts", "warning", "warnings", "watch", "watches", "advisory", "advisories")),
    ("help", ("shelter", "shelters", "help", "evacuate", "evacuation", "911", "emergency", "where can i go",
              "where should i go", "where do i go", "call", "called")),
    ("history", ("history", "past", "before", "ida", "storm", "storms", "when did", "ever", "ever flood",
                 "last time")),
)
# Bare flood and rain words also mean "risk", but only as a tie-break after every intent above:
# "Will it flood?" is a risk question, while "Has it flooded here before?" stays history and
# "Is there a flood warning on the river?" stays gauges.
WEAK_RISK = ("flood", "floods", "flooding", "flooded", "rain", "rains", "raining", "rainy")
YEAR = re.compile(r"(?<!\w)(?:19|20)\d{2}(?!\w)")  # a 4-digit year is a history marker
# Emergency wording: the answer opens with EMERGENCY_LINE whatever the intent.
EMERGENCY = ("911", "emergency", "trapped", "rescue", "help me", "help my", "flooding now", "water in my",
             "water on my", "who do i call")


@dataclass
class Answer:
    intent: str
    text: str  # Markdown-free plain text; "- " lines are list items
    facts: list[tuple[str, str, str]] = field(default_factory=list)  # (source, relationship, target)
    nodes: list[str] = field(default_factory=list)  # node ids the answer is about


class _Facts:
    """Graph edges an answer used, each once, in the order they were used."""

    def __init__(self, G: nx.DiGraph):
        self.G, self.edges, self.nodes = G, [], []

    def edge(self, u: str, v: str) -> None:
        if self.G.has_edge(u, v) and (u, v) not in self.edges:
            self.edges.append((u, v))

    def path(self, path) -> None:
        """Every edge along a reason's path. A pair that isn't an edge (rule 1's alert -> spot) uses the
        trigger's own edge to the path's end instead (alert -> covers -> area)."""
        for u, v in zip(path, path[1:]):
            self.edge(u, v if self.G.has_edge(u, v) else path[-1])

    def node(self, n: str) -> None:
        if n in self.G and n not in self.nodes:
            self.nodes.append(n)

    def rows(self) -> list[tuple[str, str, str]]:
        return [(explain.node_name(self.G, u), self.G.edges[u, v].get("rel", ""), explain.node_name(self.G, v))
                for u, v in self.edges]


def _text(*blocks) -> str:
    """Paragraphs (str) and lists (list of str, drawn as "- " items), blank-line separated."""
    out = []
    for b in blocks:
        if isinstance(b, (list, tuple)):
            if b:
                out.append("\n".join(f"- {item}" for item in b))
        elif b:
            out.append(b)
    return "\n\n".join(out)


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _nodes(G: nx.DiGraph, kind: str) -> list[str]:
    return [n for n, d in G.nodes(data=True) if d.get("type") == kind]


def _label(G: nx.DiGraph, n: str) -> str:
    return G.nodes[n].get("label") or n


def _out(G: nx.DiGraph, n: str, rel: str) -> list[str]:
    return [v for v in G.successors(n) if G.edges[n, v].get("rel") == rel]


def _in(G: nx.DiGraph, n: str, rel: str) -> list[str]:
    return [u for u in G.predecessors(n) if G.edges[u, n].get("rel") == rel]


def _area(G: nx.DiGraph) -> str | None:
    areas = _nodes(G, "area")
    return areas[0] if areas else None


def normalize(text) -> str:
    """NFKC, casefolded, punctuation as spaces, single-spaced: how questions and labels are compared."""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _has(q: str, keyword: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", q) is not None


def _names(G: nx.DiGraph, nodes: list[str]) -> str:
    shown = "; ".join(explain.node_name(G, n) for n in nodes[:MAX_NAMES])
    return shown + (f"; …and {len(nodes) - MAX_NAMES} more" if len(nodes) > MAX_NAMES else "")


def _area_level(assessment, inputs) -> str:
    return "unknown" if explain.low_unconfirmed(inputs, assessment.area_level) else assessment.area_level


def _level(level: str | None, inputs) -> str:
    """A waterway's or spot's level as shown: "unknown" when missing, or low while alerts are unknown."""
    return "unknown" if level is None or explain.low_unconfirmed(inputs, level) else level


def risk_header(assessment, inputs) -> str:
    """The one-line area risk that opens other answers when it is raised or unknown, else ""."""
    level = _area_level(assessment, inputs)
    if level == "unknown":
        return RISK_UNKNOWN
    if level not in RAISED:
        return ""
    # Only the chain's first step (the alert or reading that set it off); the why answer has the rest.
    top = assessment.reasons[0].label.split(" → ")[0] if assessment.reasons else ""
    return f"Area risk: {level} — {top}." if top else f"Area risk: {level}."


def _checked(G: nx.DiGraph, f: _Facts) -> None:
    """With no rule triggered, the live inputs the rules checked: alerts, rain and gauges."""
    for kind in ("alert", "rain", "gauge"):
        for n in _nodes(G, kind):
            for v in G.successors(n):
                if G.edges[n, v].get("rel") in ("covers", "falls on", "measures"):
                    f.edge(n, v)


def _live_notes(inputs, alerts: bool = True) -> list[str]:
    """The page's honesty captions for answers built on live data."""
    notes = []
    if alerts and getattr(inputs, "alerts_unavailable", False):
        notes.append(ALERTS_UNCHECKED)
    if getattr(inputs, "stale", False):
        notes.append(STALE_NOTE)
    return notes


# --- matching -------------------------------------------------------------------------------

def find_entity(q: str, G: nx.DiGraph) -> str | None:
    """The graph node whose label (or the area's place name) the question names; the longest wins."""
    best, best_len = None, 0
    for n, d in G.nodes(data=True):
        if d.get("type") not in ENTITY_TYPES:
            continue
        for name in {normalize(x) for x in (d.get("label"), d.get("place"), d.get("name")) if x} - {""}:
            if len(name) > best_len and _has(q, name):
                best, best_len = n, len(name)
    return best


def match(question: str, G: nx.DiGraph) -> tuple[str, str | None]:
    """(intent, entity node or None) for a question.

    Entity first, then INTENTS in order. Tie-break: a bare flood or rain word (WEAK_RISK) means risk
    only when no intent matched, and a history marker (INTENTS' history words or a 4-digit year)
    always beats it, so "Did it flood in 2021?" is history and "Will it flood?" is risk.
    """
    q = normalize(str(question or "")[:MAX_QUESTION])
    if not q:
        return "fallback", None
    entity = find_entity(q, G)
    if entity is not None:
        return "entity", entity
    for intent, keywords in INTENTS:
        if any(_has(q, k) for k in keywords):
            return intent, None
    if YEAR.search(q):
        return "history", None
    if any(_has(q, k) for k in WEAK_RISK):
        return "risk", None
    return "fallback", None


def emergency(question) -> bool:
    """True when the question has emergency wording (whole words)."""
    q = normalize(str(question or "")[:MAX_QUESTION])
    return any(_has(q, k) for k in EMERGENCY)


def suggestions(G: nx.DiGraph) -> list[str]:
    """Suggested questions: one per intent, plus the waterway with the most flood spots."""
    waters = sorted(_nodes(G, "waterway"), key=lambda w: (-len(_out(G, w, "floods")), _label(G, w)))
    return list(SUGGESTIONS) + ([f"What about {_label(G, waters[0])}?"] if waters else [])


# --- answers --------------------------------------------------------------------------------

def _roads(G, assessment, places, inputs, f: _Facts) -> str:
    level = _area_level(assessment, inputs)
    raised = [s for s, lv in assessment.spot_levels.items() if lv in RAISED and s in G]
    avoid = [p for p in assessment.precautions
             if isinstance(p, dict) and p.get("kind") == "avoid" and p.get("spot") and p.get("text")]
    if level not in RAISED:
        # Calm: the roads with the most past flood reports.
        roads = sorted(_nodes(G, "road"), key=lambda r: (-len(_in(G, r, "on")), _label(G, r)))[:TOP_ROADS]
        for r in roads:
            f.node(r)
            for s in _in(G, r, "on"):
                f.edge(s, r)
        lines = [f"{_label(G, r)}: {_plural(len(_in(G, r, 'on')), 'past flood report')}" for r in roads]
        notes = ["NWS alerts could not be checked, so this is not an all-clear."] \
            if getattr(inputs, "alerts_unavailable", False) else []
        if getattr(inputs, "stale", False):
            notes.append(STALE_NOTE)
        return _text("No roads are flagged right now.",
                     "Roads with the most past flood reports:" if roads else "No roads are named in past flood reports.",
                     lines, *notes)

    # Raised: roads on the elevated or high spots, traced back through the rules' edges.
    by_road: dict[str, list[str]] = {}
    for s in raised:
        f.node(s)
        for r in assessment.reasons_for(s):
            f.path(r.path)
        w = G.nodes[s].get("waterway")
        if w and assessment.waterway_levels.get(w) in RAISED:
            for g in _in(G, w, "measures"):
                f.edge(g, w)
            f.edge(w, s)
        for road in _out(G, s, "on"):
            f.edge(s, road)
            by_road.setdefault(road, []).append(s)
    order = {"high": 0, "elevated": 1}
    lines = []
    for road, spots in sorted(by_road.items(), key=lambda kv: (min(order[assessment.spot_levels[s]] for s in kv[1]),
                                                               -len(kv[1]), _label(G, kv[0]))):
        f.node(road)
        top = "high" if any(assessment.spot_levels[s] == "high" for s in spots) else "elevated"
        lines.append(f"{_label(G, road)}: {_plural(len(spots), 'flood spot')} at {top} risk")
    for p in avoid:
        f.edge(p["spot"], build.AVOID_NODE)
    if not lines and not avoid:
        top = assessment.reasons[0] if assessment.reasons else None
        if top:
            f.path(top.path)
        return _text(f"No specific roads are flagged, but area risk is {level}"
                     + (f": {top.label}." if top else "."), *_live_notes(inputs))
    unnamed = len([s for s in raised if not _out(G, s, "on")])
    return _text(
        f"{_plural(len(raised), 'flood spot is', 'flood spots are')} at elevated or high risk right now."
        if raised else f"Area risk is {level}. No flood spot is raised, but the avoid list names these.",
        "Roads on them:" if lines else "", lines,
        f"{_plural(unnamed, 'raised spot names', 'raised spots name')} no road." if unnamed and lines else "",
        "Avoid list (rule 4):" if avoid else "", [p["text"] for p in avoid],
        *_live_notes(inputs),
    )


def _why(G, assessment, inputs, f: _Facts) -> str:
    level = _area_level(assessment, inputs)
    if not assessment.reasons:
        _checked(G, f)
        return _text(RISK_UNKNOWN if level == "unknown" else "No rule is triggered right now.",
                     *_live_notes(inputs, alerts=level != "unknown"))
    shown = assessment.reasons[:MAX_REASONS]
    for r in shown:
        f.path(r.path)
        f.node(r.path[0])
    more = len(assessment.reasons) - len(shown)
    return _text(f"Area risk: {level}. Rules triggered, highest level first:",
                 [f"{r.level.capitalize()} (rule {r.rule}): {explain.chain(G, r)}" for r in shown],
                 f"…and {more} more." if more else "", *_live_notes(inputs))


def _risk(G, assessment, inputs, f: _Facts) -> str:
    area = _area(G)
    if area:
        f.node(area)
    if not assessment.reasons:
        _checked(G, f)
    if _area_level(assessment, inputs) == "unknown":
        return _text(RISK_UNKNOWN, "The other rules say low, but an active alert can't be ruled out.",
                     *_live_notes(inputs, alerts=False))
    if not assessment.reasons:
        return _text(f"Area risk: {assessment.area_level}. Nothing is raising the flood risk right now.",
                     *_live_notes(inputs))
    top = assessment.reasons[0]
    f.path(top.path)
    return _text(f"Area risk: {assessment.area_level}.", f"Top reason (rule {top.rule}): {top.label}.",
                 *_live_notes(inputs))


def _gauge_reading(G, g, inputs) -> tuple[str, str]:
    """(reading such as "10.2 ft" or "no reading", flood status as on the forecast page)."""
    d = G.nodes[g]
    level_ft, (status, _, _) = explain.gauge_status(d.get("lid"), d.get("stages"), inputs)
    return ("no reading" if level_ft is None else f"{round(level_ft, 2):g} ft"), status


def _gauge_line(G, g, inputs) -> str:
    reading, status = _gauge_reading(G, g, inputs)
    return f"{_label(G, g)}: {reading}, {status}"


def _gauges(G, assessment, inputs, f: _Facts) -> str:
    gauges = sorted(_nodes(G, "gauge"), key=lambda g: _label(G, g))
    if not gauges:
        return _text("There are no stream gauges near this ZIP.")
    for g in gauges:
        f.node(g)
        for w in _out(G, g, "measures"):
            f.edge(g, w)
    return _text(f"{_plural(len(gauges), 'stream gauge')} near this ZIP:", [_gauge_line(G, g, inputs) for g in gauges],
                 *_live_notes(inputs, alerts=False))


def _alerts(G, assessment, inputs, f: _Facts) -> str:
    if getattr(inputs, "alerts_unavailable", False):
        return _text("NWS alerts are unavailable: they could not be checked right now.",
                     *_live_notes(inputs, alerts=False))
    flood, other = [], []
    for a in _nodes(G, "alert"):
        d = G.nodes[a]
        level = reason.alert_level(d.get("event") or "")
        is_flood = bool(level) or "flood" in normalize(d.get("event"))
        for v in _out(G, a, "covers"):
            f.edge(a, v)
        f.node(a)
        text = d.get("event") or "Alert"
        if d.get("severity"):
            text += f" ({d['severity']} severity)"
        if d.get("headline"):
            text += f": {d['headline']}"
        (flood if is_flood else other).append(f"{text}, sets {level} risk" if level else text)
    return _text("Active flood alerts:" if flood else "No active flood alerts.", flood,
                 "Other NWS alerts:" if other else "", other, *_live_notes(inputs, alerts=False))


def _help_node(G, h: dict, places: dict) -> str | None:
    if h.get("entry") == "shelter":
        for i, sh in enumerate(places.get("shelters") or []):
            if isinstance(sh, dict) and sh.get("name") == h.get("name") and sh.get("id") == h.get("id"):
                return f"help:shelter_{sh.get('id') or i}"
        return None
    return f"help:{h.get('kind') or slug(h.get('name') or 'help')}"


def _is_911(h: dict) -> bool:
    return h.get("entry") != "shelter" and (h.get("kind") == "emergency" or str(h.get("number") or "") == "911")


def _help(G, assessment, places, inputs, f: _Facts) -> str:
    listed = [h for h in assessment.help_order
              if isinstance(h, dict) and (h.get("entry") != "shelter" or explain.is_open(h))]
    # The first MAX_HELP entries in rule 5's order, but 911 is never cut.
    keep = {i for i, h in enumerate(listed) if _is_911(h)}
    for i in range(len(listed)):
        if len(keep) >= MAX_HELP:
            break
        keep.add(i)
    entries = [h for i, h in enumerate(listed) if i in keep]
    cut = [h for i, h in enumerate(listed) if i not in keep]
    area = _area(G)
    lines = []
    for h in entries:
        n = _help_node(G, h, places)
        if area and n:
            f.edge(area, n)
            f.node(n)
        if h.get("entry") == "shelter":
            where = ", ".join(str(x) for x in (h.get("address"), h.get("city")) if x)
            distance = explain.number(h.get("distance_mi"))
            miles = f" ({distance:.1f} mi)" if distance is not None else ""
            lines.append(f"Shelter: {h.get('name') or 'Shelter'}{', ' + where if where else ''}{miles}")
        else:
            lines.append(f"{h.get('name') or 'Help'}: {h.get('number') or 'no number listed'}")
    shelters = any(h.get("entry") == "shelter" for h in listed)
    first = "Risk is high, so open shelters come first." if assessment.area_level == "high" and shelters else ""
    more = ""
    if cut:
        cut_shelters = sum(1 for h in cut if h.get("entry") == "shelter")
        more = f"…and {len(cut)} more on the Get help page" + (
            f", including {_plural(cut_shelters, 'open shelter')}." if cut_shelters else ".")
    return _text(first, "Where to get help:" if lines else "No help entries are listed for this ZIP.", lines, more,
                 "" if shelters else "No open shelters are listed right now.")


def _history(G, assessment, inputs, f: _Facts) -> str:
    events = sorted(_nodes(G, "storm event"), key=lambda e: G.nodes[e].get("start") or "", reverse=True)
    spots = _nodes(G, "spot")
    reports = f"{_plural(len(spots), 'past flood report')} near this ZIP are in the graph."
    if not events:
        return _text(reports, "No storm events (2 or more reports on consecutive days) are on record.")
    lines = []
    for e in events[:NEWEST_EVENTS]:
        f.node(e)
        reported = _in(G, e, "reported during")
        for s in reported:
            f.edge(s, e)
        line = f"{_label(G, e)}: {_plural(len(reported), 'flood spot')} reported during it"
        disasters = _out(G, e, "declared as")
        for d in disasters:
            f.edge(e, d)
        if disasters:
            line += f"; declared as {', '.join(_label(G, d) for d in disasters)}"
        lines.append(line)
    return _text(reports, f"{_plural(len(events), 'storm event')} on record. The newest:", lines)


def _entity(G, assessment, inputs, n: str, f: _Facts) -> str:
    d = G.nodes[n]
    kind = d.get("type", "")
    f.node(n)
    head = f"{explain.node_name(G, n)} is {'an' if kind == 'area' else 'a'} " \
           f"{explain.TYPE_NAMES.get(kind, kind).lower()} in the knowledge graph."
    facts = []
    if kind == "waterway":
        facts.append(f"Level: {_level(assessment.waterway_levels.get(n), inputs)}.")
    elif kind == "gauge":
        facts.append("Reading: {}, {}.".format(*_gauge_reading(G, n, inputs)))
    elif kind == "area":
        level = _area_level(assessment, inputs)
        facts.append(RISK_UNKNOWN if level == "unknown" else f"Area risk: {level}.")
    elif kind == "road":
        spots = _in(G, n, "on")
        raised = [s for s in spots if assessment.spot_levels.get(s) in RAISED]
        if not raised and getattr(inputs, "alerts_unavailable", False):
            facts.append(f"{_plural(len(spots), 'flood spot')} on it; their risk now is unknown.")
        else:
            facts.append(f"{_plural(len(spots), 'flood spot')} on it, {len(raised)} at elevated or high risk now.")

    groups = []
    for rel in build.EDGE_RELS:
        out, inc = _out(G, n, rel), _in(G, n, rel)
        if out:
            groups.append(f"{rel} → {_plural(len(out), 'node')}: {_names(G, out)}")
        if inc:
            groups.append(f"← {rel} from {_plural(len(inc), 'node')}: {_names(G, inc)}")
        for v in out:
            f.edge(n, v)
        for u in inc:
            f.edge(u, n)
    if kind == "waterway":
        # Roads two hops out: waterway -> floods -> spot -> on -> road.
        roads = []
        for s in _out(G, n, "floods"):
            for road in _out(G, s, "on"):
                f.edge(s, road)
                if road not in roads:
                    roads.append(road)
        spots = _out(G, n, "floods")
        facts.append(f"{_plural(len(spots), 'flood spot')}; roads on them: {_names(G, roads) or 'none named'}.")
    live = kind in ("waterway", "gauge", "area", "road")
    return _text(head, " ".join(facts), "Direct connections:" if groups else "It has no direct connections.",
                 groups, *(_live_notes(inputs) if live else []))


def answer(question: str, G: nx.DiGraph, assessment: reason.Assessment, places: dict,
           inputs: build.LiveInputs) -> Answer:
    """Answer one question from the graph and assessment; never raises on odd input."""
    intent, entity = match(question, G)
    f = _Facts(G)
    places = places or {}
    if intent == "entity":
        text = _entity(G, assessment, inputs, entity, f)
    elif intent == "roads":
        text = _roads(G, assessment, places, inputs, f)
    elif intent == "why":
        text = _why(G, assessment, inputs, f)
    elif intent == "risk":
        text = _risk(G, assessment, inputs, f)
    elif intent == "gauges":
        text = _gauges(G, assessment, inputs, f)
    elif intent == "alerts":
        text = _alerts(G, assessment, inputs, f)
    elif intent == "help":
        text = _help(G, assessment, places, inputs, f)
    elif intent == "history":
        text = _history(G, assessment, inputs, f)
    else:
        text = fallback(G).text
    header = "" if intent in ("risk", "why") else risk_header(assessment, inputs)
    text = _text(EMERGENCY_LINE if emergency(question) else "", header, text)
    return Answer(intent=intent, text=text, facts=f.rows(), nodes=f.nodes)


def fallback(G: nx.DiGraph) -> Answer:
    """What the agent can answer, with the suggestions (also the page's answer when one fails)."""
    return Answer(intent="fallback", text=_text(FALLBACK, "Try one of these:", suggestions(G)))
