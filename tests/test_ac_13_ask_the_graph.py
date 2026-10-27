"""Story 13: Ask the graph, the rule-based query agent on the knowledge graph page.

Unit tests call ``query.answer`` on graphs built from the story 3 fixtures (graph_helpers) and
the recorded 08807 places record. Page tests run the real app shell with AppTest; the places
record and live inputs are patched into ``assessment_loader``, so no request is made.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app_logging
import assessment_loader
from graph import build, explain, query, reason
from tests.graph_helpers import FLASH_FLOOD_WARNING, make_inputs, make_places
from tests.test_ac_9_flood_map_page import fixture_08807

APP = "../app.py"
PAGE = "app_pages/intelligence.py"
ROADS_Q = "Which roads should I avoid?"


def graph(record=None, **kw):
    record = make_places() if record is None else record
    inputs = make_inputs(**kw)
    G = build.build_graph(record, inputs)
    return G, reason.assess(G, record, inputs), record, inputs


def ask(question, record=None, **kw):
    G, assessment, record, inputs = graph(record, **kw)
    return query.answer(question, G, assessment, record, inputs), G, assessment


@pytest.fixture(scope="module")
def record_08807():
    return fixture_08807()


# --- AC 13.1: each intent's sample question, with the graph facts it used ---------------

@pytest.mark.parametrize("question, intent", [
    ("Which roads should I avoid?", "roads"),
    ("Why is the risk high?", "why"),
    ("Am I safe?", "risk"),
    ("What are the river levels?", "gauges"),
    ("Are there any flood alerts?", "alerts"),
    ("Where can I go?", "help"),
    ("Has it flooded here before?", "history"),
    ("What about Middle Brook?", "entity"),
    ("What's for dinner?", "fallback"),
])
def test_ac_13_1_sample_questions_match_intents(record_08807, question, intent):
    ans, G, _ = ask(question, record_08807)
    assert ans.intent == intent
    assert ans.text
    if intent not in ("fallback", "alerts"):  # no alert is active in the fixture
        assert ans.facts, intent
    # Every fact is a real edge of the graph, named as the page names nodes.
    edges = {(explain.node_name(G, u), d["rel"], explain.node_name(G, v)) for u, v, d in G.edges(data=True)}
    assert set(ans.facts) <= edges


def test_ac_13_1_calm_roads_top_five_by_past_reports(record_08807):
    """Matrix row Calm, roads."""
    ans, G, _ = ask(ROADS_Q, record_08807)
    assert ans.text.startswith("No roads are flagged right now.")
    roads = sorted((n for n, d in G.nodes(data=True) if d.get("type") == "road"),
                   key=lambda r: (-sum(1 for u in G.predecessors(r) if G.edges[u, r]["rel"] == "on"),
                                  G.nodes[r]["label"]))[:query.TOP_ROADS]
    lines = [line for line in ans.text.splitlines() if line.startswith("- ")]
    assert len(lines) == 5
    assert [line[2:].split(":")[0] for line in lines] == [G.nodes[r]["label"] for r in roads]
    assert ans.facts and {rel for _, rel, _ in ans.facts} == {"on"}
    assert ans.nodes == roads


def test_ac_13_1_gauges_level_unit_and_status():
    """Matrix row Gauges: one line per gauge, as on the forecast page."""
    ans, G, _ = ask("What are the river levels?", gauge_levels={"BDBN4": 10.2, "MRTN4": 3.0})
    lines = [line for line in ans.text.splitlines() if line.startswith("- ")]
    assert lines == [
        "- Middle Brook at Bound Brook: 10.2 ft, Action stage",
        "- Peters Brook at Somerville: no reading, No flood status",
        "- West Branch Middle Brook near Martinsville: 3 ft, No flooding",
    ]
    assert ("Middle Brook at Bound Brook 10.2 ft", "measures", "Middle Brook") in ans.facts


def test_ac_13_1_gauge_status_uses_nwps_category():
    ans, _, _ = ask("gauge status", gauge_levels={"BDBN4": 10.2}, gauge_categories={"BDBN4": "minor"})
    assert "- Middle Brook at Bound Brook: 10.2 ft, Minor flooding" in ans.text


def test_ac_13_1_alerts_unavailable_am_i_safe():
    """Matrix row Alerts unavailable: low risk with alerts unknown is "unknown"."""
    ans, _, assessment = ask("Am I safe?", alerts_unavailable=True)
    assert assessment.area_level == "low"
    assert ans.intent == "risk"
    assert "Risk unknown: NWS alerts could not be checked" in ans.text
    assert "Area risk: low" not in ans.text


@pytest.mark.parametrize("kw, expected", [
    ({}, "No active flood alerts."),
    ({"alerts_unavailable": True}, "NWS alerts are unavailable"),
    ({"alerts": [FLASH_FLOOD_WARNING]}, "Flash Flood Warning (Severe severity): Flash Flood Warning until 9 PM"),
])
def test_ac_13_1_alerts_answer(kw, expected):
    ans, _, _ = ask("Any alerts?", **kw)
    assert ans.intent == "alerts" and expected in ans.text
    if kw.get("alerts"):
        assert ("Flash Flood Warning", "covers", "Bridgewater, NJ") in ans.facts


def test_ac_13_1_help_high_risk_open_shelters_first():
    """Matrix row Help: open shelters first, then 911 and the other lines, numbers as text."""
    record = make_places()
    record["shelters"].append({"id": "shut", "name": "Closed Hall", "status": "CLOSED", "distance_mi": 0.5})
    ans, _, assessment = ask("Where can I go?", record, alerts=[FLASH_FLOOD_WARNING])
    assert assessment.area_level == "high"
    lines = [line for line in ans.text.splitlines() if line.startswith("- ")]
    assert lines[0].startswith("- Shelter: Near Gym") and lines[1].startswith("- Shelter: Far School")
    assert lines[2] == "- Emergency: 911"
    assert "- FEMA helpline: 1-800-621-3362" in lines
    assert "Closed Hall" not in ans.text
    assert ("Bridgewater, NJ", "served by", "Near Gym") in ans.facts


def test_ac_13_1_help_calm_lines_first():
    ans, _, _ = ask("I need help")
    lines = [line for line in ans.text.splitlines() if line.startswith("- ")]
    assert lines[0] == "- Emergency: 911" and lines[-1].startswith("- Shelter:")


def test_ac_13_1_history_newest_events(record_08807):
    """Matrix row History: event count, newest 3 events with dates, spots reported during each."""
    ans, G, _ = ask("Has it flooded here before?", record_08807)
    events = sorted((n for n, d in G.nodes(data=True) if d.get("type") == "storm event"),
                    key=lambda e: G.nodes[e]["start"], reverse=True)
    assert len(events) >= 3
    assert f"{len(events)} storm events on record" in ans.text
    lines = [line for line in ans.text.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    for line, e in zip(lines, events):
        reported = [u for u in G.predecessors(e) if G.edges[u, e]["rel"] == "reported during"]
        assert line.startswith(f"- {G.nodes[e]['label']}: {len(reported)} flood spot")
    assert {rel for _, rel, _ in ans.facts} <= {"reported during", "declared as"}


def test_ac_13_1_fallback_lists_suggestions():
    """Matrix row Unknown question."""
    ans, G, _ = ask("What's for dinner?")
    assert ans.intent == "fallback"
    assert ans.text.startswith(query.FALLBACK)
    for s in query.suggestions(G):
        assert f"- {s}" in ans.text
    assert ans.facts == []


def test_ac_13_1_stale_data_is_stated():
    ans, _, _ = ask("What are the river levels?", gauge_levels={"BDBN4": 10.2}, stale=True)
    assert query.STALE_NOTE in ans.text
    calm, _, _ = ask("Has it flooded here before?", stale=True)  # history isn't live data
    assert query.STALE_NOTE not in calm.text


def test_ac_13_1_query_module_is_pure():
    source = (Path(query.__file__)).read_text()
    for banned in ("import streamlit", "from streamlit", "requests", "ui_shared", "anthropic", "openai"):
        assert banned not in source, banned


# --- AC 13.2: raised risk traces the rule chain through graph edges -----------------------

def test_ac_13_2_raised_roads_trace_gauge_and_waterway():
    """Matrix row Raised, roads: BDBN4 at 10.2 ft (action) raises Middle Brook's spots."""
    ans, G, assessment = ask(ROADS_Q, gauge_levels={"BDBN4": 10.2})
    assert assessment.waterway_levels["water:middle_brook"] == "elevated"
    assert "3 flood spots are at elevated or high risk right now." in ans.text
    for road in ("US 22", "Old York Rd", "Mountain Ave", "Vosseller Ave"):
        assert f"- {road}: " in ans.text
    assert "Washington Valley Rd" not in ans.text  # West Branch is calm
    assert "Avoid list (rule 4):" in ans.text
    assert "- Avoid US 22 at Mountain Ave closed (flooded Jul 2025; Middle Brook is elevated)" in ans.text
    assert ("Middle Brook at Bound Brook 10.2 ft", "measures", "Middle Brook") in ans.facts
    assert ("Middle Brook", "floods", "US 22 at Mountain Ave closed") in ans.facts
    assert ("US 22 at Mountain Ave closed", "on", "US 22") in ans.facts
    assert ("US 22 at Mountain Ave closed", "avoid", "Avoid flooded roads") in ans.facts


