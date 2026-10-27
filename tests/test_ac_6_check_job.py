"""Story 6 (CAP-8), AC 6.3 to 6.5: the scheduled risk check (alerts/check.py) and the push (alerts/notify.py).

Supabase (registrations, zip_state) and ntfy are mocked with ``responses``; nothing reaches the network.
Most tests stub the per-ZIP assessment to set the level; one runs the real pipeline from fixtures.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import networkx as nx
import pytest
import requests
import responses

import config
from alerts import check, notify
from data import places
from data.location import resolve_zip
from graph import build, reason
from tests.conftest import SUPABASE_TEST_KEY, SUPABASE_TEST_SERVICE_KEY, SUPABASE_TEST_URL
from tests.test_ac_4_flood_alerts_page import set_alerts
from tests.test_ac_5_help_page import mock_zip
from tests.test_ac_6_phone_alerts_page import expected_topic

SECRET = "test-topic-secret-123"
APP_URL = "https://floodsense.example.app"
NTFY_RE = re.compile(r"^https://ntfy\.sh/.*")
REST = f"{SUPABASE_TEST_URL}/rest/v1"
PRECAUTION = "Move to higher ground immediately."


@pytest.fixture
def job_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", SUPABASE_TEST_SERVICE_KEY)
    monkeypatch.setenv("NTFY_TOPIC_SECRET", SECRET)
    monkeypatch.setenv("APP_URL", APP_URL + "/")


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """State-write retries never really wait in tests."""
    waits = []
    monkeypatch.setattr(check, "_sleep", waits.append)
    return waits


class FakeAlertsDb:
    """PostgREST ``registrations`` (GET) and ``zip_state`` (GET, POST upsert) for the job.

    ``rows`` holds each ZIP's full state row; ``states`` is just ``{zip: last_risk}``.
    """

    def __init__(self, rsps, zips=(), states=None):
        self.zips = list(zips)
        self.rows = {z: {"last_risk": lv} for z, lv in (states or {}).items()}
        self.down = False
        self.states_down = False
        self.state_post_status = None  # e.g. 503 to fail every state write
        self.fail_next_posts = 0  # fail only this many state writes, then succeed
        self.keys = []
        rsps.add_callback(responses.GET, f"{REST}/registrations", callback=self._get_regs)
        rsps.add_callback(responses.GET, f"{REST}/zip_state", callback=self._get_states)
        rsps.add_callback(responses.POST, f"{REST}/zip_state", callback=self._post_state)

    @property
    def states(self):
        return {z: r["last_risk"] for z, r in self.rows.items()}

    def _get_regs(self, request):
        self.keys.append(request.headers["apikey"])
        if self.down:
            return (503, {}, "down")
        return (200, {}, json.dumps([{"zip": z} for z in self.zips]))

    def _get_states(self, request):
        self.keys.append(request.headers["apikey"])
        if self.down or self.states_down:
            return (503, {}, "down")
        return (200, {}, json.dumps([{"zip": z, **r} for z, r in self.rows.items()]))

    def _post_state(self, request):
        self.keys.append(request.headers["apikey"])
        if self.state_post_status:
            return (self.state_post_status, {}, "fail")
        if self.fail_next_posts:
            self.fail_next_posts -= 1
            return (503, {}, "fail")
        assert parse_qs(urlparse(request.url).query)["on_conflict"] == ["zip"]
        assert "resolution=merge-duplicates" in request.headers["Prefer"]
        row = json.loads(request.body)
        # merge-duplicates updates only the columns sent.
        self.rows.setdefault(row.pop("zip"), {}).update(row)
        return (201, {}, "")


def ntfy_calls(rsps):
    return [c for c in rsps.calls if c.request.url.startswith("https://ntfy.sh/")]


def state_writes(rsps):
    return [c for c in rsps.calls if c.request.method == "POST" and "/zip_state" in c.request.url]


def fake_graph(zip_code="08807", place="Bridgewater, NJ"):
    G = nx.DiGraph()
    G.add_node("alert:0_flash_flood_warning", type="alert", label="Flash Flood Warning", event="Flash Flood Warning")
    G.add_node(f"area:{zip_code}", type="area", label=zip_code, place=place, zip=zip_code)
    G.add_edge("alert:0_flash_flood_warning", f"area:{zip_code}", rel="covers")
    return G


def fake_assessment(level, zip_code="08807"):
    reasons, precautions = [], []
    if level != "low":
        reasons = [reason.Reason(1, level, ("alert:0_flash_flood_warning", f"area:{zip_code}"),
                                 "Flash Flood Warning covers it", (f"area:{zip_code}",))]
        precautions = [{"kind": "hazard", "hazard": "flash_flood", "level": level, "text": PRECAUTION}]
    return reason.Assessment(area_level=level, reasons=reasons, precautions=precautions)


@pytest.fixture
def levels(monkeypatch):
    """Map ZIP -> level (or an Exception to raise); the job's assessment is stubbed from it."""
    table: dict = {}
    calls: list[str] = []

    def stub(zip_code, now=None):
        calls.append(zip_code)
        value = table[zip_code]
        if isinstance(value, Exception):
            raise value
        loc = resolve_zip(zip_code)
        record = {"precautions": {"flood": {"high": ["Leave low areas now."], "elevated": ["Watch the water."]}}}
        return loc, record, fake_graph(zip_code, loc.place), fake_assessment(value, zip_code)

    monkeypatch.setattr(check, "assess_zip", stub)
    table["_calls"] = calls
    return table


