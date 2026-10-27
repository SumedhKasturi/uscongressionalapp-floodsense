"""Per-ZIP places data, generated from public APIs and versioned in Supabase.

``build_places(loc)`` fetches every section in parallel and returns one record::

    {"zip", "version", "built_at", "stale", "all_sources_failed", "status": {...},
     "area", "waterways", "gauges", "flood_spots", "history", "shelters", "help", "precautions"}

``get_places(loc)`` returns the newer of the Supabase version and the local copy in
``data/cache/places/{zip}.json``, and rebuilds it when it is ``PLACES_MAX_AGE_DAYS``
or more days old, or has a failed or carried section and was built over
``PLACES_RETRY_AFTER_MINUTES`` ago. A rebuild fills any failed
section from the previous version (status "carried"). Nothing here is hand-curated: every place
comes from the sources below, for any US ZIP.

CLI: ``python -m data.places <zip> [--force]``
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

import config
from data import sources, supabase_rest
from data.location import Location, resolve_zip

log = logging.getLogger(__name__)

# Top-level sections of every places record.
SECTIONS = ("area", "waterways", "gauges", "flood_spots", "history", "shelters", "help", "precautions")
# history has three parts, each with its own status key "history.<part>".
HISTORY_PARTS = ("warnings", "disasters", "flood_claims")
# Section statuses: ok; failed (no data); partial (some of the data is missing);
# carried (this build failed, so the data was copied from the previous version).
OK, FAILED, PARTIAL, CARRIED = "ok", "failed", "partial", "carried"
STATUS_VALUES = (OK, FAILED, PARTIAL, CARRIED)
# A record with any of these section statuses is retried on the next get_places.
RETRY_STATUSES = (FAILED, CARRIED)

# NWPS reports a flood category that has not been set for a gauge with stage -9999,
# a placeholder rather than a real height, so stages at or below it are dropped.
NWPS_NO_STAGE = -9999
STAGE_NAMES = ("action", "minor", "moderate", "major")
LATEST_WARNINGS = 10  # most recent warnings kept in full; the rest are only counted
# OpenFEMA returns at most NFIP_PAGE rows per request, so flood claims are paged (up to NFIP_MAX_PAGES).
NFIP_PAGE = 10000
NFIP_MAX_PAGES = 10

# Written once by scripts/fetch_ida_archive.py and committed.
IDA_ARCHIVE = config.APP_DIR / "data" / "history" / "ida_2021.json"

# State postal code -> state FIPS code, to turn a county UGC (e.g. NJC035) into the
# FIPS pair OpenFEMA filters on. A standard code table, not places data.
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "AS": "60", "GU": "66", "MP": "69", "PR": "72", "VI": "78",
}

# Generic NWS-style precautions, keyed by hazard and risk level (the same for every ZIP).
PRECAUTIONS: dict[str, dict[str, list[str]]] = {
    "flood": {
        "low": [
            "Know whether your home, work and usual routes are in a flood-prone area.",
            "Keep an emergency kit with water, food, medicine, a flashlight and a phone charger.",
        ],
        "elevated": [
            "Move cars, valuables and important papers to higher ground.",
            "Clear storm drains and gutters near your home if it is safe to do so.",
            "Plan a route to higher ground and check on neighbors who may need help.",
        ],
        "high": [
            "Turn around, don't drown: never drive or walk through flood water.",
            "Move to higher ground now if you are told to evacuate or water is rising.",
            "Avoid flooded roads and stay away from creeks, streams and storm drains.",
        ],
    },
    "flash_flood": {
        "low": ["Flash floods can start within minutes of heavy rain. Know how you would get to higher ground."],
        "elevated": [
            "Be ready to move to higher ground quickly; flash floods rise in minutes.",
            "Avoid underpasses, low-water crossings and roads next to streams.",
        ],
        "high": [
            "Move to higher ground immediately. Do not wait for instructions.",
            "Never drive into flood water. Six inches can knock you down; a foot can float a car.",
            "If water surrounds your car and is rising, leave the car and move to higher ground if safe.",
        ],
    },
    "heavy_rain": {
        "low": ["Watch the forecast for heavy rain that could cause street flooding."],
        "elevated": [
            "Expect ponding on roads and slower travel; allow extra time.",
            "Keep basement valuables off the floor and check your sump pump.",
        ],
        "high": [
            "Avoid travel during the heaviest rain if you can.",
            "Stay away from flooded roads, creeks and storm drains.",
        ],
    },
}


class PlacesError(ValueError):
    """A places record failed validation."""


# --- small helpers -----------------------------------------------------------

def _utc_now() -> datetime:
    """Current time in UTC (timezone-aware).


    Version dates use UTC so the app, the cleanup job and Supabase all agree on which
    day it is, wherever they run.
    """
    return datetime.now(timezone.utc)


def today_utc() -> str:
    """Today's UTC date as "YYYY-MM-DD", the version a build made now would get."""
    return _utc_now().date().isoformat()


def _headers() -> dict:
    """Default request headers; api.weather.gov rejects requests without an identifying User-Agent."""
    return {"User-Agent": config.nws_user_agent(), "Accept": "application/json"}


