"""Roster tools: schedule, open slots, candidate ranking, assignments, availability, hours."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from strands import tool

from .. import config, ranking
from . import _ctx
from .common import _deliver


@tool
def get_schedule(days: int = 7) -> dict[str, Any]:
    """Return every shift in the next N days with who is on it and how many are still needed.

    Args:
        days: Horizon in days from today (default 7, max 14).

    Returns:
        shifts: list of slots with slot_id, when, role, needed/filled/open, roster (names and
        confirmed/tentative status), supervisor_confirmed, minors_on_roster, ask_rounds.
    """
    days = max(0, min(int(days), 14))
    by_id = {v["id"]: v for v in _ctx.store().volunteers()}
    shifts = [_ctx.shift_view(sh, by_id) for sh in _ctx.shifts_in_window(days)]
    return {"today": config.today().isoformat(), "days": days, "count": len(shifts), "shifts": shifts}


@tool
def get_open_slots(days: int = 7) -> dict[str, Any]:
    """Return only the shifts in the next N days that still need people.

    Args:
        days: Horizon in days (default 7).

    Returns:
        open_slots: same shape as get_schedule, filtered to open > 0, soonest first, with an
        "urgent" flag when the shift starts within 24 hours.
    """
    days = max(0, min(int(days), 14))
    by_id = {v["id"]: v for v in _ctx.store().volunteers()}
    out = []
    for sh in _ctx.shifts_in_window(days):
        view = _ctx.shift_view(sh, by_id)
        if view["open"] > 0:
            view["urgent"] = view["hours_until_start"] <= 24
            out.append(view)
    return {"count": len(out), "open_slots": out}


@tool
def get_volunteer(volunteer_id: str) -> dict[str, Any]:
    """Look up one volunteer by id or name: skills, availability, reliability, notes, upcoming shifts.

    Args:
        volunteer_id: Volunteer id such as "V-02" or a name such as "Priya".

    Returns:
        The volunteer record (phone omitted) plus is_minor, recent_shifts_30d, upcoming.
    """
    person = _ctx.resolve_person(volunteer_id)
    if not person or person.get("type") != "volunteer":
        return {"ok": False, "error": f"No volunteer matching '{volunteer_id}'"}
    v = dict(person)
    v.pop("phone", None)
    v.pop("type", None)
    upcoming = []
    for sh in _ctx.shifts_in_window(14):
        for entry in sh.get("roster", []):
            if entry["volunteer_id"] == v["id"]:
                upcoming.append(
                    {"slot_id": sh["id"], "title": sh.get("title"), "date": sh["date"], "status": entry["status"]}
                )
    v["is_minor"] = ranking.is_minor(v)
    v["recent_shifts_30d"] = ranking.recent_shift_count(v, config.today())
    v["upcoming"] = upcoming
    return v


@tool
def find_candidates(slot_id: str, limit: int = 5) -> dict[str, Any]:
    """Rank volunteers who could fill an open slot.

    Filters by required skills and weekly availability, then orders by fairness (fewest shifts
    in the last 30 days), reliability, and time since last shift. Minors are flagged
    needs_supervisor when the shift has no confirmed supervisor; do not confirm them until one
    is confirmed. Use the ranking to decide who to ask first and to justify recommendations.

    Args:
        slot_id: The shift id, e.g. "S-0913-INTAKE".
        limit: Max candidates to return (default 5).

    Returns:
        slot summary, candidates (rank, volunteer_id, name, why, eligible_now), and a note.
    """
    s = _ctx.store()
    shift = s.shift(slot_id)
    if not shift:
        return {"ok": False, "error": f"No shift {slot_id}"}
    rules = s.org().get("rules") or {}
    ranked = ranking.rank_candidates(shift, s.volunteers(), config.today(), rules)
    return {
        "slot": _ctx.shift_view(shift),
        "candidates": ranked[: max(1, int(limit))],
        "note": "Ask one at a time, best-ranked first; wait for a yes before confirming.",
    }


def _mentions_shift(body: str, shift: dict[str, Any]) -> bool:
    """True if a message text refers to the shift's day (weekday name, "tomorrow", date or slot id)."""
    text = body.lower()
    day = date.fromisoformat(shift["date"])
    tokens = {shift["id"].lower(), shift["date"], day.strftime("%A").lower(), day.strftime("%a").lower()}
    if day == config.today() + timedelta(days=1):
        tokens.add("tomorrow")
    if day == config.today():
        tokens.add("today")
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens)


