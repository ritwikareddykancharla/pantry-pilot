"""SQLite-backed store for domain state, messages, decisions, audit log, cycles and reports.

The ``Store`` class is the only thing that touches the database. Its public surface is small
and document-oriented on purpose: a DynamoDB implementation (one table per section, JSON
attributes) could replace it without changing tools or the service layer. See
``docs/architecture.md`` for the swap notes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    kind TEXT NOT NULL DEFAULT 'message',
    from_id TEXT,
    from_name TEXT,
    to_id TEXT,
    to_name TEXT,
    body TEXT NOT NULL,
    handled INTEGER NOT NULL DEFAULT 0,
    handled_note TEXT,
    cycle_id TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    options TEXT NOT NULL,
    recommendation TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    cycle_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    response TEXT,
    result TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    agent TEXT,
    kind TEXT NOT NULL DEFAULT 'tool',
    tool TEXT,
    input TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    status TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    task TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    handoff_trail TEXT NOT NULL DEFAULT '[]',
    summary TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    cycle_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hours (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    volunteer_id TEXT NOT NULL,
    shift_id TEXT,
    hours REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS donations (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    donor TEXT NOT NULL,
    items TEXT NOT NULL,
    scheduled_dropoff TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    cycle_id TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Store:
    """Thread-safe SQLite store. One instance per process is the intended usage."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.DB_PATH
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        """Drop all rows (keeps the schema). Used by ``seed``."""
        with self._lock:
            for table in ("docs", "messages", "decisions", "audit", "cycles", "reports", "hours", "donations", "meta"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def _next_id(self, table: str, prefix: str) -> tuple[str, int]:
        row = self._conn.execute(f"SELECT COALESCE(MAX(seq), 0) AS m FROM {table}").fetchone()
        seq = int(row["m"]) + 1
        return f"{prefix}-{seq:03d}", seq

    # ------------------------------------------------------------------ documents
    def put_doc(self, kind: str, doc_id: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "id": doc_id}
        with self._lock:
            self._conn.execute(
                "INSERT INTO docs(kind, id, body) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET body = excluded.body",
                (kind, doc_id, _dumps(body)),
            )
            self._conn.commit()
        return body

    def get_doc(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM docs WHERE kind = ? AND id = ?", (kind, doc_id)).fetchone()
        return _loads(row["body"]) if row else None

    def list_docs(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM docs WHERE kind = ? ORDER BY id", (kind,)).fetchall()
        return [_loads(r["body"]) for r in rows]

    def delete_doc(self, kind: str, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE kind = ? AND id = ?", (kind, doc_id))
            self._conn.commit()

    # Convenience accessors for the domain documents.
    def org(self) -> dict[str, Any]:
        return self.get_doc("org", "org") or {}

    def volunteers(self) -> list[dict[str, Any]]:
        return self.list_docs("volunteer")

    def volunteer(self, volunteer_id: str) -> dict[str, Any] | None:
        return self.get_doc("volunteer", volunteer_id)

    def shifts(self) -> list[dict[str, Any]]:
        return sorted(self.list_docs("shift"), key=lambda s: (s["date"], s["start"], s["id"]))

    def shift(self, shift_id: str) -> dict[str, Any] | None:
        return self.get_doc("shift", shift_id)

    def inventory(self) -> list[dict[str, Any]]:
        return self.list_docs("inventory")

    def contacts(self) -> list[dict[str, Any]]:
        return self.list_docs("contact")

    # ------------------------------------------------------------------ messages
    def add_message(
        self,
        *,
        direction: str,
        body: str,
        from_id: str | None = None,
        from_name: str | None = None,
        to_id: str | None = None,
        to_name: str | None = None,
        channel: str = "sms",
        kind: str = "message",
        ts: str | None = None,
        handled: bool = False,
        cycle_id: str | None = None,
        message_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            generated, seq = self._next_id("messages", "M")
            mid = message_id or generated
            self._conn.execute(
                "INSERT INTO messages(id, seq, ts, direction, channel, kind, from_id, from_name, to_id, to_name,"
                " body, handled, handled_note, cycle_id, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    seq,
                    ts or config.now_iso(),
                    direction,
                    channel,
                    kind,
                    from_id,
                    from_name,
                    to_id,
                    to_name,
                    body,
                    1 if handled else 0,
                    None,
                    cycle_id,
                    _dumps(meta or {}),
                ),
            )
            self._conn.commit()
        return self.get_message(mid)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._message_row(row) if row else None

    def list_messages(
        self,
        *,
        direction: str | None = None,
        handled: bool | None = None,
        cycle_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if handled is not None:
            clauses.append("handled = ?")
            params.append(1 if handled else 0)
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        sql = "SELECT * FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [self._message_row(r) for r in rows]
        return out[-limit:] if limit else out

    def mark_message_handled(self, message_id: str, note: str = "", cycle_id: str | None = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET handled = 1, handled_note = ?, cycle_id = COALESCE(cycle_id, ?) WHERE id = ?",
                (note, cycle_id, message_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["handled"] = bool(d["handled"])
        d["meta"] = _loads(d.get("meta"), {})
        return d

    # ------------------------------------------------------------------ decisions
    def create_decision(
        self,
        *,
        kind: str,
        summary: str,
        options: list[str],
        recommendation: str | None,
        payload: dict[str, Any] | None = None,
        created_by: str | None = None,
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            did, seq = self._next_id("decisions", "D")
            self._conn.execute(
                "INSERT INTO decisions(id, seq, created_at, kind, summary, options, recommendation, payload,"
                " created_by, cycle_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,'pending')",
                (
                    did,
                    seq,
                    config.now_iso(),
                    kind,
                    summary,
                    _dumps(options),
                    recommendation,
                    _dumps(payload or {}),
                    created_by,
                    cycle_id,
                ),
            )
            self._conn.commit()
        return self.get_decision(did)  # type: ignore[return-value]

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._decision_row(row) if row else None

    def list_decisions(self, status: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM decisions", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY seq"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decision_row(r) for r in rows]

    def find_pending_decision(self, kind: str, summary: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE status = 'pending' AND kind = ? AND summary = ? ORDER BY seq LIMIT 1",
                (kind, summary),
            ).fetchone()
        return self._decision_row(row) if row else None

    def resolve_decision(self, decision_id: str, status: str, response: str, result: Any = None) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE decisions SET status = ?, response = ?, result = ?, resolved_at = ? WHERE id = ?",
                (status, response, _dumps(result), config.now_iso(), decision_id),
            )
            self._conn.commit()
        return self.get_decision(decision_id)  # type: ignore[return-value]

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["options"] = _loads(d["options"], [])
        d["payload"] = _loads(d["payload"], {})
        d["result"] = _loads(d.get("result"))
        return d

    # ------------------------------------------------------------------ audit
    def add_audit(
        self,
        *,
        cycle_id: str | None,
        agent: str | None,
        tool: str | None,
        input: dict[str, Any] | None = None,
        result: str | None = None,
        status: str = "success",
        kind: str = "tool",
    ) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit(ts, cycle_id, agent, kind, tool, input, result, status) VALUES (?,?,?,?,?,?,?,?)",
                (config.now_iso(), cycle_id, agent, kind, tool, _dumps(input or {}), result, status),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM audit WHERE seq = ?", (cur.lastrowid,)).fetchone()
        return self._audit_row(row)

    def list_audit(self, *, cycle_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM audit", []
        if cycle_id:
            sql += " WHERE cycle_id = ?"
            params.append(cycle_id)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._audit_row(r) for r in reversed(rows)]

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["input"] = _loads(d["input"], {})
        return d

    # ------------------------------------------------------------------ cycles / reports
    def start_cycle(self, kind: str, task: str) -> dict[str, Any]:
        with self._lock:
            _, seq = self._next_id("cycles", "C")
            # Suffix with a short random token so two processes that share the database
            # (for example a CLI sweep next to the web app scheduler) never collide on an id.
            cid = f"C-{seq:04d}-{secrets.token_hex(2)}"
            self._conn.execute(
                "INSERT INTO cycles(id, seq, kind, task, started_at, status) VALUES (?,?,?,?,?,'running')",
                (cid, seq, kind, task, config.now_iso()),
            )
            self._conn.commit()
        return self.get_cycle(cid)  # type: ignore[return-value]

    def finish_cycle(
        self, cycle_id: str, *, status: str, handoff_trail: list[str], summary: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE cycles SET finished_at = ?, status = ?, handoff_trail = ?, summary = ? WHERE id = ?",
                (config.now_iso(), status, _dumps(handoff_trail), summary, cycle_id),
            )
            self._conn.commit()
        return self.get_cycle(cycle_id)  # type: ignore[return-value]

    def get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        return self._cycle_row(row) if row else None

    def last_cycle(self, kind: str | None = None) -> dict[str, Any] | None:
        sql, params = "SELECT * FROM cycles", []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY seq DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return self._cycle_row(row) if row else None

    def list_cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cycles ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [self._cycle_row(r) for r in rows]

    @staticmethod
    def _cycle_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["handoff_trail"] = _loads(d["handoff_trail"], [])
        return d

    def save_report(self, cycle_id: str, report: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reports(cycle_id, ts, body) VALUES (?,?,?) "
                "ON CONFLICT(cycle_id) DO UPDATE SET ts = excluded.ts, body = excluded.body",
                (cycle_id, config.now_iso(), _dumps(report)),
            )
            self._conn.commit()

    def last_report(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM reports ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
        return _loads(row["body"]) if row else None

    # ------------------------------------------------------------------ hours / donations
    def log_hours(self, volunteer_id: str, shift_id: str | None, hours: float, note: str = "") -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO hours(ts, volunteer_id, shift_id, hours, note) VALUES (?,?,?,?,?)",
                (config.now_iso(), volunteer_id, shift_id, hours, note),
            )
            self._conn.commit()
        return {"volunteer_id": volunteer_id, "shift_id": shift_id, "hours": hours, "note": note}

    def list_hours(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM hours ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def add_donation(
        self,
        *,
        donor: str,
        items: list[dict[str, Any]],
        scheduled_dropoff: str | None,
        status: str = "scheduled",
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            did, seq = self._next_id("donations", "DN")
            self._conn.execute(
                "INSERT INTO donations(id, seq, ts, donor, items, scheduled_dropoff, status, cycle_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (did, seq, config.now_iso(), donor, _dumps(items), scheduled_dropoff, status, cycle_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM donations WHERE id = ?", (did,)).fetchone()
        return self._donation_row(row)

    def list_donations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM donations ORDER BY seq").fetchall()
        return [self._donation_row(r) for r in rows]

    @staticmethod
    def _donation_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["items"] = _loads(d["items"], [])
        return d

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _dumps(value)),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return _loads(row["value"]) if row else default

    def counts(self) -> dict[str, int]:
        """Small summary used by ``status``."""
        with self._lock:
            q = self._conn.execute
            return {
                "volunteers": q("SELECT COUNT(*) FROM docs WHERE kind='volunteer'").fetchone()[0],
                "shifts": q("SELECT COUNT(*) FROM docs WHERE kind='shift'").fetchone()[0],
                "inventory_items": q("SELECT COUNT(*) FROM docs WHERE kind='inventory'").fetchone()[0],
                "inbound_unhandled": q(
                    "SELECT COUNT(*) FROM messages WHERE direction='inbound' AND handled=0"
                ).fetchone()[0],
                "outbound": q("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0],
                "decisions_pending": q("SELECT COUNT(*) FROM decisions WHERE status='pending'").fetchone()[0],
                "decisions_resolved": q("SELECT COUNT(*) FROM decisions WHERE status!='pending'").fetchone()[0],
                "audit_entries": q("SELECT COUNT(*) FROM audit").fetchone()[0],
                "cycles": q("SELECT COUNT(*) FROM cycles").fetchone()[0],
            }


# --------------------------------------------------------------------- process-wide handle
_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Return the process-wide store (created lazily at ``config.DB_PATH``)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def bind_store(store: Store | None) -> None:
    """Replace the process-wide store (tests use this with a temporary database)."""
    global _store
    with _store_lock:
        _store = store
