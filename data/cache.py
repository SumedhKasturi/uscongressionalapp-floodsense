"""Network-first JSON fetch with a local cache fallback.

Every successful response is written to ``<CACHE_DIR>/<key>.json`` as
``{"fetched_at": ISO-8601 UTC, "source_url": ..., "data": ...}``. When the
network call fails, the cached copy is returned with ``stale=True``; if there
is no cached copy, ``None`` is returned.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

import config

log = logging.getLogger(__name__)

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class CacheResult:
    data: Any
    fetched_at: str
    source_url: str
    stale: bool = False


def cache_path(key: str) -> Path:
    return Path(config.CACHE_DIR) / f"{_SAFE_KEY.sub('_', key)}.json"


def _write_atomic(path: Path, envelope: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_cached(key: str) -> CacheResult | None:
    path = cache_path(key)
    try:
        with open(path, encoding="utf-8") as f:
            env = json.load(f)
        return CacheResult(
            data=env["data"],
            fetched_at=env["fetched_at"],
            source_url=env.get("source_url", ""),
            stale=True,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Unreadable cache file %s: %s", path, exc)
        return None


def get_json(key: str, url: str, headers: dict | None = None) -> CacheResult | None:
    """Fetch ``url`` as JSON (network first), caching under ``key``."""
    try:
        resp = requests.get(url, headers=headers or {}, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        cached = read_cached(key)
        if cached is not None:
            log.warning("Fetch failed for %s (%s); using cache from %s", url, exc, cached.fetched_at)
        else:
            log.warning("Fetch failed for %s (%s); no cached copy", url, exc)
        return cached

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope = {"fetched_at": fetched_at, "source_url": url, "data": data}
    try:
        _write_atomic(cache_path(key), envelope)
    except OSError as exc:
        log.warning("Could not write cache for %s: %s", key, exc)
    return CacheResult(data=data, fetched_at=fetched_at, source_url=url, stale=False)


def get_text(key: str, url: str, headers: dict | None = None,
             validate: Callable[[str], Any] | None = None) -> CacheResult | None:
    """Like ``get_json``, for a text body (e.g. RSS XML): network first, cached under ``key``.

    The envelope's ``data`` holds the raw text. An empty body counts as a failure. ``validate``,
    if given, runs on the body before it is cached; if it raises, that is a fetch failure too,
    so a bad body never replaces a good cached copy.
    """
    try:
        resp = requests.get(url, headers=headers or {}, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.text
        if not data.strip():
            raise ValueError("empty response body")
        if validate is not None:
            try:
                validate(data)
            except Exception as exc:
                raise ValueError(f"invalid response body: {exc}") from exc
    except (requests.RequestException, ValueError) as exc:
        cached = read_cached(key)
        if cached is not None and not isinstance(cached.data, str):
            log.warning("Cache file for %s does not hold text; ignoring it", key)
            cached = None
        if cached is not None:
            log.warning("Fetch failed for %s (%s); using cache from %s", url, exc, cached.fetched_at)
        else:
            log.warning("Fetch failed for %s (%s); no cached copy", url, exc)
        return cached

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope = {"fetched_at": fetched_at, "source_url": url, "data": data}
    try:
        _write_atomic(cache_path(key), envelope)
    except OSError as exc:
        log.warning("Could not write cache for %s: %s", key, exc)
    return CacheResult(data=data, fetched_at=fetched_at, source_url=url, stale=False)