def test_ac_13_2_why_flash_flood_warning_rule_1_chain():
    """Matrix row Why: the rule 1 chain path, level high."""
    ans, G, assessment = ask("Why is the risk high?", alerts=[FLASH_FLOOD_WARNING])
    assert ans.intent == "why"
    assert "Area risk: high." in ans.text
    top = assessment.reasons[0]
    assert top.rule == 1
    assert f"- High (rule 1): {explain.chain(G, top)}" in ans.text
    assert ("Flash Flood Warning", "covers", "Bridgewater, NJ") in ans.facts


def test_ac_13_2_why_gauge_chain_edges():
    ans, G, assessment = ask("Why?", gauge_levels={"BDBN4": 10.2})
    r = assessment.reasons[0]
    assert f"- Elevated (rule 2): {explain.chain(G, r)}" in ans.text
    path_edges = [(explain.node_name(G, u), G.edges[u, v]["rel"], explain.node_name(G, v))
                  for u, v in zip(r.path, r.path[1:])]
    assert path_edges == ans.facts


def test_ac_13_2_why_calm():
    ans, _, _ = ask("Why is the risk high?")
    assert ans.text.startswith("No rule is triggered right now")
    assert {rel for _, rel, _ in ans.facts} == {"falls on", "measures"}  # the inputs the rules checked


