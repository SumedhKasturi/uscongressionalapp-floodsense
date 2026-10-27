"""Shared fixtures. All HTTP is mocked with ``responses``; nothing hits the network."""

from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import os
import tempfile

import pytest
import responses

# Tests write app logs to a throwaway folder, never the real uscongressionalapp-floodsense/log/.
os.environ["FLOODSENSE_LOG_DIR"] = tempfile.mkdtemp(prefix="floodsense-test-logs-")  # override Docker's /app/log
# The app's background places prebuild would outlive each test's HTTP mocks; tests call it directly.
os.environ["FLOODSENSE_PREBUILD"] = "0"

import config  # noqa: E402  (must import after FLOODSENSE_LOG_DIR is set)
from data import location

FIXTURES = Path(__file__).parent / "fixtures"
TEST_UA = "FloodSense-Test (test@example.com)"
REAL_FROM_ST_SECRETS = config._from_st_secrets  # captured before the autouse patch


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True, scope="session")
def offline_pgeocode(tmp_path_factory):
    """Point pgeocode at a trimmed copy of its real US data so lookups never download."""
    import pgeocode

    data_dir = tmp_path_factory.mktemp("pgeocode")
    shutil.copy(FIXTURES / "pgeocode_US.txt", data_dir / "US.txt")
    original = pgeocode.STORAGE_DIR
    pgeocode.STORAGE_DIR = str(data_dir)
    location._nominatim.cache_clear()
    yield data_dir
    pgeocode.STORAGE_DIR = original
    location._nominatim.cache_clear()


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    """Isolate the JSON cache in tmp_path so tests never touch the real cache."""
    d = tmp_path / "cache"
    monkeypatch.setattr(config, "CACHE_DIR", d)
    return d


@pytest.fixture(autouse=True)
def user_agent(monkeypatch):
    """Ignore any real secrets.toml and use a known User-Agent from the environment."""
    monkeypatch.setattr(config, "_from_st_secrets", lambda name: None)
    monkeypatch.setenv("NWS_USER_AGENT", TEST_UA)
    return TEST_UA


@pytest.fixture(autouse=True)
def no_supabase_env(monkeypatch):
    """Tests never see a real Supabase project; the ``supabase`` fixture configures a fake one."""
    for name in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_KEY"):
        monkeypatch.delenv(name, raising=False)
    from data import supabase_rest

    monkeypatch.setattr(supabase_rest, "_warned_unconfigured", False)


@pytest.fixture(autouse=True)
def clear_streamlit_cache():
    import streamlit as st

    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as r:
        yield r