def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    """GET ``url`` and return the parsed JSON.


    Raises a ``requests`` error on a timeout, connection problem or non-2xx status,
    and ValueError when the body is not JSON.
    """
    resp = requests.get(url, params=params, headers=headers or _headers(), timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _num(value) -> float | None:
    """``value`` as a float, or None for anything that isn't a number (including NaN).


    Booleans are rejected: bool is a subclass of int, so True would otherwise become 1.0.
    """
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # drop NaN


def _is_num(value) -> bool:
    """True for an actual int or float (not a bool, not NaN); validation checks types without converting."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def spot_id(when: str, lat: float, lon: float) -> str:
    """Stable id for a storm report: a hash of its date, lat and lon."""
    return hashlib.sha1(f"{when}|{lat:.4f}|{lon:.4f}".encode()).hexdigest()[:12]


def slug(name: str) -> str:
    """"Middle Brook" -> "middle_brook": a lowercase id made of letters, digits and underscores."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# Gauge names read "<waterway> at/near/above/below <place>"; split off the place.
_WATERWAY_SPLIT = re.compile(r"\s+(?:at|near|above|below)\s+", re.IGNORECASE)


def waterway_from_gauge_name(name: str | None) -> str | None:
    """"Middle Brook at Bound Brook" -> "Middle Brook" (also near / above / below)."""
    if not isinstance(name, str):
        return None
    head = _WATERWAY_SPLIT.split(name.strip(), maxsplit=1)[0]
    head = re.sub(r"\s+", " ", head).strip(" ,-")
    return head or None


def county_fips(county_ugc: str | None) -> tuple[str, str] | None:
    """``"NJC035"`` -> ``("34", "035")``; None for anything that isn't a county UGC."""
    m = re.fullmatch(r"([A-Z]{2})C(\d{3})", county_ugc or "")
    if not m or m.group(1) not in STATE_FIPS:
        return None
    return STATE_FIPS[m.group(1)], m.group(2)


def _ugc_from_url(url: str | None) -> str | None:
    """".../zones/county/NJC035" -> "NJC035": the last path part of an NWS URL, or None."""
    if not isinstance(url, str) or not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


# --- fetchers (one per source; each returns data or raises) --------------------

def fetch_point(loc: Location) -> dict:
    """County UGC, forecast zone and NWS office id (WFO) for ``loc``, from the NWS points API.


    Raises RuntimeError when the NWS point lookup is unavailable.
    """
    result = sources.nws_point(loc)
    if result is None:
        raise RuntimeError("NWS point unavailable")
    props = (result.data or {}).get("properties") or {}
    return {
        "county_ugc": _ugc_from_url(props.get("county")),
        "zone": _ugc_from_url(props.get("forecastZone")),
        "wfo": props.get("gridId") or props.get("cwa"),
    }


def fetch_flood_spots(loc: Location, now: datetime | None = None) -> list[dict]:
    """Flood storm reports (NWS Local Storm Reports, via IEM) near ``loc``, newest first.


    Covers STORM_REPORT_RADIUS_MI around the ZIP since HISTORY_START. Each spot's
    ``waterway`` is filled in later by :func:`link_spots`. Raises on a request error
    or an unexpected response.
    """
    now = now or _utc_now()
    data = _get_json(
        f"{config.IEM_BASE}/api/1/nws/lsrs_by_point.json",
        params={
            "lat": f"{loc.lat:.4f}",
            "lon": f"{loc.lon:.4f}",
            "radius_miles": config.STORM_REPORT_RADIUS_MI,
            "begints": f"{config.HISTORY_START}T00:00Z",
            "endts": now.strftime("%Y-%m-%dT%H:%MZ"),
        },
    )
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected storm report response")
    # Keep only flood reports (flood, flash flood, coastal flood...) that have a location and a time.
    spots, seen = [], set()
    for r in rows:
        if "FLOOD" not in str((r or {}).get("typetext") or "").upper():
            continue
        lat, lon, when = _num(r.get("lat")), _num(r.get("lon")), r.get("valid")
        if lat is None or lon is None or not isinstance(when, str):
            continue
        sid = spot_id(when, lat, lon)
        if sid in seen:  # IEM repeats corrected reports at the same time and place
            continue
        seen.add(sid)
        spots.append(
            {
                "id": sid,
                "date": when,
                "type": r.get("typetext"),
                "remark": (r.get("remark") or "").strip(),
                "lat": lat,
                "lon": lon,
                "waterway": None,
            }
        )
    spots.sort(key=lambda s: s["date"], reverse=True)
    return spots


def _gauge_stages(lid: str) -> dict:
    """Official flood stages (ft) of one NWPS gauge, e.g. ``{"minor": 9.0, "moderate": 11.0}``.


    Categories the NWS has not set are left out. Raises on a request error.
    """
    data = _get_json(f"{config.NWPS_BASE}/gauges/{lid}")
    cats = ((data or {}).get("flood") or {}).get("categories") or {}
    stages = {}
    for name in STAGE_NAMES:
        stage = _num((cats.get(name) or {}).get("stage"))
        if stage is not None and stage > NWPS_NO_STAGE:
            stages[name] = stage
    return stages


def fetch_gauges(loc: Location) -> tuple[list[dict], bool]:
    """NWPS gauges nearest the ZIP with their official flood stages. Returns (gauges, all_stages_ok).

    Raises RuntimeError when the gauge search fails. ``all_stages_ok`` is False when
    some gauge's stages could not be fetched; that gauge keeps empty ``stages``.
    """
    levels = sources.gauges_near(loc, n=config.PLACES_GAUGE_COUNT)
    if levels is None:
        raise RuntimeError("NWPS gauge search unavailable")
    # Keep only gauges we can tie to a waterway and place on the map.
    gauges = []
    for g in levels:
        waterway = waterway_from_gauge_name(g.name)
        if not waterway or g.lat is None or g.lon is None:
            log.info("Skipping gauge %s (%r): no waterway or location", g.site, g.name)
            continue
        gauges.append(
            {
                "lid": g.site,
                "name": g.name,
                "waterway": waterway,
                "lat": g.lat,
                "lon": g.lon,
                "distance_mi": None if g.distance_mi is None else round(g.distance_mi, 2),
                "stages": {},
            }
        )
    # Fetch every gauge's stages in parallel; one failure only makes the section partial.
    all_ok = True
    if gauges:
        with ThreadPoolExecutor(max_workers=len(gauges)) as pool:
            futures = {g["lid"]: pool.submit(_gauge_stages, g["lid"]) for g in gauges}
        for g in gauges:
            try:
                g["stages"] = futures[g["lid"]].result()
            except Exception as exc:  # noqa: BLE001 - one gauge's stages failing must not fail the section
                all_ok = False
                log.warning("Flood stages unavailable for %s: %s", g["lid"], exc)
    return gauges, all_ok


def fetch_warnings(county_ugc: str | None, today: str) -> dict:
    """NWS warnings, watches and advisories (VTEC events, via IEM) for the county since

    HISTORY_START: the total, a count per type, and the LATEST_WARNINGS most recent.

    Raises RuntimeError without a county UGC and ValueError on an unexpected response.
    """
    if not county_ugc:
        raise RuntimeError("no county UGC")
    data = _get_json(
        f"{config.IEM_BASE}/json/vtec_events_byugc.py",
        params={"ugc": county_ugc, "sdate": config.HISTORY_START, "edate": today},
    )
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise ValueError("unexpected VTEC response")
    # Count events per phenomenon code (FF = flash flood, FL = flood, ...).
    by_phenomena: dict[str, dict] = {}
    for e in events:
        ph = (e or {}).get("phenomena")
        if not ph:
            continue
        entry = by_phenomena.setdefault(ph, {"name": e.get("ph_name") or ph, "count": 0})
        entry["count"] += 1
    latest = sorted((e for e in events if isinstance(e, dict)), key=lambda e: e.get("issue") or "", reverse=True)
    return {
        "ugc": county_ugc,
        "since": config.HISTORY_START,
        "count": len(events),
        "by_phenomena": dict(sorted(by_phenomena.items(), key=lambda kv: -kv[1]["count"])),
        "latest": [
            {
                "issue": e.get("issue"),
                "expire": e.get("expire"),
                "phenomena": e.get("phenomena"),
                "significance": e.get("significance"),
                "name": e.get("name"),
            }
            for e in latest[:LATEST_WARNINGS]
        ],
    }


def fetch_disasters(county_ugc: str | None) -> dict:
    """FEMA disaster declarations for the county (OpenFEMA), newest first.


    Raises RuntimeError when the county has no FIPS code and ValueError on an
    unexpected response.
    """
    fips = county_fips(county_ugc)
    if fips is None:
        raise RuntimeError(f"no county FIPS for {county_ugc!r}")
    state, county = fips
    data = _get_json(
        f"{config.OPENFEMA_BASE}/v2/DisasterDeclarationsSummaries",
        params={
            "$filter": f"fipsStateCode eq '{state}' and fipsCountyCode eq '{county}'",
            "$orderby": "declarationDate desc",
            "$top": "1000",
        },
    )
    rows = data.get("DisasterDeclarationsSummaries") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected OpenFEMA disasters response")
    # The same disaster number can appear in several rows; keep one entry per disaster.
    seen, items = set(), []
    for r in rows:
        number = (r or {}).get("disasterNumber")
        if number in seen:
            continue
        seen.add(number)
        items.append(
            {
                "number": number,
                "id": r.get("femaDeclarationString"),
                "declared": (r.get("declarationDate") or "")[:10],
                "incident_type": r.get("incidentType"),
                "title": r.get("declarationTitle"),
                "area": r.get("designatedArea"),
            }
        )
    items.sort(key=lambda d: d["declared"], reverse=True)
    return {"fips": state + county, "count": len(items), "items": items}


def fetch_flood_claims(zip_code: str) -> dict:
    """NFIP flood insurance claims for the ZIP (OpenFEMA): count, total paid, and per-year totals.


    Raises on a request error or an unexpected response.
    """
    rows: list[dict] = []
    # Page through the claims until a page comes back short (the last one).
    for page in range(NFIP_MAX_PAGES):
        data = _get_json(
            f"{config.OPENFEMA_BASE}/v3/NfipClaims",
            params={
                "$filter": f"reportedZipCode eq '{zip_code}'",
                "$select": "yearOfLoss,amountPaidOnBuildingClaim,amountPaidOnContentsClaim,"
                "amountPaidOnIncreasedCostOfComplianceClaim",
                "$top": str(NFIP_PAGE),
                "$skip": str(page * NFIP_PAGE),
            },
        )
        batch = data.get("NfipClaims") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise ValueError("unexpected OpenFEMA NFIP response")
        rows.extend(batch)
        if len(batch) < NFIP_PAGE:
            break
    # A claim's payout is building + contents + increased-cost-of-compliance; total them by year of loss.
    by_year: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        paid = sum(
            _num(r.get(k)) or 0.0
            for k in (
                "amountPaidOnBuildingClaim",
                "amountPaidOnContentsClaim",
                "amountPaidOnIncreasedCostOfComplianceClaim",
            )
        )
        year = str(r.get("yearOfLoss") or "unknown")
        entry = by_year.setdefault(year, {"count": 0, "paid": 0.0})
        entry["count"] += 1
        entry["paid"] = round(entry["paid"] + paid, 2)
        total += paid
    return {"zip": zip_code, "count": len(rows), "total_paid": round(total, 2), "by_year": dict(sorted(by_year.items()))}


def fetch_shelters(loc: Location) -> list[dict]:
    """Shelters within SHELTER_RADIUS_MI from FEMA's National Shelter System, nearest first.


    Raises ValueError when the service reports an error or answers in an unexpected shape.
    """
    data = _get_json(
        config.FEMA_SHELTERS_URL,
        params={
            "where": "1=1",
            # ArcGIS query: features within the radius of this point. ArcGIS points are x,y = lon,lat;
            # 4326 means plain GPS latitude/longitude.
            "geometry": f"{loc.lon:.4f},{loc.lat:.4f}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "distance": str(config.SHELTER_RADIUS_MI),
            "units": "esriSRUnit_StatuteMile",
            "outFields": "*",
            "outSR": "4326",
            "f": "json",
        },
    )
    # ArcGIS can answer HTTP 200 with an "error" object, so the body is checked too.
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("features"), list):
        raise ValueError(f"unexpected FEMA shelters response: {str(data)[:200]}")
    shelters = []
    for f in data["features"]:
        a = (f or {}).get("attributes") or {}
        geom = (f or {}).get("geometry") or {}
        # Use the feature's geometry, else its lat/lon attributes; skip shelters with neither.
        lat, lon = _num(geom.get("y")), _num(geom.get("x"))
        if lat is None or lon is None:
            lat, lon = _num(a.get("latitude")), _num(a.get("longitude"))
        if lat is None or lon is None:
            continue
        shelters.append(
            {
                "id": a.get("shelter_id"),
                "name": (a.get("shelter_name") or "").strip(),
                "address": (a.get("address") or "").strip(),
                "city": (a.get("city") or "").strip(),
                "state": a.get("state"),
                "zip": a.get("zip"),
                "status": a.get("shelter_status"),
                "org": a.get("org_name"),
                "lat": lat,
                "lon": lon,
                "distance_mi": round(sources.haversine_mi(loc.lat, loc.lon, lat, lon), 2),
            }
        )
    shelters.sort(key=lambda s: s["distance_mi"])
    return shelters