def test_ac_13_2_risk_top_reason():
    ans, _, assessment = ask("Should I worry about flooding?", alerts=[FLASH_FLOOD_WARNING])
    assert ans.intent == "risk"
    assert ans.text.startswith("Area risk: high.")
    assert assessment.reasons[0].label in ans.text


# --- AC 13.3: an entity question describes the node and its direct connections ----------

def test_ac_13_3_entity_middle_brook():
    """Matrix row Entity: level, gauge and reading, spot count, roads."""
    ans, G, _ = ask("What about Middle Brook?", gauge_levels={"BDBN4": 10.2})
    assert ans.intent == "entity" and ans.nodes[0] == "water:middle_brook"
    assert ans.text.startswith("Area risk: elevated — Middle Brook at Bound Brook is 10.2 ft")  # P1 header
    assert "\n\nMiddle Brook is a waterway" in ans.text
    assert "Level: elevated." in ans.text
    assert "Middle Brook at Bound Brook 10.2 ft" in ans.text
    assert "3 flood spots; roads on them: US 22; Mountain Ave; Vosseller Ave; Old York Rd." in ans.text
    rels = {rel for _, rel, _ in ans.facts}
    assert {"measures", "floods", "sets", "on"} <= rels
    direct = {(explain.node_name(G, u), d["rel"], explain.node_name(G, v))
              for u, v, d in G.edges(data=True) if "water:middle_brook" in (u, v)}
    assert direct <= set(ans.facts)


@pytest.mark.parametrize("question, node", [
    ("tell me about WEST BRANCH MIDDLE BROOK", "water:west_branch_middle_brook"),  # longest match wins
    ("Is US 22 flooded?", "road:us_22"),
    ("middle brook at bound brook reading", "gauge:BDBN4"),
    ("Is Bridgewater, NJ ok?", "area:08807"),
    ("Which roads near Middle Brook flood?", "water:middle_brook"),  # an entity beats the roads intent
])
def test_ac_13_3_entity_matching(question, node):
    ans, G, _ = ask(question)
    assert ans.intent == "entity" and ans.nodes[0] == node
    assert "Direct connections:" in ans.text


def test_ac_13_3_entity_storm_event(record_08807):
    G, assessment, record, inputs = graph(record_08807)
    label = G.nodes["event:2021-09-01"]["label"]
    ans = query.answer(f"what was the {label}?", G, assessment, record, inputs)
    assert ans.intent == "entity" and ans.nodes[0] == "event:2021-09-01"
    assert "declared as" in ans.text and "Remnants of Hurricane Ida (DR-4614)" in ans.text