# --- AC 6.3: risk computed once per ZIP, compared with last_risk, push on a rise, state saved ---

def test_ac_6_3_first_run_pushes_once_and_saves_state(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = "elevated"
    rsps.post(NTFY_RE, status=200)
    assert check.run() == 0
    assert len(ntfy_calls(rsps)) == 1
    assert db.states == {"08807": "elevated"}
    assert set(db.keys) == {SUPABASE_TEST_SERVICE_KEY}  # only the service key reads/writes the job tables


def test_ac_6_3_two_registrations_one_zip_assessed_once(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807", "08807"])
    levels["08807"] = "high"
    rsps.post(NTFY_RE, status=200)
    assert check.run() == 0
    assert levels["_calls"] == ["08807"]
    assert len(ntfy_calls(rsps)) == 1
    assert db.states == {"08807": "high"}


def test_ac_6_3_registered_zips_distinct_sorted(rsps, job_env):
    from data import supabase_rest

    FakeAlertsDb(rsps, zips=["10001", "08807", "10001"])
    assert supabase_rest.registered_zips(SUPABASE_TEST_SERVICE_KEY) == ["08807", "10001"]


def test_ac_6_3_uses_the_page_pipeline(monkeypatch):
    seen = []
    loc = resolve_zip("08807")
    monkeypatch.setattr(places, "get_places", lambda l, now=None: seen.append(("get_places", l)) or {"rec": 1})
    monkeypatch.setattr(build, "live_inputs", lambda l, r, now=None: seen.append(("live_inputs", r)) or "inputs")
    monkeypatch.setattr(build, "build_graph", lambda r, i: seen.append(("build_graph", i)) or "G")
    monkeypatch.setattr(reason, "assess", lambda G, r, i: seen.append(("assess", G)) or "A")
    assert check.assess_zip("08807") == (loc, {"rec": 1}, "G", "A")
    assert [s[0] for s in seen] == ["get_places", "live_inputs", "build_graph", "assess"]


def test_ac_6_3_real_pipeline_from_fixtures(rsps, job_env):
    """A Flash Flood Warning in the fixtures makes 08807 high: one push and the state saved."""
    mock_zip(rsps, alerts="nws_alerts_one.json", alerts_edit=set_alerts(("Flash Flood Warning", "Severe")))
    db = FakeAlertsDb(rsps, zips=["08807"], states={"08807": "low"})
    rsps.post(NTFY_RE, status=200)
    assert check.run() == 0
    pushes = ntfy_calls(rsps)
    assert len(pushes) == 1
    assert pushes[0].request.headers["Title"] == "FloodSense: High flood risk in Bridgewater, NJ"
    body = pushes[0].request.body.decode("utf-8")
    assert body.startswith("Flash Flood Warning → Bridgewater, NJ")
    assert db.states == {"08807": "high"}


# --- AC 6.4: no push when risk stays or falls; one rise sends exactly one push --------------

def test_ac_6_4_unchanged_no_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"], states={"08807": "elevated"})
    levels["08807"] = "elevated"
    assert check.run() == 0
    assert not ntfy_calls(rsps)
    assert db.states == {"08807": "elevated"}


def test_ac_6_4_rise_elevated_to_high_one_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"], states={"08807": "elevated"})
    levels["08807"] = "high"
    rsps.post(NTFY_RE, status=200)
    assert check.run() == 0
    assert len(ntfy_calls(rsps)) == 1
    assert db.states == {"08807": "high"}
    # The next run at the same level sends nothing more.
    assert check.run() == 0
    assert len(ntfy_calls(rsps)) == 1


def test_ac_6_4_fall_no_push_state_recorded(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"], states={"08807": "high"})
    levels["08807"] = "low"
    assert check.run() == 0
    assert not ntfy_calls(rsps)
    assert db.states == {"08807": "low"}


def test_ac_6_4_low_with_no_state_no_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = "low"
    assert check.run() == 0
    assert not ntfy_calls(rsps)
    assert db.states == {"08807": "low"}


def test_ac_6_4_ntfy_down_state_not_updated_and_retried(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807", "10001"])
    levels.update({"08807": "high", "10001": "low"})
    rsps.post(NTFY_RE, status=502)
    assert check.run() == 0  # 10001 succeeded
    assert "08807" not in db.states and db.states == {"10001": "low"}
    # Next run: ntfy is back, and the rise is pushed then.
    rsps.replace(responses.POST, NTFY_RE, status=200)
    assert check.run() == 0
    assert db.states["08807"] == "high"
    assert len(ntfy_calls(rsps)) == 2


def test_ac_6_4_ntfy_timeout_logged(rsps, job_env, levels, caplog):
    db = FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = "high"
    rsps.post(NTFY_RE, body=requests.exceptions.ReadTimeout("slow"))
    assert check.run() == 1  # the only ZIP failed
    assert db.states == {}
    assert "Push failed for 08807" in caplog.text
    assert SECRET not in caplog.text  # the topic never reaches the log


def test_ac_6_4_one_zip_fails_others_processed(rsps, job_env, levels, caplog):
    db = FakeAlertsDb(rsps, zips=["10001", "08807"])
    levels.update({"10001": RuntimeError("assessment broke"), "08807": "elevated"})
    rsps.post(NTFY_RE, status=200)
    assert check.run() == 0
    assert db.states == {"08807": "elevated"}
    assert len(ntfy_calls(rsps)) == 1
    assert "Risk check failed for 10001" in caplog.text


def test_ac_6_4_state_write_failure_logged_not_fatal(rsps, job_env, levels, caplog):
    db = FakeAlertsDb(rsps, zips=["08807", "10001"])
    db.state_post_status = 503
    levels.update({"08807": "low", "10001": "low"})
    assert check.run() == 1  # every ZIP failed
    assert "Could not save the state for 08807" in caplog.text and "10001" in caplog.text


def test_ac_6_4_every_zip_failed_exit_1(rsps, job_env, levels):
    FakeAlertsDb(rsps, zips=["08807", "10001"])
    levels.update({"08807": RuntimeError("x"), "10001": RuntimeError("y")})
    assert check.run() == 1


@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "NTFY_TOPIC_SECRET"])
def test_ac_6_4_missing_settings_exit_1(rsps, job_env, levels, monkeypatch, missing):
    monkeypatch.delenv(missing)
    FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = "high"
    assert check.run() == 1
    assert not ntfy_calls(rsps)


def test_ac_6_4_supabase_down_exit_1_no_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    db.down = True
    levels["08807"] = "high"
    assert check.run() == 1
    assert not ntfy_calls(rsps)


def test_ac_6_4_no_registrations_exit_0(rsps, job_env, levels):
    FakeAlertsDb(rsps, zips=[])
    assert check.run() == 0
    assert not ntfy_calls(rsps)


def test_ac_6_4_forced_push_leaves_state(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"], states={"08807": "high"})
    levels["08807"] = "low"
    rsps.post(NTFY_RE, status=200)
    assert check.main(["--zip", "08807", "--force-level", "high"]) == 0
    assert len(ntfy_calls(rsps)) == 1
    req = ntfy_calls(rsps)[0].request
    assert req.headers["Priority"] == "urgent"
    assert req.headers["Title"] == "TEST: FloodSense: High flood risk in Bridgewater, NJ"
    assert req.body.decode("utf-8").endswith(" (This is a test alert.)")
    assert not state_writes(rsps) and db.states == {"08807": "high"}


def test_ac_6_4_forced_push_without_zip_refused(rsps, job_env, levels):
    FakeAlertsDb(rsps, zips=["08807", "10001"])
    levels.update({"08807": "low", "10001": "low"})
    assert check.run(force_level="high") == 1
    assert check.main(["--force-level", "high"]) == 1
    assert not ntfy_calls(rsps) and not levels["_calls"]


def test_ac_6_4_state_read_down_exit_1_no_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    db.states_down = True
    levels["08807"] = "high"
    assert check.run() == 1  # registrations read fine
    assert check.run(zips=["08807"]) == 1  # --zip given
    assert not ntfy_calls(rsps) and not state_writes(rsps)


def test_ac_6_4_state_write_retried_after_push_no_double_push(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    db.fail_next_posts = 2
    levels["08807"] = "high"
    rsps.post(NTFY_RE, status=200)
    sleeps = []
    assert check.run(sleep=sleeps.append) == 0
    assert len(state_writes(rsps)) == 3 and len(sleeps) == 2  # two failures, then the write lands
    assert db.states == {"08807": "high"} and db.rows["08807"]["last_pushed_level"] == "high"
    assert check.run(sleep=sleeps.append) == 0  # state now matches: no second push
    assert len(ntfy_calls(rsps)) == 1


def test_ac_6_4_state_write_all_attempts_fail_logged_loudly(rsps, job_env, levels, caplog):
    db = FakeAlertsDb(rsps, zips=["08807"])
    db.state_post_status = 503
    levels["08807"] = "high"
    rsps.post(NTFY_RE, status=200)
    sleeps = []
    assert check.run(sleep=sleeps.append) == 1
    assert len(state_writes(rsps)) == check.STATE_WRITE_ATTEMPTS and len(sleeps) == check.STATE_WRITE_ATTEMPTS - 1
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("Could not save the state for 08807 after 3 attempts" in r.getMessage() for r in errors)


def test_ac_6_4_github_warning_per_failed_zip(rsps, job_env, levels, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    FakeAlertsDb(rsps, zips=["08807", "10001"])
    levels.update({"08807": RuntimeError("x"), "10001": "low"})
    assert check.run() == 0
    out = capsys.readouterr().out
    assert "::warning::Flood risk check failed for ZIP 08807" in out and "ZIP 10001" not in out


def test_ac_6_4_no_github_warning_outside_actions(rsps, job_env, levels, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = RuntimeError("x")
    check.run()
    assert "::warning::" not in capsys.readouterr().out


def test_ac_6_4_message_failure_does_not_abort_run(rsps, job_env, levels, monkeypatch):
    db = FakeAlertsDb(rsps, zips=["08807", "10001"])
    levels.update({"08807": "high", "10001": "low"})
    real = notify.build_message

    def flaky(zip_code, *a, **kw):
        if zip_code == "08807":
            raise KeyError("broken")
        return real(zip_code, *a, **kw)

    monkeypatch.setattr(notify, "build_message", flaky)
    assert check.run() == 0
    assert db.states == {"10001": "low"} and not ntfy_calls(rsps)


# --- Cooldown: the same level is not pushed again within 6 hours -------------------------------

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def _run_at(levels, level, hours):
    levels["08807"] = level
    return check.run(now=T0 + timedelta(hours=hours))


def test_ac_6_4_cooldown_flip_within_6h_pushes_once(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    rsps.post(NTFY_RE, status=200)
    for level, hours in (("elevated", 0), ("low", 1), ("elevated", 2), ("low", 3), ("elevated", 5.5)):
        assert _run_at(levels, level, hours) == 0
    assert len(ntfy_calls(rsps)) == 1
    assert db.states == {"08807": "elevated"}  # the state still follows every change


def test_ac_6_4_cooldown_higher_level_pushes_right_away(rsps, job_env, levels):
    FakeAlertsDb(rsps, zips=["08807"])
    rsps.post(NTFY_RE, status=200)
    assert _run_at(levels, "elevated", 0) == 0
    assert _run_at(levels, "high", 0.5) == 0
    assert len(ntfy_calls(rsps)) == 2


def test_ac_6_4_cooldown_same_level_after_6h_pushes_again(rsps, job_env, levels):
    db = FakeAlertsDb(rsps, zips=["08807"])
    rsps.post(NTFY_RE, status=200)
    assert _run_at(levels, "elevated", 0) == 0
    assert _run_at(levels, "low", 1) == 0
    assert _run_at(levels, "elevated", 6) == 0
    assert len(ntfy_calls(rsps)) == 2
    assert db.rows["08807"]["last_pushed_at"].startswith("2026-10-25T18:00")


def test_ac_6_4_schema_has_cooldown_columns():
    sql = (config.APP_DIR / "supabase" / "schema.sql").read_text()
    assert "alter table public.zip_state add column if not exists last_pushed_level text" in sql
    assert "alter table public.zip_state add column if not exists last_pushed_at timestamptz" in sql
    assert config.COOLDOWN_HOURS == 6


def test_ac_6_4_dry_run_prints_without_sending(rsps, job_env, levels, capsys):
    db = FakeAlertsDb(rsps, zips=["08807"])
    levels["08807"] = "elevated"
    assert check.main(["--zip", "08807", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "08807: level elevated (last low); would push" in out
    assert "FloodSense: Elevated flood risk in Bridgewater, NJ" in out
    assert not ntfy_calls(rsps) and not state_writes(rsps) and db.states == {}


def test_ac_6_4_dry_run_forced_without_service_key(rsps, job_env, levels, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    levels["08807"] = "low"
    assert check.main(["--zip", "08807", "--force-level", "high", "--dry-run"]) == 0
    assert "FloodSense: High flood risk" in capsys.readouterr().out
    assert not rsps.calls


# --- AC 6.5: the push has the level, the reason path, the top precaution and the app link -----

def test_ac_6_5_message_contents(job_env):
    G = fake_graph()
    msg = notify.build_message("08807", "Bridgewater, NJ", fake_assessment("high"), {}, G=G)
    assert msg["title"] == "FloodSense: High flood risk in Bridgewater, NJ"
    assert msg["body"] == f"Flash Flood Warning → Bridgewater, NJ · {PRECAUTION} · Open FloodSense: {APP_URL}"
    assert msg["priority"] == "urgent" and msg["tags"] == "warning,ocean"
    assert msg["click"] == f"{APP_URL}/alerts?zip=08807"
    assert notify.build_message("08807", "Bridgewater, NJ", fake_assessment("elevated"), {}, G=G)["priority"] == "high"


def test_ac_6_5_forced_level_uses_places_precaution(job_env):
    record = {"precautions": {"flood": {"high": ["Leave low areas now."]}}}
    msg = notify.build_message("08807", "Bridgewater, NJ", fake_assessment("low"), record, level="high")
    assert msg["title"] == "FloodSense: High flood risk in Bridgewater, NJ"
    assert "Leave low areas now." in msg["body"] and f"Open FloodSense: {APP_URL}" in msg["body"]


def test_ac_6_5_app_url_default(monkeypatch):
    monkeypatch.delenv("APP_URL", raising=False)
    assert config.app_url() == "http://localhost:8501"


def test_ac_6_5_send_posts_headers_with_timeout(rsps, job_env, monkeypatch):
    seen = {}
    real = requests.post

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(requests, "post", spy)
    rsps.post(NTFY_RE, status=200)
    msg = notify.build_message("08807", "Bridgewater, NJ", fake_assessment("high"), {}, G=fake_graph())
    notify.send(config.ntfy_topic("08807"), msg)
    req = ntfy_calls(rsps)[0].request
    assert req.url == f"https://ntfy.sh/{expected_topic('08807', SECRET)}"
    assert SECRET not in req.url
    assert req.headers["Title"] == msg["title"] and req.headers["Priority"] == "urgent"
    assert req.headers["Tags"] == "warning,ocean" and req.headers["Click"] == f"{APP_URL}/alerts?zip=08807"
    assert req.body.decode("utf-8") == msg["body"]
    assert seen["timeout"] == config.HTTP_TIMEOUT


def test_ac_6_5_non_ascii_title_is_encoded(rsps, job_env):
    rsps.post(NTFY_RE, status=200)
    msg = notify.build_message("00601", "Adjuntas, PR · Año", fake_assessment("high", "00601"), {})
    notify.send("floodsense-00601-x", msg)
    assert ntfy_calls(rsps)[0].request.headers["Title"].startswith("=?UTF-8?B?")


def test_ac_6_5_alerts_page_chain_helper_shared():
    from graph import explain

    G = fake_graph()
    r = fake_assessment("high").reasons[0]
    assert explain.chain(G, r) == "Flash Flood Warning → Bridgewater, NJ"
    src = (config.APP_DIR / "app_pages" / "alerts.py").read_text()
    assert "from graph.explain import chain, node_name" in src and "def chain(" not in src
