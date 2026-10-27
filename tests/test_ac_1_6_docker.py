"""AC 1.6 (static): Docker serves on 8501, secrets are mounted read-only and never baked in."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def dockerfile_lines():
    return [l.strip() for l in (ROOT / "Dockerfile").read_text().splitlines() if l.strip() and not l.strip().startswith("#")]


def test_ac_1_6_compose_mounts_secrets_read_only_and_cache_volume():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    app = compose["services"]["app"]
    vols = app["volumes"]
    secrets = [v for v in vols if isinstance(v, dict) and v.get("target") == "/app/.streamlit/secrets.toml"]
    assert len(secrets) == 1
    s = secrets[0]
    assert s["type"] == "bind" and s["source"] == "./.streamlit/secrets.toml"
    assert s["read_only"] is True
    assert s["bind"]["create_host_path"] is False  # missing file fails instead of making a directory
    cache = [v for v in vols if isinstance(v, dict) and v.get("target") == "/app/data/cache"]
    assert cache and cache[0]["type"] == "volume" and cache[0]["source"] in compose["volumes"]
    assert "8501:8501" in app["ports"]


def test_ac_1_6_dockerfile_base_port_healthcheck():
    lines = dockerfile_lines()
    assert lines[0] == "FROM python:3.12-slim"
    assert "EXPOSE 8501" in lines
    text = "\n".join(lines)
    assert "HEALTHCHECK" in text and "/_stcore/health" in text
    assert "PGEOCODE_DATA_DIR=/app/.pgeocode" in text
    assert re.search(r"pgeocode\.Nominatim\('us'\)\.query_postal_code", text)


def test_ac_1_6_image_never_contains_secrets():
    for line in dockerfile_lines():
        if line.upper().startswith(("COPY", "ADD")):
            assert "secrets" not in line.lower()
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".streamlit/secrets.toml" in ignored


def test_ac_1_6_secrets_are_git_ignored_and_example_has_placeholder():
    if not (ROOT / ".gitignore").exists():
        import pytest

        pytest.skip(".gitignore is excluded from the Docker image")
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for entry in (".streamlit/secrets.toml", "data/cache/", ".pgeocode/"):
        assert entry in ignored
    example = (ROOT / ".streamlit" / "secrets.toml.example").read_text()
    assert "NWS_USER_AGENT" in example and "@example.com" in example


def test_ac_1_6_requirements_are_pinned():
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    names = {r.split("==")[0].lower() for r in reqs}
    assert {"streamlit", "requests", "pgeocode", "pandas", "pytest", "responses"} <= names
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*", r) for r in reqs)


def test_ac_1_6_no_secret_literals_in_code():
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        # Contact emails belong in secrets.toml, never in code.
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", path.read_text()), path
