"""Shared builders for the story 3 graph tests: a small 08807-like places record and live inputs."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import config
from data import places
from graph import build, reason

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
ZIP = "08807"
AREA = f"area:{ZIP}"
MIDDLE = "water:middle_brook"
WEST = "water:west_branch_middle_brook"
PETERS = "water:peters_brook"

SPOT_2025 = {"id": "s2025", "date": "2025-07-14T20:00:00Z", "type": "FLASH FLOOD",
             "remark": "US 22 at Mountain Ave closed. Water over the road.", "lat": 40.58, "lon": -74.57,
             "waterway": "Middle Brook"}
SPOT_2019 = {"id": "s2019", "date": "2019-07-06T22:00:00Z", "type": "FLOOD",
             "remark": "US 22 at Vosseller Ave flooded.", "lat": 40.58, "lon": -74.58, "waterway": "Middle Brook"}
SPOT_2009 = {"id": "s2009", "date": "2009-08-01T12:00:00Z", "type": "FLOOD",
             "remark": "Old York Rd flooded.", "lat": 40.57, "lon": -74.57, "waterway": "Middle Brook"}
SPOT_WEST = {"id": "swest", "date": "2024-08-19T18:00:00Z", "type": "FLASH FLOOD",
             "remark": "Washington Valley Rd flooded near Martinsville.", "lat": 40.61, "lon": -74.59,
             "waterway": "West Branch Middle Brook"}
SPOT_NONE = {"id": "snone", "date": "2023-09-29T15:00:00Z", "type": "FLOOD",
             "remark": "NJ 28 east of Mountain Ave flooded.", "lat": 40.56, "lon": -74.55, "waterway": None}

GAUGES = [
    {"lid": "BDBN4", "name": "Middle Brook at Bound Brook", "waterway": "Middle Brook", "lat": 40.567,
     "lon": -74.546, "distance_mi": 4.6, "stages": {"action": 10.0, "minor": 11.0}},
    {"lid": "MRTN4", "name": "West Branch Middle Brook near Martinsville", "waterway": "West Branch Middle Brook",
     "lat": 40.612, "lon": -74.591, "distance_mi": 2.4, "stages": {"action": 5.0, "minor": 6.0}},
    {"lid": "SMVN4", "name": "Peters Brook at Somerville", "waterway": "Peters Brook", "lat": 40.575,
     "lon": -74.616, "distance_mi": 1.2, "stages": {}},
]

SHELTERS = [
    {"id": "far", "name": "Far School", "address": "1 Far Rd", "city": "X", "state": "NJ", "zip": "08800",
     "status": "OPEN", "org": "ARC", "lat": 40.7, "lon": -74.7, "distance_mi": 12.5},
    {"id": "near", "name": "Near Gym", "address": "2 Near St", "city": "Y", "state": "NJ", "zip": "08807",
     "status": "OPEN", "org": "ARC", "lat": 40.6, "lon": -74.6, "distance_mi": 1.8},
]


def make_places(spots=None, gauges=None, shelters=None) -> dict:
    gauges = copy.deepcopy(GAUGES if gauges is None else gauges)
    return {
        "zip": ZIP,
        "version": "2026-10-25",
        "area": {"zip": ZIP, "place": "Bridgewater, NJ", "lat": 40.5904, "lon": -74.6267,
                 "county_ugc": "NJC035", "zone": "NJZ010", "wfo": "PHI"},
        "waterways": places.waterways_from(gauges),
        "gauges": gauges,
        "flood_spots": copy.deepcopy([SPOT_2025, SPOT_2019, SPOT_2009, SPOT_WEST, SPOT_NONE]
                                     if spots is None else spots),
        "history": {},
        "shelters": copy.deepcopy(SHELTERS if shelters is None else shelters),
        "help": [dict(h) for h in config.FIXED_HELP],
        "precautions": copy.deepcopy(places.PRECAUTIONS),
    }


def make_inputs(**kw) -> build.LiveInputs:
    kw.setdefault("now", NOW)
    kw.setdefault("rain_in_per_hr_max", 0.0)
    kw.setdefault("rain_6h_in", 0.0)
    return build.LiveInputs(**kw)


def run(places_record=None, **kw):
    """(graph, assessment) for the record (default: make_places()) and inputs built from ``kw``."""
    record = make_places() if places_record is None else places_record
    inputs = make_inputs(**kw)
    G = build.build_graph(record, inputs)
    return G, reason.assess(G, record, inputs)


FLASH_FLOOD_WARNING = {"event": "Flash Flood Warning", "headline": "Flash Flood Warning until 9 PM",
                       "severity": "Severe"}
