"""View environmental intelligence: the ZIP's knowledge graph (live data -> places -> actions) and its risk level."""

import altair as alt
import networkx as nx
import pandas as pd
import streamlit as st

from app_logging import log
from assessment_loader import load_assessment
from data.location import InvalidZipError, resolve_zip
from graph import build, query
from graph.explain import TYPE_NAMES
from ui_shared import ASK_FOR_ZIP, ASK_ICON, entered_zip, md

LOOKUP_UNAVAILABLE = "ZIP lookup unavailable"  # same messages as the forecast page
UNAVAILABLE = "Live data unavailable"
STALE_NOTICE = "Some live data could not be refreshed; cached readings were used."
MAX_REASONS_SHOWN = 12
HUB_LABELS = 20  # "Hubs only" labels this many of the best-connected nodes
RAISED = ("elevated", "high")
RAISED_STROKE = "#d62728"
LEVEL_BADGES = {"low": ("green", ":material/check_circle:"), "elevated": ("orange", ":material/warning:"),
                "high": ("red", ":material/emergency_home:")}
# Fixed color of each node type (display names are explain.TYPE_NAMES).
TYPE_COLORS = {
    "Area": "#0b3d91", "NWS alert": "#d62728", "Rain forecast": "#17becf", "Stream gauge": "#7a5cc4",
    "Waterway": "#2c8fd6", "Flood spot": "#8c564b", "Road": "#5f7487", "Storm event": "#e377c2",
    "FEMA disaster": "#7f2704", "Risk level": "#e8833a", "Precaution": "#c9a227", "Help": "#2ca02c",
}
VIEWS = ["Single item", "Full knowledge graph"]
LABEL_MODES = ["Hubs only", "All", "None"]
MAX_ASKED = 5  # questions and answers kept per ZIP
FACT_COLUMNS = ["Source", "Relationship", "Target"]
NODE_PICK = "node_pick"  # name of the chart's click selection
CONNECTION_COLUMNS = ["Direction", "Relationship", "Connected node", "Type", "Level"]


@st.cache_data(ttl=600, max_entries=32, show_spinner=False)
def spring_layout(nodes: tuple, edges: tuple) -> dict:
    """Node positions for one slice of the graph (cached per node and edge set)."""
    sub = nx.Graph()
    sub.add_nodes_from(nodes)
    sub.add_edges_from(edges)
    pos = nx.spring_layout(sub, seed=42, iterations=100, k=1.8 / max(len(sub), 1) ** 0.5)
    return {n: (float(x), float(y)) for n, (x, y) in pos.items()}


def type_name(G, n) -> str:
    return TYPE_NAMES.get(G.nodes[n].get("type", ""), G.nodes[n].get("type", ""))