def test_ac_13_3_area_unknown_when_alerts_unavailable():
    ans, _, _ = ask("Bridgewater, NJ", alerts_unavailable=True)
    assert query.RISK_UNKNOWN in ans.text


# --- AC 13.4: odd questions degrade safely -------------------------------------------------

@pytest.mark.parametrize("question", ["", "   ", None, "<script>alert(1)</script>", "x" * 500, "**_`[]|#",
                                      "🌊🌊🌊", "\n\n\t"])
def test_ac_13_4_odd_questions_never_raise(question):
    ans, _, _ = ask(question)
    assert isinstance(ans, query.Answer) and ans.text


def test_ac_13_4_long_question_cut_to_200_characters():
    """Matrix row Too long: only the first 200 characters are read."""
    assert query.match("x" * 200 + " roads", build.build_graph(make_places(), make_inputs()))[0] == "fallback"
    assert query.match("x" * 190 + " roads", build.build_graph(make_places(), make_inputs()))[0] == "roads"


def test_ac_13_4_empty_and_degraded_graph():
    record = {"zip": "08807", "area": {"zip": "08807", "place": "Bridgewater, NJ"}, "help": [], "precautions": {}}
    for q in ("Which roads should I avoid?", "What are the river levels?", "Has it flooded here before?",
              "Where can I go?", "Why?", "Am I safe?", "Any alerts?"):
        ans, _, _ = ask(q, record)
        assert ans.text, q


# --- Page section (AppTest) -----------------------------------------------------------------

def patch_data(monkeypatch, record=None, inputs=None):
    record = make_places() if record is None else record
    inputs = inputs or make_inputs()
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: record)
    monkeypatch.setattr(assessment_loader, "load_inputs", lambda loc, rec: inputs)


def open_page():
    at = AppTest.from_file(APP, default_timeout=30).switch_page(PAGE)
    at.run()
    assert not at.exception, at.exception
    return at


def ask_form(at, question):
    # set_value, not input: AppTest's input() drops text over max_chars like the browser does,
    # and the page must still cut a longer value itself.
    at.text_input(key="ask_text").set_value(question)
    next(b for b in at.button if b.label == "Ask").click().run()
    assert not at.exception, at.exception
    return at


def markdowns(at) -> list[str]:
    return [m.value for m in at.markdown]


def history(at) -> list[dict]:
    return at.session_state["ask_history"]["items"]


def test_ac_13_1_section_sits_between_reasons_and_statistics(rsps, monkeypatch):
    patch_data(monkeypatch)
    at = open_page()
    subheaders = [s.value for s in at.subheader]
    assert subheaders.index("Ask the graph") == subheaders.index("Graph statistics") - 1
    assert at.button_group(key="ask_pill").options[0] == query.SUGGESTIONS[0]
    assert not any(m.startswith("**You asked:**") for m in markdowns(at))  # nothing asked yet


def test_ac_13_1_form_submit_shows_answer_and_facts(rsps, monkeypatch, caplog):
    patch_data(monkeypatch)
    at = open_page()
    with caplog.at_level(logging.INFO, logger=app_logging.log.name):
        ask_form(at, ROADS_Q)
    assert f"**You asked:** {ROADS_Q}" in markdowns(at)
    assert any(m.startswith("No roads are flagged right now.") for m in markdowns(at))
    facts = at.dataframe[0].value
    assert list(facts.columns) == ["Source", "Relationship", "Target"]
    assert set(facts["Relationship"]) == {"on"}
    assert any(e.label == "Graph facts used" for e in at.expander)
    messages = [r.getMessage() for r in caplog.records]
    assert "Graph question for 08807 matched roads" in messages
    assert not any(ROADS_Q in m for m in messages)  # the raw question is never logged


def test_ac_13_1_pill_pick_asks_it(rsps, monkeypatch):
    patch_data(monkeypatch, inputs=make_inputs(alerts=[FLASH_FLOOD_WARNING]))
    at = open_page()
    at.button_group(key="ask_pill").set_value("Where can I go for help?").run()
    assert not at.exception, at.exception
    assert "**You asked:** Where can I go for help?" in markdowns(at)
    assert any("Shelter: Near Gym" in m for m in markdowns(at))
    assert at.button_group(key="ask_pill").value is None  # cleared, so it can be picked again
    at.run()  # a plain rerun doesn't ask it again
    assert len(history(at)) == 1


