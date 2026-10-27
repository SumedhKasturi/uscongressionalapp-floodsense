"""AC 2.5: weekly cleanup keeps only the latest version per ZIP; the Ida archive loads offline."""

import re
import socket
import sqlite3
import subprocess
import sys

import pytest
import requests
import yaml

import config
from data import places
from tests.conftest import SUPABASE_TEST_SERVICE_KEY, SUPABASE_TEST_URL

sys.path.insert(0, str(config.APP_DIR / "scripts"))
import cleanup_place_versions  # noqa: E402

SCHEMA = (config.APP_DIR / "supabase" / "schema.sql").read_text()
WORKFLOW = config.APP_DIR / ".github" / "workflows" / "cleanup.yml"
RPC_URL = f"{SUPABASE_TEST_URL}/rest/v1/rpc/delete_old_place_versions"


@pytest.fixture
def service_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", SUPABASE_TEST_SERVICE_KEY)
    monkeypatch.setenv("SUPABASE_KEY", "anon-key-must-not-be-used")


# --- cleanup script ---------------------------------------------------------------

def test_ac_2_5_cleanup_calls_rpc_with_service_key(rsps, service_env, capsys):
    rsps.post(RPC_URL, json=3)
    assert cleanup_place_versions.main() == 0
    assert len(rsps.calls) == 1
    req = rsps.calls[0].request
    assert req.url == RPC_URL
    assert req.headers["apikey"] == SUPABASE_TEST_SERVICE_KEY
    assert req.headers["Authorization"] == f"Bearer {SUPABASE_TEST_SERVICE_KEY}"
    assert "Deleted 3 old place versions" in capsys.readouterr().out


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 500},
        {"status": 401, "json": {"message": "permission denied"}},
        {"body": requests.exceptions.ReadTimeout("slow")},
        {"json": {"unexpected": True}},
    ],
)
def test_ac_2_5_cleanup_exits_non_zero_on_error(rsps, service_env, capsys, kwargs):
    rsps.post(RPC_URL, **kwargs)
    assert cleanup_place_versions.main() != 0
    assert "Cleanup failed" in capsys.readouterr().err


def test_ac_2_5_cleanup_exits_non_zero_without_service_key(rsps, monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_TEST_URL)
    monkeypatch.setenv("SUPABASE_KEY", "anon")
    assert cleanup_place_versions.main() == 1
    assert len(rsps.calls) == 0


def test_ac_2_5_cleanup_script_process_exit_code(tmp_path):
    """Run as the workflow does; with no settings it must fail visibly."""
    env = {"PATH": "/usr/bin:/bin", "FLOODSENSE_LOG_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "scripts/cleanup_place_versions.py"], cwd=config.APP_DIR, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1 and "SUPABASE_SERVICE_KEY" in result.stderr


# --- schema.sql ---------------------------------------------------------------------

def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.lower())


def test_ac_2_5_schema_table_and_primary_key():
    s = norm(SCHEMA)
    assert "create table if not exists public.places_versions" in s
    for col in ("zip text", "version date", "built_at timestamptz", "status jsonb", "data jsonb"):
        assert col in s
    assert "primary key (zip, version)" in s


def test_ac_2_5_schema_rls_policies_for_anon():
    s = norm(SCHEMA)
    assert "alter table public.places_versions enable row level security" in s
    for action in ("select", "insert", "update"):
        assert re.search(rf"create policy \w+ on public.places_versions for {action} to anon", s), action
    assert "for delete to anon" not in s


def test_ac_2_5_schema_cleanup_function_service_role_only():
    s = norm(SCHEMA)
    assert "create or replace function public.delete_old_place_versions() returns integer" in s
    assert "security definer" in s
    assert "revoke all on function public.delete_old_place_versions() from public, anon, authenticated" in s
    assert "grant execute on function public.delete_old_place_versions() to service_role" in s


def test_ac_2_5_cleanup_sql_keeps_only_latest_per_zip():
    """Run the function's DELETE (verbatim, minus the schema prefix) on the matrix data."""
    stmt = re.search(r"(delete from public\.places_versions.*?;)", SCHEMA, re.S).group(1)
    stmt = stmt.replace("public.", "")
    db = sqlite3.connect(":memory:")
    db.execute("create table places_versions (zip text, version text, primary key (zip, version))")
    db.executemany(
        "insert into places_versions values (?, ?)",
        [("08807", "2026-10-11"), ("08807", "2026-10-18"), ("08807", "2026-10-25"), ("10001", "2026-10-02")],
    )
    deleted = db.execute(stmt).rowcount
    assert deleted == 2
    assert sorted(db.execute("select zip, version from places_versions")) == [
        ("08807", "2026-10-25"), ("10001", "2026-10-02"),
    ]


# --- workflow -----------------------------------------------------------------------

def test_ac_2_5_cleanup_workflow_schedule_and_secrets():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare key "on" as True
    assert on["schedule"] == [{"cron": "0 6 * * 1"}]
    assert "workflow_dispatch" in on
    steps = wf["jobs"]["cleanup"]["steps"]
    run = [s for s in steps if "cleanup_place_versions.py" in s.get("run", "")]
    assert len(run) == 1
    assert run[0]["env"] == {
        "SUPABASE_URL": "${{ secrets.SUPABASE_URL }}",
        "SUPABASE_SERVICE_KEY": "${{ secrets.SUPABASE_SERVICE_KEY }}",
    }


# --- Ida archive ---------------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_ac_2_5_ida_archive_loads_offline(no_network):
    archive = places.load_ida_archive()
    gauges = archive["gauges"]
    assert set(gauges) == {"BDBN4", "MRTN4", "MNVN4", "RRTN4", "BDKN4"}
    bdbn4 = gauges["BDBN4"]
    assert bdbn4["usgs"] == "01403200"
    assert max(ft for _, ft in bdbn4["readings"]) == 20.66
    assert bdbn4["max"]["ft"] == 20.66
    assert gauges["MRTN4"]["max"]["ft"] == 11.47
    times = [t for t, _ in bdbn4["readings"]]
    assert times == sorted(times) and times[0].startswith("2021-09-01") and "2021-09-02" in times[len(times) // 2]


def test_ac_2_5_ida_archive_has_no_no_data_values():
    for g in places.load_ida_archive()["gauges"].values():
        assert g["readings"] and all(ft > -1000 for _, ft in g["readings"])