def graph_chart(G, levels, node_ids, edges, highlight=None, label_mode="Hubs only", height=720):
    """Altair layers: edges (rules), nodes (circles sized by connections), labels (text)."""
    pos = spring_layout(tuple(sorted(node_ids)), tuple(sorted((u, v) for u, v, _ in edges)))
    degree = pd.Series([n for u, v, _ in edges for n in (u, v)], dtype="object").value_counts()
    neighbours = {n for u, v, _ in edges if highlight in (u, v) for n in (u, v)}
    nodes = pd.DataFrame([
        {
            "id": n, "label": G.nodes[n].get("label") or n, "type": type_name(G, n),
            "level": levels.get(n, "low"), "connections": int(degree.get(n, 0)),
            "raised": levels.get(n, "low") in RAISED, "highlighted": n in neighbours,
            "x": pos[n][0], "y": pos[n][1],
        }
        for n in node_ids
    ])
    edge_rows = pd.DataFrame([
        {
            "source": G.nodes[u].get("label") or u, "relationship": rel, "target": G.nodes[v].get("label") or v,
            "x": pos[u][0], "y": pos[u][1], "x2": pos[v][0], "y2": pos[v][1],
            "highlighted": highlight in (u, v),
        }
        for u, v, rel in edges
    ], columns=["source", "relationship", "target", "x", "y", "x2", "y2", "highlighted"])

    # Raised nodes draw larger so the red outline stands out.
    nodes["size"] = (nodes["connections"] + 1) * nodes["raised"].map({True: 2.5, False: 1.0})
    if label_mode == "All":
        nodes["text"] = nodes["label"]
    elif label_mode == "Hubs only":
        cutoff = nodes["connections"].nlargest(HUB_LABELS).min() if len(nodes) else 0
        keep = (nodes["connections"] >= cutoff) | nodes["raised"] | nodes["highlighted"]
        nodes["text"] = nodes["label"].where(keep, "")
    else:
        nodes["text"] = nodes["label"].where(nodes["highlighted"], "")
    nodes["text"] = nodes["text"].str.slice(0, 32)

    type_pick = alt.selection_point(fields=["type"], bind="legend")
    # Clicking a node selects it; the page reads the pick back through st.altair_chart(on_select=...).
    node_pick = alt.selection_point(name=NODE_PICK, fields=["id"], on="click", empty=False)
    x, y = alt.X("x:Q", axis=None), alt.Y("y:Q", axis=None)
    edge_layer = alt.Chart(edge_rows).mark_rule().encode(
        x=x, y=y, x2="x2:Q", y2="y2:Q",
        color=alt.condition(alt.datum.highlighted, alt.value("crimson"), alt.value("#9aa7b4")),
        opacity=alt.condition(alt.datum.highlighted, alt.value(0.9), alt.value(0.3)),
        strokeWidth=alt.condition(alt.datum.highlighted, alt.value(2), alt.value(0.7)),
        tooltip=[alt.Tooltip("source:N", title="Source"), alt.Tooltip("relationship:N", title="Relationship"),
                 alt.Tooltip("target:N", title="Target")],
    )
    node_layer = alt.Chart(nodes).mark_circle().encode(
        x=x, y=y,
        size=alt.Size("size:Q", scale=alt.Scale(range=[60, 1400]), legend=None),
        color=alt.Color("type:N", title="Node type (click to highlight)",
                        scale=alt.Scale(domain=list(TYPE_COLORS), range=list(TYPE_COLORS.values()))),
        stroke=alt.condition(alt.datum.raised, alt.value(RAISED_STROKE), alt.value("white")),
        strokeWidth=alt.condition(alt.datum.raised, alt.value(3), alt.value(0.8)),
        opacity=alt.condition(type_pick, alt.value(0.95), alt.value(0.08)),
        tooltip=[alt.Tooltip("label:N", title="Name"), alt.Tooltip("type:N", title="Type"),
                 alt.Tooltip("level:N", title="Level"), alt.Tooltip("connections:Q", title="Connections")],
    ).add_params(type_pick, node_pick)
    label_layer = alt.Chart(nodes).mark_text(dy=-12, fontSize=10, fontWeight="bold").encode(
        x=x, y=y, text="text:N",
        opacity=alt.condition(type_pick, alt.value(1), alt.value(0.05)),
    )
    chart = alt.layer(edge_layer, node_layer, label_layer).properties(height=height).interactive()
    return chart, len(nodes), len(edge_rows)


def picked_node(chart_key):
    """The node id clicked in a chart, or None (empty click, or no selection yet)."""
    state = st.session_state.get(chart_key) or {}
    points = (state.get("selection") or {}).get(NODE_PICK) or []
    return points[0].get("id") if points and isinstance(points[0], dict) else None


def highlight_clicked():
    """A node clicked in the full graph becomes the highlighted node, which opens its connections."""
    n = picked_node("intel_graph")
    if n is not None:
        st.session_state["intel_highlight"] = n


def focus_clicked(G):
    """A neighbour clicked in the single-item view becomes the new focus item."""
    n = picked_node("intel_focus_graph")
    if n is not None and n in G:
        st.session_state["intel_item_type"] = type_name(G, n)
        st.session_state["intel_item"] = n