def test_ac_13_1_keeps_last_five(rsps, monkeypatch):
    patch_data(monkeypatch)
    at = open_page()
    for i in range(6):
        ask_form(at, f"Am I safe? {i}")
    assert history(at) == [f"Am I safe? {i}" for i in range(5, 0, -1)]
    assert any(e.label == "Earlier questions" for e in at.expander)


def test_ac_13_2_raised_page_answer(rsps, monkeypatch):
    patch_data(monkeypatch, inputs=make_inputs(gauge_levels={"BDBN4": 10.2}))
    at = open_page()
    ask_form(at, ROADS_Q)
    facts = at.dataframe[0].value
    rows = set(map(tuple, facts.values.tolist()))
    assert ("Middle Brook at Bound Brook 10.2 ft", "measures", "Middle Brook") in rows
    assert ("Middle Brook", "floods", "US 22 at Mountain Ave closed") in rows


def test_ac_13_4_zip_change_clears_history(rsps, monkeypatch):
    """Matrix row ZIP change."""
    patch_data(monkeypatch)
    at = open_page()
    ask_form(at, ROADS_Q)
    assert len(history(at)) == 1
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert at.session_state["ask_history"] == {"zip": "10001", "items": []}
    assert not any(m.startswith("**You asked:**") for m in markdowns(at))


def test_ac_13_4_html_question_shown_as_text(rsps, monkeypatch):
    """Matrix row HTML in question."""
    patch_data(monkeypatch)
    at = open_page()
    ask_form(at, "<script>alert(1)</script>")
    asked = [m for m in markdowns(at) if m.startswith("**You asked:**")]
    assert asked == [r"**You asked:** \<script\>alert(1)\</script\>"]
    assert not any("<script>" in m for m in markdowns(at))


def test_ac_13_4_empty_question_asks_nothing(rsps, monkeypatch):
    """Matrix row Empty."""
    patch_data(monkeypatch)
    at = open_page()
    ask_form(at, "   ")
    assert history(at) == []
    assert not any(m.startswith("**You asked:**") for m in markdowns(at))


def test_ac_13_4_long_question_cut_on_page(rsps, monkeypatch):
    """Matrix row Too long: cut to 200 characters."""
    patch_data(monkeypatch)
    at = open_page()
    assert at.text_input(key="ask_text").max_chars == query.MAX_QUESTION  # the browser stops at 200
    ask_form(at, "Am I safe? " + "x" * 489)
    assert len(history(at)[0]) == query.MAX_QUESTION
    assert any(m.startswith("Area risk: low.") for m in markdowns(at))  # the risk answer


def test_ac_13_4_graph_unavailable_hides_section(rsps, monkeypatch):
    """Matrix row Graph unavailable: the page stops before the section."""
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: make_places())

    def boom(loc):
        raise RuntimeError("assessment down")

    monkeypatch.setattr(assessment_loader, "load_assessment", boom)
    at = open_page()
    assert "Ask the graph" not in [s.value for s in at.subheader]
    assert [e.value for e in at.error] == ["Live data unavailable"]


# --- Review patches (Review Triage Log P1–P11) ------------------------------------------------

FLOOD_WARNING = {"event": "Flood Warning", "headline": "Flood Warning for Middle Brook", "severity": "Moderate"}


def bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_ac_13_p1_emergency_wording_calm_day_says_911():
    ans, _, assessment = ask("Help, my street is flooding")
    assert assessment.area_level == "low"
    assert ans.intent == "roads"  # the frozen order still picks the intent
    assert ans.text.startswith(query.EMERGENCY_LINE)
    assert "911" in ans.text


@pytest.mark.parametrize("question, expected", [
    ("I'm trapped", True), ("water in my basement", True), ("Who do I call?", True), ("rescue", True),
    ("911", True), ("It's flooding now!", True), ("Where can I go for help?", False), ("Am I safe?", False),
    ("rescues", False),
])
def test_ac_13_p1_emergency_words(question, expected):
    assert query.emergency(question) is expected


def test_ac_13_p1_risk_header_names_the_warning_on_a_gauges_answer():
    ans, _, assessment = ask("Is there a flood warning on the river?", alerts=[FLOOD_WARNING])
    assert ans.intent == "gauges"
    assert ans.text.startswith(f"Area risk: high — {assessment.reasons[0].label.split(' → ')[0]}.")
    assert " → " not in ans.text.splitlines()[0]  # the header names only what set the risk off
    assert "Flood Warning covers Bridgewater, NJ" in ans.text.splitlines()[0]


