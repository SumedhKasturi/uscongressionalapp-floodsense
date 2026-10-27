"""Fetch the Hurricane Ida (Sept 1-3, 2021) USGS gauge heights once and write
``data/history/ida_2021.json`` for the offline replay (story 12).

Run from ``uscongressionalapp-floodsense/``:  python scripts/fetch_ida_archive.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make uscongressionalapp-floodsense/ importable (config) when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

import config  # noqa: E402

# The nwis. host, not config.USGS_IV_URL: waterservices.usgs.gov only serves the last
# 120 days of instantaneous values, while nwis.waterservices.usgs.gov has the full archive.
# 00065 is gauge height (ft).
URL = (
    "https://nwis.waterservices.usgs.gov/nwis/iv/?format=json"
    "&sites=01403200,01403150,01400500,01400000,01403060"
    "&parameterCd=00065&startDT=2021-09-01&endDT=2021-09-03"
)
# USGS site -> NWS gauge id (knowledge-graph.md, gauge flood stages table).
NWS_LID = {
    "01403200": "BDBN4",
    "01403150": "MRTN4",
    "01400500": "MNVN4",
    "01400000": "RRTN4",
    "01403060": "BDKN4",
}
# Committed to the repo and read by data.places.load_ida_archive().
OUT = config.APP_DIR / "data" / "history" / "ida_2021.json"


def to_utc(stamp: str) -> str:
    """An ISO timestamp with offset (USGS uses local time) -> UTC "YYYY-MM-DDTHH:MM:SSZ"."""
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def convert(data: dict) -> dict:
    """Turn the USGS IV JSON into the archive format: per NWS gauge id, its sorted

    ``[utc_time, ft]`` readings and the peak reading (None when it has no readings).
    """
    gauges = {}
    for ts in data["value"]["timeSeries"]:
        info = ts["sourceInfo"]
        site = info["siteCode"][0]["value"]
        # USGS marks a missing reading with a sentinel (-999999), not a real height; skip those
        # so they don't show up as a huge drop in the river level.
        no_data = float(ts["variable"].get("noDataValue", -999999.0))
        readings = []
        for block in ts["values"]:
            for v in block["value"]:
                ft = float(v["value"])
                if ft != no_data:
                    readings.append([to_utc(v["dateTime"]), ft])
        # ISO UTC strings sort in time order.
        readings.sort()
        peak = max(readings, key=lambda r: r[1]) if readings else None
        # Key by NWS gauge id (what the rest of the app uses); fall back to the USGS id.
        gauges[NWS_LID.get(site, site)] = {
            "usgs": site,
            "name": info["siteName"],
            "unit": "ft",
            "readings": readings,
            "max": {"time": peak[0], "ft": peak[1]} if peak else None,
        }
    return {
        "event": "Hurricane Ida remnants, Sept 1-2, 2021",
        "source_url": URL,
        "parameter": config.USGS_GAUGE_HEIGHT,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gauges": gauges,
    }


def main() -> int:
    """Download the archive, write it to OUT and print a per-gauge summary. Returns 0.


    Raises on a network error or non-2xx answer (``raise_for_status``), so nothing is written.
    """
    # A longer timeout than HTTP_TIMEOUT: this is a one-off, multi-day download.
    resp = requests.get(URL, headers={"User-Agent": config.nws_user_agent()}, timeout=60)
    resp.raise_for_status()
    archive = convert(resp.json())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(archive, indent=1) + "\n", encoding="utf-8")
    for lid, g in archive["gauges"].items():
        print(f"{lid} ({g['usgs']}): {len(g['readings'])} readings, max {g['max']}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
