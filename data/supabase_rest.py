"""Minimal Supabase access over PostgREST, with plain ``requests`` (no extra dependency).

The app uses the anon key (``SUPABASE_KEY``); row-level security limits what it can do.
Only the weekly cleanup passes the service-role key to :func:`rpc`.
"""

from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger(__name__)

# Every places version lives in this one table (see supabase/schema.sql).
TABLE = "places_versions"
# So the "not configured" warning is logged once per process, not on every call.
_warned_unconfigured = False


class SupabaseUnavailable(RuntimeError):
    """Supabase is not configured, timed out, or answered with a 5xx."""


class SupabaseError(RuntimeError):
    """Supabase answered with a 4xx (bad key, missing table, policy refusal...)."""


def _settings() -> tuple[str, str]:
    """Return ``(url, anon key)`` from config.

    Raises SupabaseUnavailable when either is missing; the warning is logged only once.
    """
    global _warned_unconfigured
    settings = config.supabase_settings()
    if settings is None:
        if not _warned_unconfigured:
            log.warning("Supabase is not configured (SUPABASE_URL / SUPABASE_KEY); using local places copies only")
            _warned_unconfigured = True
        raise SupabaseUnavailable("Supabase is not configured")
    return settings


def _headers(key: str, extra: dict | None = None) -> dict:
    """PostgREST request headers. Supabase expects the key both as ``apikey`` and as a Bearer token.

    ``extra`` adds or overrides headers (e.g. ``Prefer``).
    """
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(extra or {})
    return headers


def _request(method: str, url: str, key: str, **kwargs) -> requests.Response:
    """Send one request to Supabase and return the response when it succeeded (2xx/3xx).

    Raises SupabaseUnavailable on a network error, timeout or 5xx (a temporary problem,
    so callers fall back to the local copy) and SupabaseError on a 4xx (our key,
    table or request is wrong, or row-level security refused it).
    """
    headers = _headers(key, kwargs.pop("headers", None))
    try:
        resp = requests.request(method, url, headers=headers, timeout=config.HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:  # timeout, DNS, connection refused
        raise SupabaseUnavailable(f"Supabase request failed: {exc}") from exc
    if resp.status_code >= 500:
        raise SupabaseUnavailable(f"Supabase returned HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise SupabaseError(f"Supabase returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def latest_version(zip_code: str) -> dict | None:
    """The newest ``places_versions`` row for ``zip_code``, or None when there is none.

    Raises SupabaseUnavailable or SupabaseError (see :func:`_request`), and
    SupabaseUnavailable if the answer is not valid JSON.
    """
    url, key = _settings()
    resp = _request(
        "GET",
        f"{url}/rest/v1/{TABLE}",
        key,
        # PostgREST filter syntax: rows where zip = zip_code, newest version first, only one.
        params={"zip": f"eq.{zip_code}", "order": "version.desc", "limit": "1", "select": "*"},
    )
    try:
        rows = resp.json()
    except ValueError as exc:
        raise SupabaseUnavailable("Supabase returned invalid JSON") from exc
    return rows[0] if isinstance(rows, list) and rows else None


def upsert_version(row: dict) -> None:
    """Insert ``row``, or replace the existing row with the same ``(zip, version)``.

    A ZIP rebuilt twice on the same UTC day gets the same version, so a plain insert
    would fail on the primary key; this replaces that day's row instead. Returns
    nothing; raises SupabaseUnavailable or SupabaseError (see :func:`_request`).
    """
    url, key = _settings()
    _request(
        "POST",
        f"{url}/rest/v1/{TABLE}",
        key,
        # on_conflict + merge-duplicates turns the insert into an upsert; return=minimal skips echoing the row back.
        params={"on_conflict": "zip,version"},
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        json=row,
    )


def rpc(name: str, key: str, params: dict | None = None, url: str | None = None):
    """Call a Postgres function through ``/rest/v1/rpc/<name>`` with ``key`` (e.g. the service key).

    Returns the function's JSON result, or None if the body is not JSON. Raises
    SupabaseUnavailable when the URL or key is missing, plus the errors of :func:`_request`.
    """
    # Takes its own key (not the app's anon key): the cleanup function is not callable by anon.
    url = (url or config.get_setting("SUPABASE_URL") or "").rstrip("/")
    if not url or not key:
        raise SupabaseUnavailable("SUPABASE_URL or the key is not set")
    resp = _request("POST", f"{url}/rest/v1/rpc/{name}", key, json=params or {})
    try:
        return resp.json()
    except ValueError:
        return None


# --- Phone alerts (story 6) -----------------------------------------------------

REGISTRATIONS = "registrations"
ZIP_STATE = "zip_state"


def _service_url(key: str) -> str:
    """``SUPABASE_URL`` (no trailing slash) for a service-key call; raises SupabaseUnavailable if unset."""
    url = (config.get_setting("SUPABASE_URL") or "").rstrip("/")
    if not url or not key:
        raise SupabaseUnavailable("SUPABASE_URL or the service key is not set")
    return url


def _json_rows(resp: requests.Response) -> list:
    try:
        rows = resp.json()
    except ValueError as exc:
        raise SupabaseUnavailable("Supabase returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise SupabaseUnavailable("Supabase returned an unexpected answer")
    return rows


def insert_registration(row: dict) -> None:
    """Insert one ``registrations`` row (zip, name, consent) with the app's anon key.

    Anon may only insert, never read, so the row is not echoed back (``return=minimal``).
    Raises SupabaseUnavailable or SupabaseError (see :func:`_request`).
    """
    url, key = _settings()
    _request("POST", f"{url}/rest/v1/{REGISTRATIONS}", key, headers={"Prefer": "return=minimal"}, json=row)


def registered_zips(service_key: str) -> list[str]:
    """Every distinct registered ZIP, sorted. Needs the service key (anon cannot read the table)."""
    url = _service_url(service_key)
    resp = _request("GET", f"{url}/rest/v1/{REGISTRATIONS}", service_key, params={"select": "zip"})
    return sorted({str(r["zip"]) for r in _json_rows(resp) if isinstance(r, dict) and r.get("zip")})


def get_states(service_key: str) -> dict[str, dict]:
    """``{zip: {"last_risk", "last_pushed_level", "last_pushed_at"}}`` for every ZIP with a stored state."""
    url = _service_url(service_key)
    resp = _request("GET", f"{url}/rest/v1/{ZIP_STATE}", service_key,
                    params={"select": "zip,last_risk,last_pushed_level,last_pushed_at"})
    return {str(r["zip"]): {"last_risk": r.get("last_risk"), "last_pushed_level": r.get("last_pushed_level"),
                            "last_pushed_at": r.get("last_pushed_at")}
            for r in _json_rows(resp) if isinstance(r, dict) and r.get("zip")}


def upsert_state(zip_code: str, level: str, service_key: str, pushed_at=None) -> None:
    """Store ``level`` as the ZIP's ``last_risk`` (insert, or replace the existing row).

    With ``pushed_at`` (a datetime) the push is recorded too: ``last_pushed_level`` and
    ``last_pushed_at``. Without it those columns are left as they are.
    """
    from datetime import datetime, timezone

    url = _service_url(service_key)
    row = {"zip": zip_code, "last_risk": level, "updated_at": datetime.now(timezone.utc).isoformat()}
    if pushed_at is not None:
        row.update(last_pushed_level=level, last_pushed_at=pushed_at.isoformat())
    _request(
        "POST",
        f"{url}/rest/v1/{ZIP_STATE}",
        service_key,
        params={"on_conflict": "zip"},
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        json=row,
    )
