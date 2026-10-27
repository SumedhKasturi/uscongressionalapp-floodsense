"""AC 1.5 / I/O matrix: expired alerts, past forecast periods and null rain chance."""

from datetime import datetime, timezone

from data import sources
from data.cache import CacheResult


def _result(data, stale=False):
    return CacheResult(data=data, fetched_at="2026-10-25T01:00:00+00:00", source_url="u", stale=stale)


NOW = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


def test_ac_1_5_expired_alerts_are_dropped():
    data = {"features": [
        {"properties": {"event": "Old", "expires": "2026-10-24T21:00:00-04:00"}},
        {"properties": {"event": "Current", "expires": "2026-10-25T08:00:00-04:00"}},
        {"properties": {"event": "NoExpiry"}},
    ]}
    assert [a["event"] for a in sources.alert_summaries(_result(data), now=NOW)] == ["Current", "NoExpiry"]


def test_ac_1_5_past_hourly_periods_are_skipped():
    periods = [
        {"startTime": f"2026-10-24T{h:02d}:00:00-04:00", "endTime": f"2026-10-24T{h + 1:02d}:00:00-04:00"}
        for h in range(18, 23)
    ]
    out = sources.hourly_periods(_result({"properties": {"periods": periods}}), 12, now=NOW)
    # NOW is 22:00 EDT: periods ending at or before 22:00 are gone.
    assert [p["startTime"][11:13] for p in out] == ["22"]
