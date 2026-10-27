"""FloodSense app log: a daily rolling file, log/flood-appsense.log.

The app shell (app.py) calls ``configure(show_debug)`` on every run; pages import ``log``.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import config

log = logging.getLogger("floodsense.app")


def _daily_log_name(default_name: str) -> str:
    # TimedRotatingFileHandler names rotated files "flood-appsense.log.2026-10-25";
    # rename them to "flood-appsense-2026-10-25.log".
    path = Path(default_name)
    stem, _, day = path.name.rpartition(".")
    return str(path.with_name(f"{Path(stem).stem}-{day}.log"))


def configure(show_debug: bool) -> logging.Logger:
    """Attach the daily file handler (once per process) and set the level from the debug switch."""
    _log_path = os.path.abspath(config.LOG_DIR / config.LOG_FILE_NAME)
    # Streamlit reruns this script in the same process, so the logger keeps its handlers.
    # Add the file handler only once, and drop any handler an older version of the script left.
    if not any(getattr(h, "baseFilename", None) == _log_path for h in log.handlers):
        for _old in list(log.handlers):
            log.removeHandler(_old)
            _old.close()
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _handler = TimedRotatingFileHandler(
            config.LOG_DIR / config.LOG_FILE_NAME,
            when="midnight",
            backupCount=config.LOG_DAYS_KEPT,
            encoding="utf-8",
        )
        _handler.namer = _daily_log_name
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(_handler)
        log.propagate = False
    log.setLevel(logging.DEBUG if show_debug else logging.INFO)
    return log