def fetch_office(wfo: str | None) -> dict:
    """Help-line entry for the ZIP's NWS forecast office (name and phone number).


    Raises RuntimeError without an office id and ValueError when the office lists no phone.
    """
    if not wfo:
        raise RuntimeError("no NWS office id")
    data = _get_json(
        f"{config.NWS_BASE}/offices/{wfo}",
        headers={"User-Agent": config.nws_user_agent(), "Accept": "application/ld+json"},
    )
    name, phone = (data or {}).get("name"), (data or {}).get("telephone")
    if not phone:
        raise ValueError(f"NWS office {wfo} has no telephone")
    return {"name": f"NWS {name or wfo} forecast office", "number": phone, "kind": "nws_office", "wfo": wfo}


# --- linking -----------------------------------------------------------------

def nearest_gauge(lat: float, lon: float, gauges: list[dict], max_mi: float) -> dict | None:
    """The gauge closest to ``(lat, lon)`` within ``max_mi`` miles, or None if none is that close."""
    best, best_d = None, None
    for g in gauges:
        d = sources.haversine_mi(lat, lon, g["lat"], g["lon"])
        if d <= max_mi and (best_d is None or d < best_d):
            best, best_d = g, d
    return best


def link_spots(spots: list[dict], gauges: list[dict]) -> None:
    """Give each spot the waterway of its nearest gauge within SPOT_GAUGE_LINK_MI (else None)."""
    for s in spots:
        g = nearest_gauge(s["lat"], s["lon"], gauges, config.SPOT_GAUGE_LINK_MI)
        s["waterway"] = g["waterway"] if g else None