def test_ac_13_p1_risk_header_only_when_raised_or_unknown():
    calm, _, _ = ask("What are the river levels?")
    assert not calm.text.startswith("Area risk")
    unknown, _, _ = ask("What are the river levels?", alerts_unavailable=True)
    assert unknown.text.startswith(query.RISK_UNKNOWN)
    for q in ("Am I safe?", "Why?"):  # the risk and why answers state the level themselves
        ans, _, _ = ask(q, alerts=[FLASH_FLOOD_WARNING])
        assert ans.text.count("Area risk: high") == 1, q


def test_ac_13_p2_raised_area_with_no_flagged_road():
    from tests.graph_helpers import SPOT_2009

    ans, _, assessment = ask(ROADS_Q, make_places(spots=[SPOT_2009]), alerts=[FLASH_FLOOD_WARNING])
    assert assessment.area_level == "high" and not [p for p in assessment.precautions if p["kind"] == "avoid"]
    assert "No roads are flagged right now" not in ans.text
    assert (f"No specific roads are flagged, but area risk is high: {assessment.reasons[0].label}."
            in ans.text)
    assert ("Flash Flood Warning", "covers", "Bridgewater, NJ") in ans.facts


def test_ac_13_p3_stored_answer_follows_current_risk(rsps, monkeypatch):
    live = {"inputs": make_inputs()}
    monkeypatch.setattr(assessment_loader, "load_places", lambda loc: make_places())
    monkeypatch.setattr(assessment_loader, "load_inputs", lambda loc, rec: live["inputs"])
    at = open_page()
    ask_form(at, "Am I safe?")
    assert any(m.startswith("Area risk: low.") for m in markdowns(at))
    live["inputs"] = make_inputs(alerts=[FLASH_FLOOD_WARNING])
    at.run()
    assert not at.exception, at.exception
    assert history(at) == ["Am I safe?"]  # only questions are stored
    assert any(m.startswith("Area risk: high.") for m in markdowns(at))
    assert not any(m.startswith("Area risk: low.") for m in markdowns(at))


def test_ac_13_p4_waterway_level_unknown_when_missing_or_unconfirmed():
    G, assessment, record, inputs = graph()
    del assessment.waterway_levels["water:middle_brook"]
    assert "Level: unknown." in query.answer("Middle Brook", G, assessment, record, inputs).text
    ans, _, _ = ask("Middle Brook", alerts_unavailable=True)
    assert "Level: unknown." in ans.text and "Level: low." not in ans.text


def test_ac_13_p4_road_risk_unknown_when_alerts_unavailable():
    ans, _, _ = ask("Is US 22 flooded?", alerts_unavailable=True)
    assert "2 flood spots on it; their risk now is unknown." in ans.text
    raised, _, _ = ask("Is US 22 flooded?", alerts_unavailable=True, gauge_levels={"BDBN4": 10.2})
    assert "2 flood spots on it, 2 at elevated or high risk now." in raised.text


def test_ac_13_p5_seven_shelters_at_high_risk_keep_911():
    shelters = [{"id": f"s{i}", "name": f"Shelter {i}", "status": "OPEN", "distance_mi": float(i)} for i in range(7)]
    ans, _, _ = ask("Where can I go?", make_places(shelters=shelters), alerts=[FLASH_FLOOD_WARNING])
    lines = bullets(ans.text)
    assert len(lines) == query.MAX_HELP
    assert lines[0] == "- Shelter: Shelter 0 (0.0 mi)" and lines[-1] == "- Emergency: 911"
    assert "…and 5 more on the Get help page, including 2 open shelters." in ans.text
    assert "No open shelters" not in ans.text


def test_ac_13_p5_many_lines_one_shelter_still_counted():
    record = make_places(shelters=[{"id": "only", "name": "Only Gym", "status": "OPEN", "distance_mi": 2.0}])
    record["help"] += [{"name": "Township OEM", "number": "908-555-0100", "kind": "oem"},
                       {"name": "County OEM", "number": "908-555-0101", "kind": "county_oem"}]
    ans, _, _ = ask("Where can I go?", record)
    assert len(bullets(ans.text)) == query.MAX_HELP and "Only Gym" not in ans.text
    assert "…and 1 more on the Get help page, including 1 open shelter." in ans.text
    assert "No open shelters" not in ans.text


@pytest.mark.parametrize("question", ["Where can I get pasta?", "Is the causeway nice?", "Rescues?!"])
def test_ac_13_p6_whole_words_only(question):
    ans, _, _ = ask(question)
    assert ans.intent == "fallback"