def connections(G, levels, n) -> pd.DataFrame:
    """Every edge into or out of a node: direction, relationship, the node at the other end."""
    rows = [("→ out", d.get("rel", ""), v) for _, v, d in G.out_edges(n, data=True)]
    rows += [("← in", d.get("rel", ""), u) for u, _, d in G.in_edges(n, data=True)]
    return pd.DataFrame(
        [(direction, rel, G.nodes[m].get("label") or m, type_name(G, m), levels.get(m, "low"))
         for direction, rel, m in sorted(rows, key=lambda r: (r[1], r[0], G.nodes[r[2]].get("label") or r[2]))],
        columns=CONNECTION_COLUMNS,
    )


def show_connections(G, levels, n):
    """The bordered panel listing a node's connections."""
    label = G.nodes[n].get("label") or n
    table = connections(G, levels, n)
    with st.container(border=True):
        st.markdown(f"**{md(label)}** · {type_name(G, n)} · level **{levels.get(n, 'low')}**")
        st.caption(f"{len(table)} connections")
        if table.empty:
            st.caption("This node has no connections.")
        else:
            st.dataframe(table, hide_index=True, alt=f"Every connection of {label}")


def select_all(key, options):
    st.session_state[key] = options


def pick_suggestion():
    """Ask the picked suggestion on this run (for the ZIP it was picked on), then clear the pill."""
    st.session_state["ask_pending"] = {"question": st.session_state.get("ask_pill"), "zip": entered_zip()}
    st.session_state["ask_pill"] = None


def safe_answer(question, G, assessment, record, inputs, zip_code):
    """The agent's answer, or the fallback answer when it fails."""
    try:
        return query.answer(question, G, assessment, record, inputs)
    except Exception:
        log.exception("Graph question for %s could not be answered", zip_code)
        return query.fallback(G)


st.caption("How live readings link to waterways, flood spots, roads, past storms and actions, "
           "and the risk level the rules give.")

# The ZIP comes from the sidebar box in the app shell, shared by every page.
zip_code = entered_zip()
# Taken before any early stop, so a picked suggestion is never asked later or for another ZIP.
pending = st.session_state.pop("ask_pending", None)

try:
    loc = resolve_zip(zip_code)
except InvalidZipError:
    st.info(ASK_FOR_ZIP, icon=ASK_ICON)
    st.stop()
except Exception:
    st.error(LOOKUP_UNAVAILABLE)
    st.stop()

with st.spinner("Building the graph..."):
    try:
        record, inputs, G, assessment = load_assessment(loc)
    except Exception:
        log.exception("Knowledge graph could not be built for %s", loc.zip)
        st.error(UNAVAILABLE)
        st.stop()

levels = assessment.node_levels()
levels[build.area_id(loc.zip)] = assessment.area_level
levels[build.risk_id(loc.zip)] = assessment.area_level

color, icon = LEVEL_BADGES[assessment.area_level]
st.subheader(f"{loc.place} ({loc.zip})")
st.badge(f"Area risk: {assessment.area_level}", color=color, icon=icon)
if inputs.stale:
    st.caption(STALE_NOTICE)

if assessment.reasons:
    with st.container(border=True):
        st.markdown("**Why**")
        for r in assessment.reasons[:MAX_REASONS_SHOWN]:
            st.markdown(f"- **{r.level.capitalize()}** (rule {r.rule}): {r.label}")
            st.caption(" → ".join(r.path))
        if len(assessment.reasons) > MAX_REASONS_SHOWN:
            st.caption(f"…and {len(assessment.reasons) - MAX_REASONS_SHOWN} more.")
else:
    st.caption("No rule is triggered right now.")

# --- Ask the graph -----------------------------------------------------------------
st.subheader("Ask the graph")
st.caption("Ask about risk, roads, gauges, alerts, shelters or past floods. Answers come only from this ZIP's "
           "knowledge graph and risk rules, and list the graph facts they used.")
