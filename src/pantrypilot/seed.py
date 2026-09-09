"""(Re)create the local database from the bundled demo dataset under ``data/``.

Run with ``python -m pantrypilot.seed`` or ``make seed``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from . import config
from .store import Store, get_store


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def seed_store(store: Store, data_dir: Path | None = None, *, reset: bool = True) -> dict[str, int]:
    """Load ``data/*`` into ``store``. Returns counts per collection."""
    data_dir = data_dir or config.DATA_DIR
    if reset:
        store.reset()

    with (data_dir / "org.yaml").open(encoding="utf-8") as fh:
        org = yaml.safe_load(fh)
    store.put_doc("org", "org", org)

    counts = {"org": 1}
    for kind, filename in (
        ("volunteer", "volunteers.json"),
        ("shift", "shifts.json"),
        ("inventory", "inventory.json"),
        ("contact", "contacts.json"),
    ):
        docs = _load_json(data_dir / filename)
        for doc in docs:
            store.put_doc(kind, doc["id"], doc)
        counts[kind] = len(docs)

    messages = _load_json(data_dir / "inbound_messages.json")
    for msg in messages:
        store.add_message(
            direction="inbound",
            message_id=msg["id"],
            ts=msg["ts"],
            channel=msg.get("channel", "sms"),
            from_id=msg.get("from_id"),
            from_name=msg.get("from_name"),
            to_id="pantry",
            to_name=org.get("short_name", "Pantry"),
            body=msg["text"],
            handled=bool(msg.get("handled", False)),
        )
    counts["inbound_messages"] = len(messages)
    store.set_meta("seeded_at", config.now_iso())
    store.set_meta("demo_today", config.today().isoformat())
    return counts


def seed(*, reset: bool = True, clear_sessions: bool = True) -> dict[str, int]:
    """Seed the process-wide store and optionally wipe Strands session files."""
    counts = seed_store(get_store(), reset=reset)
    if clear_sessions and config.SESSIONS_DIR.exists():
        shutil.rmtree(config.SESSIONS_DIR, ignore_errors=True)
    return counts


def main(argv: list[str] | None = None) -> int:
    config.configure_logging()
    counts = seed()
    print(f"Seeded {config.DB_PATH}:")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