def test_ac_13_p6_entity_still_wins_and_text_is_normalized():
    assert ask("Where can I get pasta near Middle Brook?")[0].intent == "entity"
    assert ask("ＷＨＩＣＨ ＲＯＡＤＳ?")[0].intent == "roads"  # NFKC: full-width letters
    assert ask("MIDDLE BROOK")[0].nodes[0] == "water:middle_brook"  # casefolded labels


def test_ac_13_p7_alert_buckets():
    alerts = [FLASH_FLOOD_WARNING, {"event": "Flood Watch", "headline": "", "severity": "Moderate"},
              {"event": "Coastal Flood Statement", "headline": "", "severity": "Minor"},
              {"event": "Wind Advisory", "headline": "Gusts to 50 mph", "severity": "Moderate"}]
    ans, _, _ = ask("Any alerts?", alerts=alerts)
    text = ans.text
    flood_part, other_part = text.split("Active flood alerts:")[1].split("Other NWS alerts:")
    assert "- Flash Flood Warning (Severe severity): Flash Flood Warning until 9 PM, sets high risk" in flood_part
    assert "- Flood Watch (Moderate severity), sets elevated risk" in flood_part
    assert "- Coastal Flood Statement (Minor severity)" in flood_part  # "flood" in the name, no rule level
    assert bullets(other_part) == ["- Wind Advisory (Moderate severity): Gusts to 50 mph"]
    assert "No active flood alerts" not in text


@pytest.mark.parametrize("question, intent", [
    ("Will it flood?", "risk"), ("Will it rain tonight?", "risk"), ("Is it unsafe?", "risk"),
    ("Is a storm coming?", "risk"), ("What's the forecast?", "risk"), ("Is flooding likely?", "risk"),
    ("Has it flooded here before?", "history"), ("Did it ever flood?", "history"),
    ("When was the last time it flooded?", "history"), ("Did it flood in 2021?", "history"),
    ("Is there a flood warning on the river?", "gauges"),
])
def test_ac_13_p8_flood_and_rain_words_tie_break(question, intent):
    assert ask(question)[0].intent == intent


def test_ac_13_p9_incomplete_precaution_and_odd_distance():
    G, assessment, record, inputs = graph(gauge_levels={"BDBN4": 10.2})
    assessment.precautions += [{"kind": "avoid", "text": "no spot"}, {"kind": "avoid", "spot": "spot:s2025"}, "junk"]
    ans = query.answer(ROADS_Q, G, assessment, record, inputs)
    assert "no spot" not in ans.text and ans.intent == "roads"
    shelters = [{"id": "a", "name": "Nan Hall", "status": "OPEN", "distance_mi": float("nan")},
                {"id": "b", "name": "Bool Hall", "status": "OPEN", "distance_mi": True}]
    help_ans, _, _ = ask("Where can I go?", make_places(shelters=shelters), alerts=[FLASH_FLOOD_WARNING])
    assert "- Shelter: Nan Hall" in help_ans.text and "- Shelter: Bool Hall" in help_ans.text
    assert " mi)" not in help_ans.text


def test_ac_13_p9_gauge_entity_reading():
    ans, _, _ = ask("middle brook at bound brook", gauge_levels={"BDBN4": 10.2})
    assert "Reading: 10.2 ft, Action stage." in ans.text


def test_ac_13_p9_failed_answer_shows_fallback_and_logs(rsps, monkeypatch, caplog):
    patch_data(monkeypatch)
    at = open_page()

    def boom(*args):
        raise RuntimeError("query down")

    monkeypatch.setattr(query, "answer", boom)
    with caplog.at_level(logging.INFO, logger=app_logging.log.name):
        ask_form(at, "Am I safe?")
    assert any(m.startswith(query.FALLBACK) for m in markdowns(at))
    messages = [r.getMessage() for r in caplog.records]
    assert "Graph question for 08807 could not be answered" in messages
    assert "Graph question for 08807 matched fallback" in messages


def test_ac_13_p10_pending_suggestion_never_asked_for_another_zip(rsps, monkeypatch):
    patch_data(monkeypatch)
    at = open_page()
    at.session_state["ask_pending"] = {"question": "Am I safe?", "zip": "08807"}
    at.text_input(key="zip").set_value("10001").run()
    assert not at.exception, at.exception
    assert history(at) == []
    # A suggestion queued before an early stop (invalid ZIP) is dropped, not asked later.
    at.session_state["ask_pending"] = {"question": "Am I safe?", "zip": "08807"}
    at.text_input(key="zip").set_value("abc").run()
    at.text_input(key="zip").set_value("08807").run()
    assert not at.exception, at.exception
    assert "ask_pending" not in at.session_state
    assert history(at) == []