# The question history belongs to one ZIP; a new ZIP starts it over. Only questions are kept:
# each answer is rebuilt from the current graph and assessment on every run.
history = st.session_state.get("ask_history")
if not isinstance(history, dict) or history.get("zip") != loc.zip:
    history = st.session_state["ask_history"] = {"zip": loc.zip, "items": []}

with st.form("ask_form"):
    question_col, ask_col = st.columns([5, 1], vertical_alignment="bottom")
    typed = question_col.text_input("Your question", max_chars=query.MAX_QUESTION, key="ask_text",
                                    placeholder="e.g. Which roads should I avoid?")
    asked = ask_col.form_submit_button("Ask", icon=":material/send:", width="stretch")
st.pills("Suggested questions", query.suggestions(G), key="ask_pill", on_change=pick_suggestion)

picked = pending.get("question") if isinstance(pending, dict) and pending.get("zip") == loc.zip else None
question = str(picked or (typed if asked else "") or "").strip()[:query.MAX_QUESTION]
if question:
    history["items"] = [question, *history["items"]][:MAX_ASKED]
answers = [safe_answer(q, G, assessment, record, inputs, loc.zip) for q in history["items"]]
if question:
    log.info("Graph question for %s matched %s", loc.zip, answers[0].intent)

if answers:
    (newest_q, *earlier_qs), (newest, *earlier) = history["items"], answers
    with st.container(border=True):
        st.markdown(f"**You asked:** {md(newest_q)}")
        st.markdown(md(newest.text))
    with st.expander("Graph facts used", icon=":material/account_tree:"):
        if newest.facts:
            st.dataframe(pd.DataFrame(newest.facts, columns=FACT_COLUMNS), hide_index=True,
                         alt="Graph facts the answer was built from: source, relationship, target")
        else:
            st.caption("This answer didn't need any graph connections.")
    if earlier:
        with st.expander("Earlier questions", icon=":material/history:"):
            for q, a in zip(earlier_qs, earlier):
                st.markdown(f"**{md(q)}**")
                st.markdown(md(a.text))

# --- Graph statistics --------------------------------------------------------------
st.subheader("Graph statistics")
counts = pd.Series([d.get("type") for _, d in G.nodes(data=True)], dtype="object").value_counts()
stats = [
    ("Stream gauges", int(counts.get("gauge", 0))), ("Waterways", int(counts.get("waterway", 0))),
    ("Flood spots", int(counts.get("spot", 0))), ("Roads", int(counts.get("road", 0))),
    ("Storm events", int(counts.get("storm event", 0))), ("Alerts", int(counts.get("alert", 0))),
    ("Help resources", int(counts.get("help", 0))), ("Connections", G.number_of_edges()),
]
for col, (label, value) in zip(st.columns(len(stats)), stats):
    col.metric(label, f"{value:,}", border=True, height="stretch")

# --- Explore -------------------------------------------------------------------------
st.subheader("Explore the graph")
all_edges = [(u, v, d.get("rel", "")) for u, v, d in G.edges(data=True)]
type_options = [TYPE_NAMES[t] for t in TYPE_NAMES if t in set(counts.index)]
rel_options = [r for r in build.EDGE_RELS if r in {rel for *_, rel in all_edges}]
view = st.segmented_control(
    "View", VIEWS, default="Full knowledge graph", key="intel_view",
    help="Single item shows one node and its direct connections; the full graph shows everything at once.",
) or "Full knowledge graph"