def waterways_from(gauges: list[dict]) -> list[dict]:
    """One waterway entry per distinct gauge waterway, listing its gauge ids, in gauge order."""
    out: dict[str, dict] = {}
    for g in gauges:
        w = out.setdefault(g["waterway"], {"id": slug(g["waterway"]), "name": g["waterway"], "gauges": []})
        w["gauges"].append(g["lid"])
    return list(out.values())


# --- build -------------------------------------------------------------------

def _run(fn: Callable, *args):
    """Run a fetcher, returning (result, ok)."""
    try:
        return fn(*args), True
    except Exception as exc:  # noqa: BLE001 - any failure leaves that section empty
        log.warning("Places source %s failed: %s", getattr(fn, "__name__", fn), exc)
        return None, False


def build_places(loc: Location, now: datetime | None = None) -> dict:
    """Build a places record for ``loc`` from every public source, fetched in parallel.

    Never raises: a failed source leaves its section empty with status failed (or
    partial), and ``all_sources_failed`` is True when no source answered at all.
    """
    now = now or _utc_now()
    today = now.date().isoformat()

    # Start every fetch at once. Warnings, disasters and the office need the NWS point
    # (county and office id) first, so those workers wait for it.
    with ThreadPoolExecutor(max_workers=8) as pool:
        point_f = pool.submit(_run, fetch_point, loc)

        def after_point(fn, field):
            """Wait for the NWS point, then run ``fn`` on one of its fields; (None, False) if the point failed."""
            point, ok = point_f.result()
            return _run(fn, (point or {}).get(field)) if ok else (None, False)

        spots_f = pool.submit(_run, fetch_flood_spots, loc, now)
        gauges_f = pool.submit(_run, fetch_gauges, loc)
        shelters_f = pool.submit(_run, fetch_shelters, loc)
        claims_f = pool.submit(_run, fetch_flood_claims, loc.zip)
        def fetch_warnings_today(county_ugc):
            """fetch_warnings with today's date filled in, so after_point can pass just the county."""
            return fetch_warnings(county_ugc, today)

        warnings_f = pool.submit(after_point, fetch_warnings_today, "county_ugc")
        disasters_f = pool.submit(after_point, fetch_disasters, "county_ugc")
        office_f = pool.submit(after_point, fetch_office, "wfo")

        # Collect every (result, ok) pair.
        point, point_ok = point_f.result()
        spots, spots_ok = spots_f.result()
        gauges_res, gauges_ok = gauges_f.result()
        shelters, shelters_ok = shelters_f.result()
        claims, claims_ok = claims_f.result()
        warnings, warnings_ok = warnings_f.result()
        disasters, disasters_ok = disasters_f.result()
        office, office_ok = office_f.result()

    # fetch_gauges returns (gauges, all_stages_ok). Then tie each flood spot to its nearest gauge's waterway.
    gauges, stages_ok = gauges_res if gauges_ok else ([], False)
    spots = spots or []
    link_spots(spots, gauges)

    # Area and help still have data when their source fails (the ZIP's location, the fixed
    # help lines), so they become partial rather than failed.
    status = {
        "area": OK if point_ok else PARTIAL,
        "gauges": (OK if stages_ok else PARTIAL) if gauges_ok else FAILED,
        "waterways": OK if gauges_ok else FAILED,
        "flood_spots": OK if spots_ok else FAILED,
        "shelters": OK if shelters_ok else FAILED,
        "help": OK if office_ok else PARTIAL,
        "precautions": OK,
        "history.warnings": OK if warnings_ok else FAILED,
        "history.disasters": OK if disasters_ok else FAILED,
        "history.flood_claims": OK if claims_ok else FAILED,
    }
    status["history"] = _history_status(status)
    # True only when no network source worked (e.g. offline); get_places then keeps the old version.
    all_failed = not any(
        [point_ok, spots_ok, gauges_ok, shelters_ok, claims_ok, warnings_ok, disasters_ok, office_ok]
    )

    point = point or {}
    # Fixed national help lines, plus the local NWS office when it was found.
    help_lines = [dict(h) for h in config.FIXED_HELP]
    if office_ok:
        help_lines.append(office)

    return {
        "zip": loc.zip,
        # One version per ZIP per UTC day.
        "version": today,
        "built_at": now.isoformat(timespec="seconds"),
        "stale": False,
        "all_sources_failed": all_failed,
        "status": status,
        "area": {
            "zip": loc.zip,
            "place": loc.place,
            "lat": loc.lat,
            "lon": loc.lon,
            "county_ugc": point.get("county_ugc"),
            "zone": point.get("zone"),
            "wfo": point.get("wfo"),
        },
        "waterways": waterways_from(gauges),
        "gauges": gauges,
        "flood_spots": spots,
        "history": {
            "warnings": warnings or {},
            "disasters": disasters or {},
            "flood_claims": claims or {},
        },
        "shelters": shelters or [],
        "help": help_lines,
        "precautions": copy.deepcopy(PRECAUTIONS),
    }