_HONESTY = [
    (ROADS_Q, {}, "stale", query.STALE_NOTE),
    (ROADS_Q, {}, "alerts_unavailable", "NWS alerts could not be checked, so this is not an all-clear."),
    (ROADS_Q, {"gauge_levels": {"BDBN4": 10.2}}, "stale", query.STALE_NOTE),
    (ROADS_Q, {"gauge_levels": {"BDBN4": 10.2}}, "alerts_unavailable", query.ALERTS_UNCHECKED),
    ("Why?", {"gauge_levels": {"BDBN4": 10.2}}, "stale", query.STALE_NOTE),
    ("Why?", {"gauge_levels": {"BDBN4": 10.2}}, "alerts_unavailable", query.ALERTS_UNCHECKED),
    ("Am I safe?", {"gauge_levels": {"BDBN4": 10.2}}, "stale", query.STALE_NOTE),
    ("Am I safe?", {"gauge_levels": {"BDBN4": 10.2}}, "alerts_unavailable", query.ALERTS_UNCHECKED),
    ("What about Middle Brook?", {}, "stale", query.STALE_NOTE),
    ("What about Middle Brook?", {}, "alerts_unavailable", query.ALERTS_UNCHECKED),
]


@pytest.mark.parametrize("question, kw, flag, note", _HONESTY)
def test_ac_13_p11_honesty_notes(question, kw, flag, note):
    ans, _, _ = ask(question, **kw, **{flag: True})
    assert note in ans.text
    clean, _, _ = ask(question, **kw)
    assert note not in clean.text


def test_ac_13_p11_calm_why_alerts_unavailable_is_unknown():
    ans, _, _ = ask("Why?", alerts_unavailable=True)
    assert ans.text.startswith(query.RISK_UNKNOWN)
    assert "No rule is triggered" not in ans.text


def test_ac_13_p11_raised_roads_high_lines_in_order():
    ans, _, _ = ask(ROADS_Q, alerts=[FLASH_FLOOD_WARNING])
    roads = ans.text.split("Roads on them:")[1].split("Avoid list (rule 4):")[0]
    assert bullets(roads) == [
        "- Mountain Ave: 2 flood spots at high risk",
        "- US 22: 2 flood spots at high risk",
        "- NJ 28: 1 flood spot at high risk",
        "- Vosseller Ave: 1 flood spot at high risk",
        "- Washington Valley Rd: 1 flood spot at high risk",
    ]
    assert bullets(ans.text.split("Avoid list (rule 4):")[1])[0] == \
        "- Avoid US 22 at Mountain Ave closed (flooded Jul 2025; Flash Flood Warning)"


def test_ac_13_p11_earlier_questions_and_alert_text_escaped(rsps, monkeypatch):
    alert = {"event": "Flood Advisory", "headline": "Rain *heavy* <b>now</b>", "severity": "Minor"}
    patch_data(monkeypatch, inputs=make_inputs(alerts=[alert]))
    at = open_page()
    ask_form(at, "<script>")
    ask_form(at, "Any alerts?")
    assert r"**\<script\>**" in markdowns(at)  # under "Earlier questions"
    assert any(r"Rain \*heavy\* \<b\>now\</b\>" in m for m in markdowns(at))
    assert not any("<b>" in m or "<script>" in m for m in markdowns(at))


def test_ac_13_p11_road_entity_counts_when_raised():
    us22, _, _ = ask("Is US 22 flooded?", gauge_levels={"BDBN4": 10.2})
    assert "2 flood spots on it, 2 at elevated or high risk now." in us22.text
    mountain, _, _ = ask("Mountain Ave?", gauge_levels={"BDBN4": 10.2})
    assert "2 flood spots on it, 1 at elevated or high risk now." in mountain.text  # NJ 28's spot has no waterway


@pytest.mark.parametrize("question, intent", [
    ("Why are the roads closed?", "roads"),  # roads before why
    ("Why is the river high?", "why"),  # why before gauges
    ("Is it safe to call 911?", "risk"),  # risk before help
    ("Any alerts about past storms?", "alerts"),  # alerts before history
])
def test_ac_13_p11_mixed_keywords_follow_intent_order(question, intent):
    ans, _, _ = ask(question)
    assert ans.intent == intent
    assert ans.text.startswith(query.EMERGENCY_LINE) is query.emergency(question)