def usgs_callback(iv: dict | None = None, empty_sites: tuple[str, ...] = ()):
    """Serve the real trimmed USGS iv fixture, filtered to the requested ``sites``."""
    iv = iv if iv is not None else load_fixture("usgs_iv.json")

    def cb(request):
        sites = parse_qs(urlparse(request.url).query).get("sites", [""])[0].split(",")
        body = copy.deepcopy(iv)
        series = [
            ts for ts in body["value"]["timeSeries"]
            if ts["sourceInfo"]["siteCode"][0]["value"] in sites
        ]
        for ts in series:
            if ts["sourceInfo"]["siteCode"][0]["value"] in empty_sites:
                ts["values"][0]["value"] = []
        body["value"]["timeSeries"] = series
        return (200, {}, json.dumps(body))

    return cb


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def shift_hourly(hourly: dict, now: datetime | None = None) -> dict:
    """Re-time the real hourly fixture so its first period is the current hour."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    body = copy.deepcopy(hourly)
    for i, p in enumerate(body["properties"]["periods"]):
        p["startTime"] = _iso(start + timedelta(hours=i))
        p["endTime"] = _iso(start + timedelta(hours=i + 1))
    return body


def shift_alerts(alerts: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    body = copy.deepcopy(alerts)
    for f in body.get("features", []):
        f["properties"]["expires"] = _iso(now + timedelta(days=1))
    return body


def shift_usgs(iv: dict, now: datetime | None = None) -> dict:
    """Make the real gauge readings recent (15 min ago) so they count as live."""
    now = now or datetime.now(timezone.utc)
    body = copy.deepcopy(iv)
    for ts in body["value"]["timeSeries"]:
        for block in ts["values"]:
            n = len(block["value"])
            for i, v in enumerate(block["value"]):
                v["dateTime"] = _iso(now - timedelta(minutes=15 * (n - i)))
    return body


def mock_nws(rsps, loc, *, alerts="nws_alerts_empty.json", hourly_edit=None, alerts_edit=None):
    point = load_fixture(f"nws_point_{loc.zip}.json")
    props = point["properties"]
    hourly_name = f"nws_hourly_{loc.zip}.json"
    if not (FIXTURES / hourly_name).exists():
        hourly_name = "nws_hourly_08807.json"
    hourly = shift_hourly(load_fixture(hourly_name))
    if hourly_edit:
        hourly_edit(hourly)
    alert_body = shift_alerts(load_fixture(alerts))
    if alerts_edit:
        alerts_edit(alert_body)
    coord = f"{loc.lat:.4f},{loc.lon:.4f}"
    rsps.get(f"{config.NWS_BASE}/points/{coord}", json=point)
    rsps.get(props["forecastHourly"], json=hourly)
    rsps.get(props["forecastGridData"], json=load_fixture("nws_gridpoint_08807.json"))
    rsps.get(f"{config.NWS_BASE}/alerts/active?point={coord}", json=alert_body)
    return point


def shift_nwps(body: dict, now: datetime | None = None) -> dict:
    """Make the real NWPS observations recent (15 min ago) so they count as live."""
    now = now or datetime.now(timezone.utc)
    body = copy.deepcopy(body)
    for g in body["gauges"]:
        obs = g["status"]["observed"]
        if obs["primary"] != -999:
            obs["validTime"] = _iso(now - timedelta(minutes=15))
    return body


def near_callback(zip_code: str, *, shift: bool = True):
    """Serve the trimmed real NWPS bbox fixture for ``zip_code``."""
    body = load_fixture(f"nwps_near_{zip_code}.json")
    if shift:
        body = shift_nwps(body)

    def cb(request):
        return (200, {}, json.dumps(body))

    return cb


NWPS_GAUGES_URL = f"{config.NWPS_BASE}/gauges"


def mock_gauges(rsps, zip_code: str = "08807"):
    """Near-ZIP gauge mock (NOAA NWPS bbox search)."""
    rsps.add_callback(responses.GET, NWPS_GAUGES_URL, callback=near_callback(zip_code), content_type="application/json")


def mock_usgs(rsps, *, empty_sites=()):
    """USGS ``sites=`` mock for usgs_levels (fixed site list)."""
    rsps.add_callback(
        responses.GET,
        config.USGS_IV_URL,
        callback=usgs_callback(iv=shift_usgs(load_fixture("usgs_iv.json")), empty_sites=empty_sites),
        content_type="application/json",
    )


def mock_live(rsps, loc, *, alerts="nws_alerts_empty.json", hourly_edit=None, alerts_edit=None):
    """Register NWS + NWPS gauge mocks for a resolved Location. Returns the point fixture."""
    point = mock_nws(rsps, loc, alerts=alerts, hourly_edit=hourly_edit, alerts_edit=alerts_edit)
    mock_gauges(rsps, loc.zip)
    return point


# --- Story 2: places sources and a fake Supabase ---------------------------------

PLACES_FIXTURES = FIXTURES / "places"
# Fixed clock for places tests, so no test depends on the wall clock.
PLACES_NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
PLACES_TODAY = "2026-10-25"
SUPABASE_TEST_URL = "https://test-project.supabase.co"
SUPABASE_TEST_KEY = "test-anon-key"
SUPABASE_TEST_SERVICE_KEY = "test-service-key"


def places_fixture(name: str):
    return json.loads((PLACES_FIXTURES / name).read_text())


def places_urls():
    return {
        "lsr": f"{config.IEM_BASE}/api/1/nws/lsrs_by_point.json",
        "vtec": f"{config.IEM_BASE}/json/vtec_events_byugc.py",
        "disasters": f"{config.OPENFEMA_BASE}/v2/DisasterDeclarationsSummaries",
        "nfip": f"{config.OPENFEMA_BASE}/v3/NfipClaims",
        "shelters": config.FEMA_SHELTERS_URL,
        "office": f"{config.NWS_BASE}/offices/PHI",
        "gauge": f"{config.NWPS_BASE}/gauges/",
    }


def mock_places_sources(rsps, loc, *, fail=(), shelters=None):
    """Register the recorded 08807 responses for every places source.

    ``fail`` names sources (keys of ``places_urls()``, plus "point" and "near") that
    time out instead. ``shelters`` replaces the (empty) FEMA shelters body.
    """
    urls = places_urls()
    timeout = requests_timeout()

    def add(name, url, body=None, callback=None):
        if name in fail:
            rsps.get(url, body=timeout)
        elif callback is not None:
            rsps.add_callback(responses.GET, url, callback=callback, content_type="application/json")
        else:
            rsps.get(url, json=body)

    coord = f"{loc.lat:.4f},{loc.lon:.4f}"
    add("point", f"{config.NWS_BASE}/points/{coord}", load_fixture(f"nws_point_{loc.zip}.json"))
    add("near", NWPS_GAUGES_URL, load_fixture(f"nwps_near_{loc.zip}.json"))
    for g in load_fixture(f"nwps_near_{loc.zip}.json")["gauges"]:
        add("gauge", urls["gauge"] + g["lid"], places_fixture(f"nwps_gauge_{g['lid']}.json"))
    add("lsr", urls["lsr"], places_fixture("iem_lsr_08807.json"))
    add("vtec", urls["vtec"], places_fixture("iem_vtec_NJC035.json"))
    add("disasters", urls["disasters"], places_fixture("openfema_disasters_34035.json"))
    add("nfip", urls["nfip"], places_fixture("openfema_nfip_08807.json"))
    add("shelters", urls["shelters"], shelters or places_fixture("fema_shelters_empty_08807.json"))
    add("office", urls["office"], places_fixture("nws_office_PHI.json"))


PLACES_SOURCES = ("point", "near", "gauge", "lsr", "vtec", "disasters", "nfip", "shelters", "office")


def requests_timeout():
    import requests

    return requests.exceptions.Timeout("timed out")


def is_source_call(call) -> bool:
    return not call.request.url.startswith(SUPABASE_TEST_URL)


class FakeSupabase:
    """A stateful stand-in for PostgREST ``places_versions`` (GET latest, POST upsert, RPC)."""

    def __init__(self, rsps):
        self.rows: dict[tuple[str, str], dict] = {}
        self.down = False
        self.fail_status: dict[str, int] = {}  # e.g. {"GET": 404}: that method answers with that status
        self.requests = []
        self.register(rsps)

    def register(self, rsps):
        """(Re-)register the PostgREST endpoints, e.g. after ``rsps.reset()``."""
        base = f"{SUPABASE_TEST_URL}/rest/v1"
        rsps.add_callback(responses.GET, f"{base}/places_versions", callback=self._get)
        rsps.add_callback(responses.POST, f"{base}/places_versions", callback=self._post)

    def add(self, record: dict):
        self.rows[(record["zip"], record["version"])] = {
            "zip": record["zip"], "version": record["version"], "built_at": record["built_at"],
            "status": record["status"], "data": record,
        }

    def versions(self, zip_code: str) -> list[str]:
        return sorted(v for z, v in self.rows if z == zip_code)

    def _get(self, request):
        self.requests.append(request)
        if "GET" in self.fail_status:
            return (self.fail_status["GET"], {}, '{"message": "not found"}')
        if self.down:
            return (503, {}, "down")
        q = parse_qs(urlparse(request.url).query)
        zip_code = q["zip"][0].removeprefix("eq.")
        assert q["order"] == ["version.desc"] and q["limit"] == ["1"]
        rows = sorted((r for (z, _), r in self.rows.items() if z == zip_code), key=lambda r: r["version"], reverse=True)
        return (200, {}, json.dumps(rows[:1]))

    def _post(self, request):
        self.requests.append(request)
        if "POST" in self.fail_status:
            return (self.fail_status["POST"], {}, '{"message": "save failed"}')
        if self.down:
            return (503, {}, "down")
        q = parse_qs(urlparse(request.url).query)
        assert q["on_conflict"] == ["zip,version"]
        assert "resolution=merge-duplicates" in request.headers["Prefer"]
        row = json.loads(request.body)
        self.rows[(row["zip"], row["version"])] = row
        return (201, {}, "")


@pytest.fixture
def supabase(rsps, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_KEY", SUPABASE_TEST_KEY)
    return FakeSupabase(rsps)