def _history_status(status: dict) -> str:
    """Overall history status from its three parts: ok if all ok, failed if all failed,

    carried if some were carried and none failed, otherwise partial.
    """
    subs = [status.get(f"history.{p}") for p in HISTORY_PARTS]
    if all(s == OK for s in subs):
        return OK
    if all(s == FAILED for s in subs):
        return FAILED
    if FAILED not in subs and CARRIED in subs:
        return CARRIED
    return PARTIAL


def carry_failed_sections(built: dict, previous: dict) -> None:
    """Fill each failed section of ``built`` from ``previous`` (status "carried"), in place.

    A section is carried only when the previous record had it (not failed there too).
    """
    st, prev_st = built["status"], previous.get("status") or {}

    def usable(key):
        """True when ``key`` failed in this build but has data in the previous one."""
        return st.get(key) == FAILED and prev_st.get(key) not in (None, FAILED)

    # Gauges and waterways come from the same source, so they are carried together.
    if usable("gauges"):
        built["gauges"] = copy.deepcopy(previous["gauges"])
        built["waterways"] = copy.deepcopy(previous["waterways"])
        st["gauges"] = st["waterways"] = CARRIED
    if usable("flood_spots"):
        built["flood_spots"] = copy.deepcopy(previous["flood_spots"])
        st["flood_spots"] = CARRIED
    if usable("shelters"):
        built["shelters"] = copy.deepcopy(previous["shelters"])
        st["shelters"] = CARRIED
    for part in HISTORY_PARTS:
        if usable(f"history.{part}"):
            built["history"][part] = copy.deepcopy((previous.get("history") or {}).get(part) or {})
            st[f"history.{part}"] = CARRIED
    st["history"] = _history_status(st)
    link_spots(built["flood_spots"], built["gauges"])  # spots and gauges may now come from different builds


