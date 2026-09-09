"""Helpers shared by tool modules: store access, cycle id, recipient lookup, formatting."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .. import config, ranking
from ..store import Store, get_store


def store() -> Store:
    return get_store()


def cycle_id_of(agent: Any) -> str | None:
    """Read the current cycle id from the invoking agent's state (set when agents are built)."""
    if agent is None:
        return None
    try:
        return agent.state.get("cycle_id")
    except Exception:  # pragma: no cover - defensive
        return None


def is_approved(agent: Any) -> bool:
    if agent is None:
        return False
    try:
        return bool(agent.state.get("approved"))
    except Exception:  # pragma: no cover - defensive
        return False


def agent_name(agent: Any) -> str:
    return getattr(agent, "name", None) or "agent"


def pretty_date(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%a %b %d").replace(" 0", " ")


def pretty_time(hhmm: str) -> str:
    t = config.parse_hhmm(hhmm)
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d}{suffix}" if t.minute else f"{hour}{suffix}"


def resolve_person(handle: str | None) -> dict[str, Any] | None:
    """Find a volunteer or contact by id, exact name, or case-insensitive name/first-name match."""
    if not handle:
        return None
    s = store()
    org = s.org()
    coordinator = org.get("coordinator", {})
    if handle in (coordinator.get("contact_id", "coordinator"), "coordinator"):
        return {"id": "coordinator", "name": coordinator.get("name", "Coordinator"), "type": "coordinator"}
    for kind in ("volunteer", "contact"):
        doc = s.get_doc(kind, handle)
        if doc:
            return {**doc, "type": kind}
    needle = handle.strip().lower()
    people = [{**v, "type": "volunteer"} for v in s.volunteers()] + [{**c, "type": "contact"} for c in s.contacts()]
    for p in people:
        if p["name"].lower() == needle or p.get("phone") == handle:
            return p
    for p in people:
        if p["name"].lower().split()[0] == needle or needle in p["name"].lower():
            return p
    for p in people:
        if p["type"] == "contact" and p.get("org", "").lower() in needle:
            return p
    return None


def shift_view(shift: dict[str, Any], volunteers_by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compact, model-friendly view of a shift with names and statuses."""
    s = store()
    by_id = volunteers_by_id or {v["id"]: v for v in s.volunteers()}
    roster = []
    minors: list[str] = []
    for entry in shift.get("roster", []):
        v = by_id.get(entry["volunteer_id"], {})
        name = v.get("name", entry["volunteer_id"])
        roster.append({"volunteer_id": entry["volunteer_id"], "name": name, "status": entry.get("status")})
        if ranking.is_minor(v):
            minors.append(name)
    filled = len(ranking.roster_ids(shift))
    return {
        "slot_id": shift["id"],
        "title": shift.get("title"),
        "date": shift["date"],
        "day": pretty_date(shift["date"]),
        "start": shift["start"],
        "end": shift["end"],
        "when": f"{pretty_date(shift['date'])} {pretty_time(shift['start'])}-{pretty_time(shift['end'])}",
        "role": shift.get("role"),
        "required_skills": shift.get("required_skills", []),
        "needed": shift.get("needed", 0),
        "filled": filled,
        "open": max(0, int(shift.get("needed", 0)) - filled),
        "hours_until_start": ranking.hours_until(shift),
        "supervisor_confirmed": ranking.shift_has_supervisor(shift, by_id),
        "minors_on_roster": minors,
        "roster": roster,
        "asks_sent": shift.get("asks", []),
        "ask_rounds": len(shift.get("asks", [])),
        "reminder_sent": bool(shift.get("reminder_sent")),
        "notes": shift.get("notes", ""),
    }


def shifts_in_window(days: int) -> list[dict[str, Any]]:
    today = config.today()
    out = []
    for sh in store().shifts():
        d = date.fromisoformat(sh["date"])
        if 0 <= (d - today).days <= days:
            out.append(sh)
    return out


def now() -> datetime:
    return config.now()
