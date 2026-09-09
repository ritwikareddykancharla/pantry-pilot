"""Environment-driven configuration and the demo clock.

Everything time-related in Pantry Pilot is computed relative to ``DEMO_TODAY`` so the
bundled dataset stays coherent no matter when the demo is run.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("PANTRYPILOT_DATA_DIR", REPO_ROOT / "data"))
STATE_DIR = Path(os.getenv("PANTRYPILOT_STATE_DIR", REPO_ROOT / ".data"))
DB_PATH = Path(os.getenv("PANTRYPILOT_DB", STATE_DIR / "pantrypilot.sqlite3"))
SESSIONS_DIR = Path(os.getenv("PANTRYPILOT_SESSIONS_DIR", STATE_DIR / "sessions"))

DEFAULT_TODAY = "2026-09-11"
LOOKAHEAD_DAYS = 7

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def today() -> date:
    """Return the demo "today" (env ``DEMO_TODAY``, default 2026-09-11, a Friday)."""
    raw = os.getenv("DEMO_TODAY", DEFAULT_TODAY)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.fromisoformat(DEFAULT_TODAY)


def now() -> datetime:
    """Return a datetime for "now" in demo time: today's date at the real wall-clock time."""
    wall = datetime.now().time().replace(microsecond=0)
    return datetime.combine(today(), wall)


def now_iso() -> str:
    """ISO-8601 timestamp for audit entries and messages."""
    return now().isoformat(timespec="seconds")


def day_key(d: date) -> str:
    """Three-letter lowercase weekday key ("mon" ... "sun")."""
    return DAY_KEYS[d.weekday()]


def parse_hhmm(value: str) -> time:
    """Parse "HH:MM" into a ``time``."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def configure_logging() -> None:
    """Configure stdlib logging once; the Strands logger is set to INFO per the shared spec."""
    if logging.getLogger().handlers:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("strands").setLevel(logging.INFO)
    logging.getLogger("botocore").setLevel(logging.WARNING)