def fallback_record(loc: Location, now: datetime | None = None) -> dict:
    """The record used when nothing is stored and nothing can be fetched or validated.

    Every section is empty and failed, except area (the ZIP's location), help (the
    fixed national lines) and precautions, so the page still has something useful.
    """
    now = now or _utc_now()
    status = {s: FAILED for s in SECTIONS}
    status.update({"area": PARTIAL, "help": PARTIAL, "precautions": OK})
    status.update({f"history.{p}": FAILED for p in HISTORY_PARTS})
    return {
        "zip": loc.zip,
        "version": now.date().isoformat(),
        "built_at": now.isoformat(timespec="seconds"),
        "stale": False,
        "all_sources_failed": True,
        "status": status,
        "area": {"zip": loc.zip, "place": loc.place, "lat": loc.lat, "lon": loc.lon,
                 "county_ugc": None, "zone": None, "wfo": None},
        "waterways": [],
        "gauges": [],
        "flood_spots": [],
        "history": {p: {} for p in HISTORY_PARTS},
        "shelters": [],
        "help": [dict(h) for h in config.FIXED_HELP],
        "precautions": copy.deepcopy(PRECAUTIONS),
    }


# --- validation --------------------------------------------------------------

def validate_places(record: dict) -> None:
    """Raise PlacesError unless ``record`` has the agreed shape (see the module docstring)."""

    def fail(msg):
        """Raise PlacesError with ``msg``."""
        raise PlacesError(msg)

    if not isinstance(record, dict):
        fail("record is not an object")
    # Required keys, ZIP and version format, and known status values.
    for key in ("zip", "version", "built_at", "status", *SECTIONS):
        if key not in record:
            fail(f"missing {key!r}")
    if not re.fullmatch(r"\d{5}", str(record["zip"])):
        fail("zip is not 5 digits")
    try:
        date.fromisoformat(str(record["version"]))
    except ValueError:
        fail("version is not YYYY-MM-DD")
    # fromisoformat checks it is a real date; the regex rejects other ISO forms that
    # Python 3.11+ also accepts (e.g. 20261025), so versions always sort as strings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record["version"])):
        fail("version is not YYYY-MM-DD")
    if not isinstance(record["status"], dict):
        fail("status must be an object")
    for key, value in record["status"].items():
        if value not in STATUS_VALUES:
            fail(f"status {key!r} is {value!r}, not one of {STATUS_VALUES}")

    # Area must be for the same ZIP and have a location.
    area = record["area"]
    if not isinstance(area, dict) or area.get("zip") != record["zip"]:
        fail("area must be an object for the same zip")
    if not (_is_num(area.get("lat")) and _is_num(area.get("lon"))):
        fail("area needs numeric lat/lon")

    # Section types.
    for name in ("waterways", "gauges", "flood_spots", "shelters", "help"):
        if not isinstance(record[name], list):
            fail(f"{name} must be a list")
    if not isinstance(record["history"], dict) or not isinstance(record["precautions"], dict):
        fail("history and precautions must be objects")
    if not record["precautions"]:
        fail("precautions are empty")

    # Every gauge names a listed waterway, has a location, and only known numeric stages.
    waterway_names = {w.get("name") for w in record["waterways"] if isinstance(w, dict)}
    gauges = record["gauges"]
    for g in gauges:
        if not isinstance(g, dict) or not g.get("lid"):
            fail("gauge without lid")
        if not isinstance(g.get("waterway"), str) or not g["waterway"].strip():
            fail(f"gauge {g.get('lid')} names no waterway")
        if g["waterway"] not in waterway_names:
            fail(f"gauge {g['lid']} waterway {g['waterway']!r} is not in waterways")
        if not (_is_num(g.get("lat")) and _is_num(g.get("lon"))):
            fail(f"gauge {g['lid']} needs numeric lat/lon")
        stages = g.get("stages")
        if not isinstance(stages, dict):
            fail(f"gauge {g['lid']} stages must be an object")
        for k, v in stages.items():
            if k not in STAGE_NAMES or not _is_num(v):
                fail(f"gauge {g['lid']} stage {k!r} is not numeric")

    # Every flood spot's waterway must be that of its nearest gauge within SPOT_GAUGE_LINK_MI (None if there is none).
    for s in record["flood_spots"]:
        if not isinstance(s, dict) or not s.get("id"):
            fail("flood spot without id")
        if not (_is_num(s.get("lat")) and _is_num(s.get("lon"))):
            fail(f"flood spot {s['id']} needs numeric lat/lon")
        nearest = nearest_gauge(s["lat"], s["lon"], gauges, config.SPOT_GAUGE_LINK_MI)
        if nearest is None:
            if s.get("waterway") is not None:
                fail(f"flood spot {s['id']} names a waterway but no gauge is within {config.SPOT_GAUGE_LINK_MI} mi")
        elif s.get("waterway") != nearest["waterway"]:
            fail(f"flood spot {s['id']} must name the waterway of its nearest gauge ({nearest['waterway']})")

    # 911 and 211 must always be listed.
    numbers = {str(h.get("number")) for h in record["help"] if isinstance(h, dict)}
    for required in ("911", "211"):
        if required not in numbers:
            fail(f"help is missing {required}")