if view == "Single item":
    type_col, item_col = st.columns([1, 2])
    chosen_type = type_col.selectbox("Node type", type_options, key="intel_item_type")
    items = sorted((n for n in G if type_name(G, n) == chosen_type), key=lambda n: G.nodes[n].get("label") or n)
    focus = item_col.selectbox("Item", items, format_func=lambda n: G.nodes[n].get("label") or n,
                               key="intel_item")
    if focus is not None:
        focus_edges = [e for e in all_edges if focus in e[:2]]
        focus_nodes = {focus} | {n for u, v, _ in focus_edges for n in (u, v)}
        d = G.nodes[focus]
        st.markdown(f"**{d.get('label') or focus}** · {chosen_type} · level **{levels.get(focus, 'low')}**")
        st.caption(f"`{focus}` · {len(focus_edges)} direct connections")
        chart, _, _ = graph_chart(G, levels, sorted(focus_nodes), focus_edges, highlight=focus, label_mode="All",
                                  height=520)
        st.caption("Click a connected node to explore it.")
        st.altair_chart(chart, width="stretch", alt=f"{d.get('label') or focus} and its direct connections",
                        key="intel_focus_graph", on_select=lambda: focus_clicked(G), selection_mode=NODE_PICK)
        show_connections(G, levels, focus)
else:
    st.session_state.setdefault("intel_types", type_options)
    st.session_state.setdefault("intel_rels", rel_options)
    # A new ZIP can drop a type or relationship; keep only what this graph has.
    st.session_state["intel_types"] = [t for t in st.session_state["intel_types"] if t in type_options]
    st.session_state["intel_rels"] = [r for r in st.session_state["intel_rels"] if r in rel_options]

    type_col, type_btn, rel_col, rel_btn = st.columns([4, 1, 4, 1], vertical_alignment="bottom")
    selected_types = type_col.multiselect("Node types", type_options, key="intel_types")
    type_btn.button("Select all", key="intel_all_types", on_click=select_all, args=("intel_types", type_options))
    selected_rels = rel_col.multiselect("Relationships", rel_options, key="intel_rels")
    rel_btn.button("Select all", key="intel_all_rels", on_click=select_all, args=("intel_rels", rel_options))

    highlight_col, label_col = st.columns([3, 2], vertical_alignment="bottom")
    highlight = highlight_col.selectbox(
        "Highlight a node (optional)", [None] + sorted(G, key=lambda n: G.nodes[n].get("label") or n),
        format_func=lambda n: "None" if n is None else f"{G.nodes[n].get('label') or n} ({type_name(G, n)})",
        key="intel_highlight", help="Colors its connections red and labels its neighbours.",
    )
    label_mode = label_col.segmented_control("Labels", LABEL_MODES, default="Hubs only",
                                             key="intel_labels") or "Hubs only"

    if not selected_types or not selected_rels:
        st.info("Select at least one node type and one relationship to draw the graph.")
    else:
        shown_nodes = [n for n in G if type_name(G, n) in selected_types]
        shown_set = set(shown_nodes)
        shown_edges = [e for e in all_edges if e[2] in selected_rels and e[0] in shown_set and e[1] in shown_set]
        chart, node_count, edge_count = graph_chart(G, levels, shown_nodes, shown_edges, highlight, label_mode)
        st.caption(
            f"Showing **{node_count}** nodes and **{edge_count}** connections. Scroll to zoom, drag to pan, "
            "double-click to reset, hover for details, click a node to see all its connections, "
            "click a legend entry to highlight one node type. Nodes outlined in red are elevated or high."
        )
        st.altair_chart(chart, width="stretch", alt=f"Knowledge graph for {loc.place} ({loc.zip})",
                        key="intel_graph", on_select=highlight_clicked, selection_mode=NODE_PICK)
        if highlight is not None:
            show_connections(G, levels, highlight)
            st.button("Clear selection", key="intel_clear_highlight", icon=":material/close:",
                      on_click=lambda: st.session_state.update(intel_highlight=None))

with st.expander("All facts", icon=":material/table:"):
    search = st.text_input("Filter facts", placeholder="e.g. Middle Brook", key="facts_filter")
    facts = pd.DataFrame(
        [(G.nodes[u].get("label") or u, rel, G.nodes[v].get("label") or v) for u, v, rel in all_edges],
        columns=["Source", "Relationship", "Target"],
    )
    if search:
        facts = facts[facts.apply(lambda row: row.str.contains(search, case=False, regex=False).any(), axis=1)]
    st.dataframe(facts, hide_index=True, alt="Every fact in the knowledge graph: source, relationship, target")