def _contesting_volunteers(
    s: Any, shift: dict[str, Any], assignee_id: str, by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Other volunteers with an unhandled inbound message who could also take this slot.

    Used to stop the model from quietly picking one of two people who both wrote in for the
    same last spot; that call belongs to the coordinator. People already on the roster or just
    removed from it (the cancellation that opened the spot) are not rivals.
    """
    removed = {c.get("removed") for c in shift.get("changes", [])}
    wrote_in = {
        m.get("from_id")
        for m in s.list_messages(direction="inbound", handled=False)
        if _mentions_shift(m.get("body", ""), shift)
    }
    rules = s.org().get("rules") or {}
    eligible = {
        c["volunteer_id"]
        for c in ranking.rank_candidates(shift, list(by_id.values()), config.today(), rules)
        if c["eligible_now"]
    }
    return [by_id[vid] for vid in sorted(eligible & wrote_in) if vid != assignee_id and vid not in removed]


@tool
def assign_volunteer(slot_id: str, volunteer_id: str, status: str = "tentative", agent: Any = None) -> dict[str, Any]:
    """Put a volunteer on a shift as "tentative" (asked / offered) or "confirmed" (they said yes).

    Rules enforced here: the volunteer must have the required skills; the slot must have room
    (if it is full and someone else also wants it, escalate instead); when exactly one spot is
    open and another qualified, available volunteer also has an unhandled message this cycle,
    the spot is contested and this tool refuses so you escalate instead of choosing; a minor
    cannot be confirmed unless a supervisor is confirmed on the same shift (tentative is
    allowed so you can hold the spot while you find a supervisor or escalate).

    Args:
        slot_id: Shift id such as "S-0918-DELIVERY".
        volunteer_id: Volunteer id such as "V-04".
        status: "tentative" or "confirmed".

    Returns:
        ok, the updated slot view, and warnings (e.g. minor without supervisor).
    """
    s = _ctx.store()
    shift = s.shift(slot_id)
    if not shift:
        return {"ok": False, "error": f"No shift {slot_id}"}
    person = _ctx.resolve_person(volunteer_id)
    if not person or person.get("type") != "volunteer":
        return {"ok": False, "error": f"No volunteer matching '{volunteer_id}'"}
    status = status if status in ("tentative", "confirmed") else "tentative"
    rules = s.org().get("rules") or {}
    warnings: list[str] = []

    if not ranking.has_skills(person, shift.get("required_skills", [])):
        return {
            "ok": False,
            "error": (
                f"{person['name']} lacks required skills {shift.get('required_skills')} (has {person.get('skills')})"
            ),
        }
    by_id = {v["id"]: v for v in s.volunteers()}
    existing = next((r for r in shift.get("roster", []) if r["volunteer_id"] == person["id"]), None)
    if existing is None and ranking.shift_open_count(shift) <= 0:
        return {
            "ok": False,
            "error": "Slot is already full. If two people want the same last spot, escalate to the coordinator.",
            "slot": _ctx.shift_view(shift, by_id),
        }
    approved = bool(getattr(getattr(agent, "state", None), "get", lambda *_: False)("approved"))
    if existing is None and not approved and ranking.shift_open_count(shift) == 1:
        rivals = _contesting_volunteers(s, shift, person["id"], by_id)
        if rivals:
            names = ", ".join(f"{r['name']} ({r['id']})" for r in rivals)
            return {
                "ok": False,
                "error": (
                    f"Contested last spot: {names} also wrote in this cycle and is qualified and available for "
                    f"{slot_id}. Do not choose. escalate_to_coordinator with both names and the fairness data from "
                    "find_candidates, tell both you are checking with Aisha, and mark their messages handled. "
                    "If the coordinator has already decided, mark the other person's message handled first, "
                    "then assign."
                ),
                "contested_by": [r["id"] for r in rivals],
                "slot": _ctx.shift_view(shift, by_id),
            }
    if ranking.is_minor(person, int(rules.get("minor_age", 18))) and rules.get("minors_need_supervisor", True):
        if not ranking.shift_has_supervisor(shift, by_id):
            if status == "confirmed":
                return {
                    "ok": False,
                    "error": (
                        f"{person['name']} is a minor and no supervisor is confirmed on {slot_id}. "
                        "Assign as tentative, then ask a supervisor-skilled volunteer or escalate."
                    ),
                    "slot": _ctx.shift_view(shift, by_id),
                }
            warnings.append("Minor assigned tentatively: a confirmed supervisor is required before confirming.")
    if not ranking.is_available(person, shift):
        warnings.append("Volunteer's usual availability does not cover this shift; confirm they really can make it.")

    if existing:
        existing["status"] = status
    else:
        entry: dict[str, Any] = {"volunteer_id": person["id"], "status": status}
        if shift.get("changes") or ranking.hours_until(shift) <= 48:
            entry["last_minute"] = True  # covered after a cancellation or on short notice: thank them
        shift.setdefault("roster", []).append(entry)
    s.put_doc("shift", shift["id"], shift)
    return {
        "ok": True,
        "assigned": person["name"],
        "status": status,
        "slot": _ctx.shift_view(shift, by_id),
        "warnings": warnings,
    }


@tool
def unassign_volunteer(slot_id: str, volunteer_id: str, reason: str, agent: Any = None) -> dict[str, Any]:
    """Remove a volunteer from a shift (cancellation, swap, or no-show), recording why.

    Args:
        slot_id: Shift id.
        volunteer_id: Volunteer id or name.
        reason: Short reason, e.g. "cancelled: kid sick".

    Returns:
        ok, the updated slot view (with new open count) and hours_until_start.
    """
    s = _ctx.store()
    shift = s.shift(slot_id)
    if not shift:
        return {"ok": False, "error": f"No shift {slot_id}"}
    person = _ctx.resolve_person(volunteer_id)
    if not person:
        return {"ok": False, "error": f"No volunteer matching '{volunteer_id}'"}
    before = len(shift.get("roster", []))
    shift["roster"] = [r for r in shift.get("roster", []) if r["volunteer_id"] != person["id"]]
    if len(shift["roster"]) == before:
        return {"ok": False, "error": f"{person['name']} is not on {slot_id}", "slot": _ctx.shift_view(shift)}
    shift.setdefault("changes", []).append({"ts": config.now_iso(), "removed": person["id"], "reason": reason})
    s.put_doc("shift", shift["id"], shift)
    return {"ok": True, "removed": person["name"], "reason": reason, "slot": _ctx.shift_view(shift)}


@tool
def record_availability(volunteer_id: str, windows: list[dict[str, str]], agent: Any = None) -> dict[str, Any]:
    """Update a volunteer's weekly availability (replaces their current windows).

    Args:
        volunteer_id: Volunteer id or name.
        windows: List of {"day": "tue", "start": "16:00", "end": "19:00"} entries; day is a
            three-letter lowercase weekday.

    Returns:
        ok and the saved windows.
    """
    s = _ctx.store()
    person = _ctx.resolve_person(volunteer_id)
    if not person or person.get("type") != "volunteer":
        return {"ok": False, "error": f"No volunteer matching '{volunteer_id}'"}
    clean = []
    for w in windows:
        day = str(w.get("day", "")).lower()[:3]
        if day not in config.DAY_KEYS:
            return {"ok": False, "error": f"Bad day '{w.get('day')}'; use mon..sun"}
        try:
            config.parse_hhmm(w["start"])
            config.parse_hhmm(w["end"])
        except (KeyError, ValueError):
            return {"ok": False, "error": f"Bad window {w}; use HH:MM"}
        clean.append({"day": day, "start": w["start"], "end": w["end"]})
    volunteer = s.volunteer(person["id"])
    assert volunteer is not None
    volunteer["availability"] = clean
    s.put_doc("volunteer", volunteer["id"], volunteer)
    return {"ok": True, "volunteer": volunteer["name"], "availability": clean}


@tool
def send_shift_reminders(slot_id: str, agent: Any = None) -> dict[str, Any]:
    """Send the day-before reminder to everyone rostered on a shift (once per shift).

    Use during the daily checklist for every shift happening tomorrow. Skips shifts that
    were already reminded. Tentative volunteers are asked to confirm.

    Args:
        slot_id: Shift id.

    Returns:
        ok, number of reminders sent, and message ids.
    """
    s = _ctx.store()
    shift = s.shift(slot_id)
    if not shift:
        return {"ok": False, "error": f"No shift {slot_id}"}
    if shift.get("reminder_sent"):
        return {"ok": True, "sent": 0, "note": "Reminders were already sent for this shift."}
    by_id = {v["id"]: v for v in s.volunteers()}
    sent = []
    when = f"{_ctx.pretty_date(shift['date'])} {_ctx.pretty_time(shift['start'])}-{_ctx.pretty_time(shift['end'])}"
    for entry in shift.get("roster", []):
        v = by_id.get(entry["volunteer_id"])
        if not v:
            continue
        first = v["name"].split()[0]
        if entry.get("status") == "confirmed":
            body = (
                f"Hi {first}, reminder: {shift.get('title')} {when} at Maple Street. "
                "See you there. Pantry Pilot (for Aisha)"
            )
        else:
            body = (
                f"Hi {first}, you are down as tentative for {shift.get('title')} {when}. "
                "Can you confirm with a quick yes? Pantry Pilot (for Aisha)"
            )
        sent.append(_deliver(to=v, body=body, kind="reminder", agent=agent)["message_id"])
    shift["reminder_sent"] = True
    s.put_doc("shift", shift["id"], shift)
    return {"ok": True, "sent": len(sent), "message_ids": sent}


@tool
def log_hours(volunteer_id: str, shift_id: str, hours: float, note: str = "", agent: Any = None) -> dict[str, Any]:
    """Log volunteer hours for a completed shift (feeds the weekly brief and fairness).

    Args:
        volunteer_id: Volunteer id or name.
        shift_id: Shift id the hours belong to.
        hours: Hours worked (e.g. 4).
        note: Optional note.

    Returns:
        ok and the saved record.
    """
    s = _ctx.store()
    person = _ctx.resolve_person(volunteer_id)
    if not person or person.get("type") != "volunteer":
        return {"ok": False, "error": f"No volunteer matching '{volunteer_id}'"}
    rec = s.log_hours(person["id"], shift_id, float(hours), note)
    volunteer = s.volunteer(person["id"])
    if volunteer is not None:
        shift = s.shift(shift_id)
        worked = shift["date"] if shift else config.today().isoformat()
        if worked not in volunteer.get("recent_shifts", []):
            volunteer.setdefault("recent_shifts", []).append(worked)
            s.put_doc("volunteer", volunteer["id"], volunteer)
    return {"ok": True, **rec, "volunteer": person["name"]}


def next_day_shifts() -> list[dict[str, Any]]:
    """Shifts happening tomorrow (used by prompts/tests)."""
    tomorrow = (config.today() + timedelta(days=1)).isoformat()
    return [sh for sh in _ctx.store().shifts() if sh["date"] == tomorrow]