# --- storage -----------------------------------------------------------------

def local_path(zip_code: str) -> Path:
    """Path of the local fallback copy for ``zip_code``."""
    return config.places_cache_dir() / f"{zip_code}.json"


def write_local(record: dict) -> None:
    """Save ``record`` as the local copy, atomically.


    It is written to a temp file in the same folder and then renamed over the old copy,
    so a crash never leaves a half-written file. Raises OSError on failure (the temp
    file is removed).
    """
    path = local_path(record["zip"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_future(record: dict, now: datetime | None = None) -> bool:
    """True when the record's version date is after today (UTC).


    Such a record (e.g. from a wrong clock) is ignored: it would never look old enough to rebuild.
    """
    return date.fromisoformat(record["version"]) > (now or _utc_now()).date()


def read_local(zip_code: str, now: datetime | None = None) -> dict | None:
    """The local copy for ``zip_code``, or None when it is missing, invalid or dated in the future.


    Problems are logged, never raised.
    """
    try:
        with open(local_path(zip_code), encoding="utf-8") as f:
            record = json.load(f)
        validate_places(record)
        if _is_future(record, now):
            log.warning("Local places copy for %s is dated in the future (%s); ignored", zip_code, record["version"])
            return None
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # PlacesError is a ValueError
        log.warning("Unusable local places copy for %s: %s", zip_code, exc)
        return None


def _row(record: dict) -> dict:
    """``record`` as a places_versions row: key fields copied into their own columns, the whole record in ``data``."""
    return {
        "zip": record["zip"],
        "version": record["version"],
        "built_at": record["built_at"],
        "status": record["status"],
        "data": record,
    }


def _record_from_row(row: dict, now: datetime | None = None) -> dict | None:
    """The places record stored in a Supabase row, or None when it is missing, invalid or

    dated in the future (logged, never raised).
    """
    record = row.get("data") if isinstance(row, dict) else None
    if not isinstance(record, dict):
        return None
    record = dict(record)
    # Work on a copy; the version column (the primary key) is the source of truth.
    record["version"] = str(row.get("version") or record.get("version"))
    try:
        validate_places(record)
    except PlacesError as exc:
        log.warning("Stored places version for %s is invalid: %s", row.get("zip"), exc)
        return None
    if _is_future(record, now):
        log.warning("Stored places version for %s is dated in the future (%s); ignored", row.get("zip"), record["version"])
        return None
    return record


def age_days(record: dict, now: datetime | None = None) -> int:
    """Whole days between the record's version date and today (UTC)."""
    today = (now or _utc_now()).date()
    return (today - date.fromisoformat(record["version"])).days


def is_fresh(record: dict | None, now: datetime | None = None) -> bool:
    """Less than PLACES_MAX_AGE_DAYS old and not from the future.

    A record with a failed or carried section is fresh only until
    PLACES_RETRY_AFTER_MINUTES after its ``built_at``, then it is retried.
    """
    if record is None:
        return False
    now = now or _utc_now()
    # Too old, or dated in the future (negative age).
    if not 0 <= age_days(record, now) < config.PLACES_MAX_AGE_DAYS:
        return False
    # A record with a failed or carried section is fresh only for a short time after it was
    # built, so a source that was down gets retried soon instead of in a week.
    if any(v in RETRY_STATUSES for v in (record.get("status") or {}).values()):
        try:
            built = datetime.fromisoformat(str(record.get("built_at")))
        except ValueError:
            return False
        # Treat a built_at without a time zone as UTC.
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return now - built < timedelta(minutes=config.PLACES_RETRY_AFTER_MINUTES)
    return True


# --- public entry point ----------------------------------------------------------

def get_places(loc: Location, force: bool = False, now: datetime | None = None) -> dict:
    """The latest places record for ``loc``, rebuilt when it isn't fresh (or ``force``).

    Supabase is the store and ``data/cache/places/{zip}.json`` the fallback copy; the
    newer of the two (by version) wins, and each is brought up to date from the other.

    Source, Supabase and disk failures are handled, not raised. When the rebuild fails, the previous version is returned with
    ``stale`` True; with no previous version, :func:`fallback_record` is returned.
    """
    now = now or _utc_now()
    # 1. Read the newest Supabase version. If Supabase is down, unconfigured or refuses,
    # remember that (so we don't try to save there) and rely on the local copy.
    supabase_ok = True
    stored = None
    try:
        row = supabase_rest.latest_version(loc.zip)
        stored = _record_from_row(row, now) if row else None
    except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
        supabase_ok = False
        # Stay quiet when Supabase simply isn't configured; supabase_rest already warned once.
        if not isinstance(exc, supabase_rest.SupabaseUnavailable) or config.supabase_settings() is not None:
            log.warning("Supabase unavailable for %s (%s); using the local places copy", loc.zip, exc)
    local = read_local(loc.zip, now)

    # 2. Pick the newer of the two. Versions are YYYY-MM-DD strings, which compare like dates.
    # Copy the newer one into the other store so both end up the same.
    latest = stored
    if local is not None and (stored is None or local["version"] > stored["version"]):
        latest = local
        if supabase_ok:  # e.g. built locally during an outage: upload it now
            _upsert(local)
    elif stored is not None and (local is None or local["version"] < stored["version"]):
        _save_local(stored)

    # 3. Fresh enough: return it without calling any source.
    if latest is not None and not force and is_fresh(latest, now):
        latest["stale"] = False
        return latest

    # 4. Rebuild. If every source failed (e.g. offline), keep the old version, marked stale;
    # with nothing stored, use the fallback record (fixed help lines only).
    built = build_places(loc, now)
    if built["all_sources_failed"]:
        if latest is not None:
            log.warning("Every places source failed for %s; using version %s", loc.zip, latest["version"])
            latest["stale"] = True
            return latest
        log.warning("Every places source failed for %s and nothing is stored; using fixed help only", loc.zip)
        return fallback_record(loc, now)

    # 5. Fill each section that failed this time from the previous version (status carried).
    if latest is not None:
        carry_failed_sections(built, latest)

    # 6. Never save a record with the wrong shape; serve the old version (or the fallback) instead.
    try:
        validate_places(built)
    except PlacesError as exc:
        log.error("Built places record for %s is invalid (%s); not saved", loc.zip, exc)
        if latest is not None:
            latest["stale"] = True
            return latest
        return fallback_record(loc, now)

    # 7. Save to both stores. The upsert on (zip, version) replaces today's row if this ZIP
    # was already built today (the app's anon key cannot delete rows, only insert or update).
    if supabase_ok:
        _upsert(built)
    _save_local(built)
    return built


def _upsert(record: dict) -> None:
    """Save ``record`` to Supabase. Failures are logged, never raised: the local copy still works."""
    try:
        supabase_rest.upsert_version(_row(record))
        log.info("Saved places version %s for %s to Supabase", record["version"], record["zip"])
    except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
        log.warning("Could not save places version for %s to Supabase: %s", record.get("zip"), exc)


def _save_local(record: dict) -> None:
    """Write the local copy. An OSError (e.g. read-only disk) is logged, never raised."""
    try:
        write_local(record)
    except OSError as exc:
        log.warning("Could not write local places copy for %s: %s", record.get("zip"), exc)


def start_prebuild(zip_code: str = config.DEFAULT_ZIP) -> threading.Thread | None:
    """Build or refresh one ZIP's places data in a background thread (app startup).

    Never raises; set FLOODSENSE_PREBUILD=0 to disable (the test suite does).
    """
    if os.environ.get("FLOODSENSE_PREBUILD", "1") == "0":
        return None

    def work():
        """Build or refresh the record; any error is logged."""
        try:
            record = get_places(resolve_zip(zip_code))
            log.info("Prebuilt places for %s (version %s)", zip_code, record.get("version"))
        except Exception:  # noqa: BLE001 - a failed prebuild must never affect the page
            log.exception("Places prebuild for %s failed", zip_code)

    # A daemon thread, so it never keeps the app from shutting down.
    t = threading.Thread(target=work, name=f"places-prebuild-{zip_code}", daemon=True)
    t.start()
    return t


def load_ida_archive(path: Path | None = None) -> dict:
    """The committed Sept 1-3, 2021 (Ida) USGS gauge-height archive.

    Raises OSError when the file is missing and ValueError when it is not valid JSON.
    """
    with open(path or IDA_ARCHIVE, encoding="utf-8") as f:
        return json.load(f)


# --- CLI -----------------------------------------------------------------------

def _summary(record: dict) -> dict:
    """Short CLI view of a record: statuses and the number of items in each section."""
    return {
        "zip": record["zip"],
        "version": record["version"],
        "stale": record.get("stale"),
        "status": record["status"],
        "counts": {
            "waterways": len(record["waterways"]),
            "gauges": len(record["gauges"]),
            "flood_spots": len(record["flood_spots"]),
            "shelters": len(record["shelters"]),
            "help": len(record["help"]),
            "warnings": (record["history"].get("warnings") or {}).get("count"),
            "disasters": (record["history"].get("disasters") or {}).get("count"),
            "flood_claims": (record["history"].get("flood_claims") or {}).get("count"),
        },
        "local_copy": str(local_path(record["zip"])),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: load or build the places record for one ZIP and print a JSON summary. Returns 0."""
    parser = argparse.ArgumentParser(prog="python -m data.places", description=__doc__.splitlines()[0])
    parser.add_argument("zip")
    parser.add_argument("--force", action="store_true", help="rebuild even if the latest version is fresh")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    started = _utc_now()
    record = get_places(resolve_zip(args.zip), force=args.force)
    summary = _summary(record)
    summary["seconds"] = round((_utc_now() - started).total_seconds(), 1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
