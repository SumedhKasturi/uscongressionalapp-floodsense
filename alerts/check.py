"""Scheduled risk check (check.yml, every 15 minutes): push when a registered ZIP's risk rises.

For each distinct registered ZIP the risk is computed with the same pipeline the pages use
(``get_places`` -> ``live_inputs`` -> ``build_graph`` -> ``assess``) and compared with the
stored ``zip_state.last_risk`` (missing counts as low). One push is sent when the new level
is above it, unless that same level was already pushed within ``config.COOLDOWN_HOURS``; the
new level is then saved, so falls are recorded too. When the push fails the state is left
alone, so the next run retries. A state write is retried a few times before giving up.

One ZIP's failure is logged and the others still run. The exit code is 1 only when the
settings are missing or every ZIP failed.

Run from ``uscongressionalapp-floodsense/``::

    python -m alerts.check [--zip Z] [--force-level L] [--dry-run]

``--force-level`` (needs ``--zip``) sends a push marked as a test at that level, without
comparing or saving the state (the demo push never corrupts it). ``--dry-run`` prints the
computed level and the push instead of sending.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import config
from alerts import notify
from data import places, supabase_rest
from data.location import resolve_zip
from data.sources import parse_time
from graph import build, reason, thresholds

log = logging.getLogger("floodsense.alerts")

STATE_WRITE_ATTEMPTS = 3
STATE_WRITE_BACKOFF_S = 2  # waits 2 s, then 4 s, between attempts
_sleep = time.sleep  # the default wait between state-write attempts (tests replace it)
TEST_TITLE_PREFIX = "TEST: "
TEST_BODY_SUFFIX = " (This is a test alert.)"


def _rank(level: str) -> int:
    return thresholds.LEVELS.index(level)


def assess_zip(zip_code: str, now: datetime | None = None):
    """(location, places record, graph, assessment) for one ZIP, with the pages' uncached pipeline."""
    loc = resolve_zip(zip_code)
    record = places.get_places(loc, now=now)
    inputs = build.live_inputs(loc, record, now=now)
    G = build.build_graph(record, inputs)
    return loc, record, G, reason.assess(G, record, inputs)


def _unique(zips) -> list[str]:
    out: list[str] = []
    for z in zips:
        z = str(z).strip()
        if z and z not in out:
            out.append(z)
    return out


def should_push(level: str, state: dict | None, now: datetime) -> bool:
    """Push iff ``level`` is above the stored last_risk (missing = low) and that same level
    was not already pushed within the cooldown. A higher level always pushes right away."""
    state = state or {}
    previous = state.get("last_risk") or thresholds.LEVELS[0]
    if _rank(level) <= _rank(previous):
        return False
    pushed_at = parse_time(state.get("last_pushed_at")) if state.get("last_pushed_at") else None
    if state.get("last_pushed_level") == level and pushed_at is not None:
        return now - pushed_at >= timedelta(hours=config.COOLDOWN_HOURS)
    return True


def _save_state(zip_code, level, service_key, pushed_at, sleep) -> bool:
    """Write the state, retrying with a short backoff. Returns False (logged loudly) if every attempt failed."""
    for attempt in range(1, STATE_WRITE_ATTEMPTS + 1):
        try:
            supabase_rest.upsert_state(zip_code, level, service_key, pushed_at=pushed_at)
            return True
        except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
            log.warning("State write for %s failed (attempt %d/%d): %s", zip_code, attempt, STATE_WRITE_ATTEMPTS, exc)
            if attempt < STATE_WRITE_ATTEMPTS:
                sleep(STATE_WRITE_BACKOFF_S * 2 ** (attempt - 1))
    log.error("Could not save the state for %s after %d attempts%s", zip_code, STATE_WRITE_ATTEMPTS,
              "; the push was sent, so the next run may push it again" if pushed_at else "")
    return False


def _print_push(zip_code: str, level: str, previous: str | None, push: bool, message: dict) -> None:
    state = "not compared (forced)" if previous is None else f"last {previous}"
    print(f"{zip_code}: level {level} ({state}); "
          f"{'would push' if push else 'no push (the message it would send on a rise)'}")
    print(json.dumps(message, indent=2, ensure_ascii=False))


