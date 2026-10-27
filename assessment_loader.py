"""Load a ZIP's places record, live inputs, knowledge graph and risk assessment.

Shared by the intelligence, flood alerts and get help pages so all read the same cached data:
``load_places`` -> ``live_inputs`` -> ``build_graph`` -> ``assess``. The places record and
live inputs are cached for 5 minutes; the graph and the rules are rebuilt on each run.
Callers wrap ``load_assessment`` in their own try/except and show "Live data unavailable"
(the get help page loads ``load_places`` first and still renders when the assessment fails).
"""

from __future__ import annotations

import streamlit as st

from data import places
from graph import build, reason


@st.cache_data(ttl=300, show_spinner=False)
def load_places(loc):
    return places.get_places(loc)


@st.cache_data(ttl=300, show_spinner=False)
def load_inputs(loc, record):
    return build.live_inputs(loc, record)


def load_assessment(loc):
    """(places record, live inputs, graph, assessment) for a resolved Location. May raise."""
    record = load_places(loc)
    inputs = load_inputs(loc, record)
    G = build.build_graph(record, inputs)
    assessment = reason.assess(G, record, inputs)
    return record, inputs, G, assessment
