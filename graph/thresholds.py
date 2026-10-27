"""Every risk-rule threshold, in one place (knowledge-graph.md, "Starting risk rules").

Rain and rise-rate values are starting values, to be tuned by replaying past events.
Gauge thresholds are not here: each gauge's official NWS stages come from the places record.
"""

# Risk levels, lowest first. A level's index is its rank.
LEVELS = ("low", "elevated", "high")

# Rule 1: an active NWS flood alert covering the area sets its level by event type (matched
# case-insensitively), and raises recent flood spots to that level. Other events don't count.
ALERT_EVENT_LEVELS = {
    "Flash Flood Warning": "high",
    "Flood Warning": "high",
    "Flash Flood Emergency": "high",
    "Flash Flood Watch": "elevated",
    "Flood Watch": "elevated",
    "Flood Advisory": "elevated",
}

# Rule 2: a gauge at or above "action" makes its waterway elevated, at or above "minor" high.
# A gauge rising more than this (ft per hour) raises its waterway one level, capped at high.
RISE_FT_PER_HR = 1.0

# Rule 3: forecast rain of at least RAIN_IN_PER_HR, or at least RAIN_6H_IN over the
# next RAIN_LOOKAHEAD_HOURS, makes the area and its flood spots elevated.
RAIN_IN_PER_HR = 1.0
RAIN_6H_IN = 2.0
RAIN_LOOKAHEAD_HOURS = 6

# Rule 4: only flood spots reported within this many years get an "avoid" precaution.
# Older spots stay in the graph.
AVOID_SPOT_MAX_AGE_YEARS = 10