def _check_zip(zip_code, states, force_level, dry_run, now, service_key, sleep) -> bool:
    """Assess one ZIP, push on a rise and save the state. Returns True on success; may raise."""
    loc, record, G, assessment = assess_zip(zip_code, now=now)
    level = force_level or assessment.area_level
    state = states.get(zip_code) or {}
    previous = None if force_level else (state.get("last_risk") or thresholds.LEVELS[0])
    push = force_level is not None or should_push(level, state, now)
    message = notify.build_message(zip_code, loc.place, assessment, record, G=G, level=level)
    if force_level:
        message["title"] = TEST_TITLE_PREFIX + message["title"]
        message["body"] += TEST_BODY_SUFFIX
    log.info("%s: level %s (%s); push %s", zip_code, level, "forced" if force_level else f"last {previous}", push)

    if dry_run:
        _print_push(zip_code, level, previous, push, message)
        return True
    pushed_at = None
    if push:
        try:
            notify.send(config.ntfy_topic(zip_code), message)
        except notify.NotifyError as exc:
            log.error("Push failed for %s: %s; state not updated, the next run retries", zip_code, exc)
            return False
        pushed_at = now
        print(f"{zip_code}: pushed {level}")
    else:
        print(f"{zip_code}: {level}, no push")
    if force_level is None:
        return _save_state(zip_code, level, service_key, pushed_at, sleep)
    return True


def run(zips=None, force_level=None, dry_run=False, now=None, sleep=None) -> int:
    """Check each ZIP (all registered ZIPs when ``zips`` is None) and push on a rise. Returns the exit code.

    ``sleep`` waits between state-write retries (default ``time.sleep``).
    """
    sleep = sleep or _sleep
    now = now or datetime.now(timezone.utc)
    if force_level is not None and force_level not in thresholds.LEVELS:
        log.error("Unknown --force-level %r", force_level)
        return 1
    if force_level is not None and not zips:
        # A forced push to every registered ZIP would reach every subscriber.
        log.error("--force-level needs --zip")
        return 1
    service_key = config.get_setting("SUPABASE_SERVICE_KEY")
    if not dry_run and not config.ntfy_topic(config.DEFAULT_ZIP):
        log.error("NTFY_TOPIC_SECRET is not set")
        return 1
    # The state (and the registration list) needs the service key. A forced push never reads the
    # state, and a dry run without the key compares against "no state".
    needs_state = force_level is None
    if not service_key and (zips is None or (needs_state and not dry_run)):
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        return 1

    try:
        zip_list = _unique(zips if zips is not None else supabase_rest.registered_zips(service_key))
    except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
        log.error("Could not read the registered ZIPs: %s", exc)
        return 1
    if not zip_list:
        log.info("No registered ZIPs")
        print("No registered ZIPs")
        return 0

    states: dict[str, dict] = {}
    if needs_state and service_key:
        try:
            states = supabase_rest.get_states(service_key)
        except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
            # Without the stored levels every ZIP would look like a rise; push nothing this run.
            log.error("Could not read zip_state: %s", exc)
            return 1
    elif needs_state:
        log.warning("No SUPABASE_SERVICE_KEY: dry run compares against no stored state")

    failed = []
    for zip_code in zip_list:
        try:
            ok = _check_zip(zip_code, states, force_level, dry_run, now, service_key, sleep)
        except Exception:
            log.exception("Risk check failed for %s", zip_code)
            ok = False
        if not ok:
            failed.append(zip_code)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::warning::Flood risk check failed for ZIP {zip_code}; see the log above")
    return 1 if len(failed) == len(zip_list) else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alerts.check", description=__doc__.splitlines()[0])
    parser.add_argument("--zip", action="append", dest="zips", metavar="ZIP",
                        help="check only this ZIP (repeatable); default: every registered ZIP")
    parser.add_argument("--force-level", choices=thresholds.LEVELS[1:],
                        help="send a test push at this level without comparing or saving the state (needs --zip)")
    parser.add_argument("--dry-run", action="store_true", help="print the push instead of sending it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(zips=args.zips, force_level=args.force_level, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
