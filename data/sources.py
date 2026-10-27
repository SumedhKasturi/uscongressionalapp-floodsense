"""Live NWS and USGS fetchers. Every call goes through ``data.cache.get_json``."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import config
from data.cache import CacheResult, get_json
from data.location import Location

NO_READING = "no reading"
_USGS_NO_DATA = -999999.0
OLD_READING_AFTER = timedelta(hours=2)


def parse_time(value) -> datetime | None:
    """Parse an ISO-8601 string to an aware datetime (naive values are taken as UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _nws_headers() -> dict:
    return {"User-Agent": config.nws_user_agent(), "Accept": "application/geo+json"}


def _coord(lat: float, lon: float) -> str:
    # NWS accepts at most 4 decimal places.
    return f"{lat:.4f},{lon:.4f}"


def _point_property(point: CacheResult | None, name: str) -> str | None:
    if point is None:
        return None
    try:
        value = point.data["properties"][name]
    except (KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


# --- NWS -------------------------------------------------------------------

def nws_point(loc: Location) -> CacheResult | None:
    url = f"{config.NWS_BASE}/points/{_coord(loc.lat, loc.lon)}"
    return get_json(f"nws_point_{loc.zip}", url, _nws_headers())


def nws_hourly(loc: Location, point: CacheResult | None = None) -> CacheResult | None:
    """Hourly forecast, using ``forecastHourly`` from the point metadata."""
    if point is None:
        point = nws_point(loc)
    url = _point_property(point, "forecastHourly")
    if url is None:
        return None
    return get_json(f"nws_hourly_{loc.zip}", url, _nws_headers())


def nws_gridpoint_qpf(loc: Location, point: CacheResult | None = None) -> CacheResult | None:
    """Raw gridpoint data (cached whole) from ``forecastGridData``.

    Use ``qpf_values`` to read the ``quantitativePrecipitation`` series (mm).
    """
    if point is None:
        point = nws_point(loc)
    url = _point_property(point, "forecastGridData")
    if url is None:
        return None
    return get_json(f"nws_gridpoint_{loc.zip}", url, _nws_headers())


def qpf_values(grid: CacheResult | None) -> list[dict]:
    """``[{"validTime": "...", "value": mm}, ...]`` from a gridpoint result."""
    if grid is None:
        return []
    try:
        return list(grid.data["properties"]["quantitativePrecipitation"]["values"])
    except (KeyError, TypeError):
        return []


def nws_alerts(lat: float, lon: float, zip_code: str | None = None) -> CacheResult | None:
    url = f"{config.NWS_BASE}/alerts/active?point={_coord(lat, lon)}"
    key = f"nws_alerts_{zip_code}" if zip_code else f"nws_alerts_{_coord(lat, lon)}"
    return get_json(key, url, _nws_headers())


def hourly_periods(hourly: CacheResult | None, n: int = 12, now: datetime | None = None) -> list[dict]:
    """The next ``n`` periods that have not ended yet (matters for a stale cache)."""
    if hourly is None:
        return []
    try:
        periods = list(hourly.data["properties"]["periods"])
    except (KeyError, TypeError):
        return []
    now = _now(now)
    upcoming = []
    for p in periods:
        end = parse_time((p or {}).get("endTime"))
        if end is not None and end <= now:
            continue
        upcoming.append(p)
    return upcoming[:n]


def alert_summaries(alerts: CacheResult | None, now: datetime | None = None) -> list[dict]:
    """``[{"event", "headline", "severity"}, ...]`` for each alert that hasn't expired."""
    if alerts is None:
        return []
    now = _now(now)
    out = []
    for feat in (alerts.data or {}).get("features") or []:
        props = (feat or {}).get("properties") or {}
        expires = parse_time(props.get("expires"))
        if expires is not None and expires <= now:
            continue
        out.append(
            {
                "event": props.get("event") or "",
                "headline": props.get("headline") or "",
                "severity": props.get("severity") or "",
            }
        )
    return out


# --- USGS ------------------------------------------------------------------

@dataclass(frozen=True)
class GaugeLevel:
    site: str
    name: str
    height_ft: float | None
    time: str | None
    stale: bool = False
    fetched_at: str | None = None
    fetch_failed: bool = False  # fetch failed and there was no cached copy
    distance_mi: float | None = None  # from the ZIP point (gauges_near only)
    unit: str = "ft"
    flood_category: str | None = None  # NWPS: no_flooding/action/minor/moderate/major
    lat: float | None = None  # gauge location (gauges_near only)
    lon: float | None = None

    @property
    def has_reading(self) -> bool:
        return self.height_ft is not None

    def status(self, now: datetime | None = None) -> str:
        """One of: "unavailable", "no reading", "old reading", "cached", "live"."""
        if self.fetch_failed:
            return "unavailable"
        if not self.has_reading:
            return NO_READING
        when = parse_time(self.time)
        if when is None or _now(now) - when > OLD_READING_AFTER:
            return "old reading"
        return "cached" if self.stale else "live"

    @property
    def display(self) -> str:
        return f"{self.height_ft:.2f} {self.unit}".rstrip() if self.has_reading else NO_READING


def _usgs_url(site: str) -> str:
    return f"{config.USGS_IV_URL}?sites={site}&parameterCd={config.USGS_GAUGE_HEIGHT}&format=json"


def _series(data) -> list:
    try:
        return list(data["value"]["timeSeries"] or [])
    except (KeyError, TypeError):
        return []


def _latest_reading(data, site: str) -> tuple[str | None, float | None, str | None]:
    """Return (site_name, height_ft, dateTime) of the latest valid value for ``site``."""
    series = _series(data)
    name = None
    best: tuple[datetime | None, float, str | None] | None = None
    for ts in series:
        info = ts.get("sourceInfo") or {}
        codes = [c.get("value") for c in info.get("siteCode") or []]
        if codes and site not in codes:
            continue
        name = name or info.get("siteName")
        for block in ts.get("values") or []:
            for v in block.get("value") or []:
                try:
                    height = float(v.get("value"))
                except (TypeError, ValueError):
                    continue
                if height == _USGS_NO_DATA:
                    continue
                when = parse_time(v.get("dateTime"))
                # Keep the latest valid value across all series and blocks.
                if best is None or (when is not None and (best[0] is None or when >= best[0])):
                    best = (when, height, v.get("dateTime"))
    if best is None:
        return name, None, None
    return name, best[1], best[2]


def _fetch_gauge(site: str, headers: dict) -> GaugeLevel:
    result = get_json(f"usgs_iv_{site}", _usgs_url(site), headers)
    fallback_name = config.GAUGES.get(site, site)
    if result is None:
        return GaugeLevel(site=site, name=fallback_name, height_ft=None, time=None, fetch_failed=True)
    name, height, when = _latest_reading(result.data, site)
    return GaugeLevel(
        site=site,
        name=name or fallback_name,
        height_ft=height,
        time=when,
        stale=result.stale,
        fetched_at=result.fetched_at,
    )


def usgs_levels(sites: Iterable[str] | None = None) -> list[GaugeLevel]:
    """Latest gauge height for each site (one cached request per site)."""
    sites = list(config.GAUGES) if sites is None else list(sites)
    if not sites:
        return []
    headers = {"User-Agent": config.nws_user_agent()}
    with ThreadPoolExecutor(max_workers=min(6, len(sites))) as pool:
        return list(pool.map(lambda s: _fetch_gauge(s, headers), sites))


# --- Gauges nearest the ZIP (NOAA NWPS) -------------------------------------

NEAR_BOX_DEG = 0.25
WIDE_BOX_DEG = 0.5  # about 35 miles
_EARTH_RADIUS_MI = 3958.8
_NWPS_NO_DATA = -999.0
FLOOD_CATEGORIES = ("no_flooding", "action", "minor", "moderate", "major")


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def _nwps_bbox_url(loc: Location, deg: float) -> str:
    w, s, e, n = loc.lon - deg, loc.lat - deg, loc.lon + deg, loc.lat + deg
    return (
        f"{config.NWPS_BASE}/gauges?bbox.xmin={w:.4f}&bbox.ymin={s:.4f}"
        f"&bbox.xmax={e:.4f}&bbox.ymax={n:.4f}&srid=EPSG_4326"
    )


def _nwps_gauges(data) -> list:
    gauges = (data or {}).get("gauges") if isinstance(data, dict) else None
    return list(gauges or [])


def _nwps_level(g: dict, result: CacheResult, loc: Location) -> GaugeLevel | None:
    lid = g.get("lid")
    if not lid:
        return None
    obs = (g.get("status") or {}).get("observed") or {}
    try:
        height = float(obs.get("primary"))
    except (TypeError, ValueError):
        height = None
    if height is not None and height <= _NWPS_NO_DATA:
        height = None
    category = obs.get("floodCategory")
    try:
        glat, glon = float(g["latitude"]), float(g["longitude"])
        distance = haversine_mi(loc.lat, loc.lon, glat, glon)
    except (KeyError, TypeError, ValueError):
        glat = glon = distance = None
    return GaugeLevel(
        site=lid,
        name=g.get("name") or lid,
        height_ft=height,
        time=obs.get("validTime") if height is not None else None,
        stale=result.stale,
        fetched_at=result.fetched_at,
        distance_mi=distance,
        unit=(obs.get("primaryUnit") or "ft") if height is not None else "ft",
        flood_category=category if category in FLOOD_CATEGORIES and height is not None else None,
        lat=glat,
        lon=glon,
    )


def gauges_near(loc: Location, n: int = 6) -> list[GaugeLevel] | None:
    """The ``n`` NWPS river gauges nearest the ZIP point, nearest first.

    One cached bounding-box request (loc +/- 0.25 deg), widened once to +/- 0.5 deg
    when the first box has no gauges. Returns ``[]`` when none are found and
    ``None`` when the fetch failed with no cached copy.
    """
    headers = {"User-Agent": config.nws_user_agent(), "Accept": "application/json"}
    result = get_json(f"nwps_near_{loc.zip}", _nwps_bbox_url(loc, NEAR_BOX_DEG), headers)
    if result is not None and not _nwps_gauges(result.data):
        result = get_json(f"nwps_near_{loc.zip}_wide", _nwps_bbox_url(loc, WIDE_BOX_DEG), headers)
    if result is None:
        return None
    seen, levels = set(), []
    for g in _nwps_gauges(result.data):
        level = _nwps_level(g or {}, result, loc)
        if level is None or level.site in seen:
            continue
        seen.add(level.site)
        levels.append(level)
    levels.sort(key=lambda g: (g.distance_mi is None, g.distance_mi or 0.0))
    return levels[:n]


# --- Recent gauge readings (NWPS stageflow, for the 1-hour rise) -------------

# The observed series covers about 30 days at 15-minute steps and runs about an hour
# behind. The rise uses the RISE_WINDOW (an hour plus slack) back from the newest valid
# reading, and only when that reading is within OLD_READING_AFTER of now.
RISE_WINDOW = timedelta(minutes=75)
# The base reading must be at least this long before the newest, so one noisy step isn't scaled up to an hour.
RISE_MIN_SPAN = timedelta(minutes=30)


def gauge_recent(lid: str) -> CacheResult | None:
    """Observed stage series of one NWPS gauge (``data[]`` of ``validTime`` and ``primary``)."""
    headers = {"User-Agent": config.nws_user_agent(), "Accept": "application/json"}
    url = f"{config.NWPS_BASE}/gauges/{lid}/stageflow/observed"
    return get_json(f"nwps_stageflow_{lid}", url, headers)


def stageflow_series(result: CacheResult | None) -> list[dict]:
    """The ``data[]`` list of a ``gauge_recent`` result ([] when missing)."""
    if result is None or not isinstance(result.data, dict):
        return []
    data = result.data.get("data")
    return list(data) if isinstance(data, list) else []


def _valid_readings(series, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out = []
    for v in series or []:
        when = parse_time((v or {}).get("validTime"))
        try:
            height = float((v or {}).get("primary"))
        except (TypeError, ValueError):
            continue
        if when is None or height <= _NWPS_NO_DATA or not (start <= when <= end):
            continue
        out.append((when, height))
    out.sort()
    return out


def _newest(readings, now: datetime) -> tuple[datetime, float] | None:
    """The newest reading, if it is within OLD_READING_AFTER of ``now``."""
    if not readings or now - readings[-1][0] > OLD_READING_AFTER:
        return None
    return readings[-1]


def latest_recent(series, now: datetime | None = None) -> tuple[float, str] | None:
    """(height_ft, validTime) of the newest valid reading, if within OLD_READING_AFTER of now; else None."""
    now = _now(now)
    newest = _newest(_valid_readings(series, now - OLD_READING_AFTER, now), now)
    if newest is None:
        return None
    when, height = newest
    return height, when.isoformat(timespec="seconds")


def rise_ft_per_hr(series, now: datetime | None = None) -> float | None:
    """Rise rate (ft/h) over about an hour, from the 75 minutes back from the newest valid reading.

    Compares the newest reading with the one closest to an hour before it. Returns None
    when there is no valid reading within OLD_READING_AFTER of now, or no base reading
    at least 30 minutes before the newest. Falling water gives a negative rate. -999
    values are ignored.
    """
    now = _now(now)
    newest = _newest(_valid_readings(series, now - OLD_READING_AFTER, now), now)
    if newest is None:
        return None
    last_t, last_h = newest
    earlier = [r for r in _valid_readings(series, last_t - RISE_WINDOW, last_t)
               if last_t - r[0] >= RISE_MIN_SPAN]
    if not earlier:
        return None
    target = last_t - timedelta(hours=1)
    base_t, base_h = min(earlier, key=lambda r: abs((r[0] - target).total_seconds()))
    return (last_h - base_h) / ((last_t - base_t).total_seconds() / 3600)
