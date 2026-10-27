"""AC 6.6: check.yml runs every 15 minutes and on demand, reads keys only from Actions secrets,
and no key is committed anywhere in uscongressionalapp-floodsense/."""

from __future__ import annotations

import re
import tomllib

import yaml

import config

WORKFLOW = config.APP_DIR / ".github" / "workflows" / "check.yml"
SECRET_NAMES = {"SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "NTFY_TOPIC_SECRET", "NWS_USER_AGENT",
                "APP_URL"}
SKIP = {".streamlit/secrets.toml", "data/cache", "log", ".venv"}
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
JWT = re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.")
# Built from parts so this file does not match itself.
SUPABASE_PREFIXES = ("sb_" + "secret_", "sb_" + "publishable_")


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_ac_6_6_cron_every_15_minutes_and_manual_trigger():
    wf = _workflow()
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    assert triggers["schedule"] == [{"cron": "*/15 * * * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"zip", "force_level"}
    assert not inputs["zip"]["required"] and not inputs["force_level"]["required"]


def test_ac_6_6_runs_the_check_on_python_312():
    steps = _workflow()["jobs"]["check"]["steps"]
    assert any(s.get("with", {}).get("python-version") == "3.12" for s in steps)
    assert any(s.get("run") == "pip install -r requirements.txt" for s in steps)
    run = next(s for s in steps if "alerts.check" in (s.get("run") or ""))
    assert run["run"].startswith("python -m alerts.check")


def test_ac_6_6_every_key_comes_from_secrets():
    text = WORKFLOW.read_text()
    for expr in re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", text):
        assert expr.startswith("secrets.") or expr.startswith("github.event.inputs."), expr
    run = next(s for s in _workflow()["jobs"]["check"]["steps"] if "alerts.check" in (s.get("run") or ""))
    env = run["env"]
    assert SECRET_NAMES <= set(env)
    for name in SECRET_NAMES:
        assert env[name] == f"${{{{ secrets.{name} }}}}"
    # Inputs reach the shell only through env vars, never pasted into the command.
    assert "${{" not in run["run"]


def _tracked_files():
    root = config.APP_DIR
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or SKIP_PARTS & set(path.relative_to(root).parts):
            continue
        if any(rel == s or rel.startswith(s + "/") for s in SKIP):
            continue
        yield rel, path


# Only real keys are scanned for; URLs and the User-Agent are not secret.
SCANNED_KEYS = ("SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "NTFY_TOPIC_SECRET")


def _local_secret_values() -> list[str]:
    local = config.APP_DIR / ".streamlit" / "secrets.toml"
    if not local.exists():
        return []
    data = tomllib.loads(local.read_text())
    return [str(data[k]) for k in SCANNED_KEYS if isinstance(data.get(k), str) and data[k].strip()]


def test_ac_6_6_secret_scan_finds_nothing():
    values = _local_secret_values()
    found = []
    for rel, path in _tracked_files():
        text = path.read_bytes().decode("utf-8", errors="ignore")
        if JWT.search(text):
            found.append((rel, "JWT-like string"))
        for prefix in SUPABASE_PREFIXES:
            if prefix in text:
                found.append((rel, f"{prefix} key"))
        for v in values:
            if v in text:
                found.append((rel, "a value from .streamlit/secrets.toml"))
    assert not found, found


def test_ac_6_6_secret_scan_detects_a_planted_key(tmp_path):
    fake_jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 10
    assert JWT.search(f'KEY = "{fake_jwt}"')
    assert any(p in "x = 'sb_" + "secret_abc'" for p in SUPABASE_PREFIXES)


def test_ac_6_6_only_key_values_are_scanned(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / ".streamlit").mkdir(parents=True)
    (app / ".streamlit" / "secrets.toml").write_text(
        'APP_URL = "http://localhost:8501"\nNWS_USER_AGENT = "FloodSense (a@b.c)"\n'
        'SUPABASE_URL = "https://x.supabase.co"\nNTFY_TOPIC_SECRET = "s3cr3t-value"\n')
    monkeypatch.setattr(config, "APP_DIR", app)
    assert _local_secret_values() == ["s3cr3t-value"]


def test_ac_6_6_secrets_example_has_placeholders():
    example = (config.APP_DIR / ".streamlit" / "secrets.toml.example").read_text()
    for name in ("NTFY_TOPIC_SECRET", "APP_URL", "SUPABASE_SERVICE_KEY"):
        assert re.search(rf"^{name} = \"", example, re.M), name
    gitignore = config.APP_DIR / ".gitignore"
    if gitignore.exists():  # .dockerignore keeps .gitignore out of the image
        assert ".streamlit/secrets.toml" in gitignore.read_text()
