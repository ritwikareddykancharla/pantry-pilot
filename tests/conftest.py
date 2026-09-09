"""Shared fixtures: a temporary seeded store bound as the process-wide store."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so tests can import main.py and app.server
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("DEMO_TODAY", "2026-09-11")
os.environ.setdefault("SWEEP_INTERVAL_SECONDS", "0")
os.environ.setdefault("SWEEP_ON_START", "0")
os.environ.setdefault("AGENT_BACKEND", "local")

from pantrypilot import config  # noqa: E402
from pantrypilot.seed import seed_store  # noqa: E402
from pantrypilot.store import Store, bind_store  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    """Fresh seeded SQLite store in a temp dir, bound as the process-wide store."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pantrypilot.sqlite3")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    s = Store(tmp_path / "pantrypilot.sqlite3")
    seed_store(s, reset=True)
    bind_store(s)
    yield s
    bind_store(None)
    s.close()
