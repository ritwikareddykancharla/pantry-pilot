"""Pure functions for scheduling logic: skills, availability, fairness, safety rules.

Everything here is deterministic and unit-tested; the ``roster`` agent calls it through
``find_candidates`` and ``assign_volunteer`` so the model never has to do the arithmetic.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from . import config

RECENT_WINDOW_DAYS = 30


def is_minor(volunteer: dict[str, Any], minor_age: int = 18) -> bool:
    """True if the volunteer is flagged ``minor`` or has an ``age`` below ``minor_age``."""
    if volunteer.get("minor"):
        return True
    age = volunteer.get("age")
    return isinstance(age, int) and age < minor_age


def has_skills(volunteer: dict[str, Any], required: list[str]) -> bool:
    return set(required or []).issubset(set(volunteer.get("skills", [])))


def is_available(volunteer: dict[str, Any], shift: dict[str, Any]) -> bool:
    """True if one of the volunteer's weekly windows fully covers the shift."""
    shift_day = config.day_key(date.fromisoformat(shift["date"]))
    start, end = config.parse_hhmm(shift["start"]), config.parse_hhmm(shift["end"])
    for window in volunteer.get("availability", []):
        if window.get("day") != shift_day:
            continue
        if config.parse_hhmm(window["start"]) <= start and config.parse_hhmm(window["end"]) >= end:
            return True
    return False


def recent_shift_count(volunteer: dict[str, Any], today: date, window_days: int = RECENT_WINDOW_DAYS) -> int:
    count = 0
    for raw in volunteer.get("recent_shifts", []):
        d = date.fromisoformat(raw)
        if 0 <= (today - d).days <= window_days:
            count += 1
    return count


def days_since_last_shift(volunteer: dict[str, Any], today: date) -> int:
    dates = [date.fromisoformat(r) for r in volunteer.get("recent_shifts", [])]
    past = [d for d in dates if d <= today]
    if not past:
        return 999
    return (today - max(past)).days


def roster_ids(shift: dict[str, Any], statuses: tuple[str, ...] = ("confirmed", "tentative")) -> list[str]:
    return [r["volunteer_id"] for r in shift.get("roster", []) if r.get("status") in statuses]


def shift_has_supervisor(shift: dict[str, Any], volunteers_by_id: dict[str, dict[str, Any]]) -> bool:
    """True if a confirmed roster member has the ``supervisor`` skill."""
    for vid in roster_ids(shift, ("confirmed",)):
        v = volunteers_by_id.get(vid)
        if v and "supervisor" in v.get("skills", []):
            return True
    return False


def shift_open_count(shift: dict[str, Any]) -> int:
    return max(0, int(shift.get("needed", 0)) - len(roster_ids(shift)))


def shift_starts_at(shift: dict[str, Any]) -> datetime:
    return datetime.combine(date.fromisoformat(shift["date"]), config.parse_hhmm(shift["start"]))


def hours_until(shift: dict[str, Any], now: datetime | None = None) -> float:
    now = now or config.now()
    return round((shift_starts_at(shift) - now).total_seconds() / 3600, 1)


def rank_candidates(
    shift: dict[str, Any],
    volunteers: list[dict[str, Any]],
    today: date,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank volunteers for a slot.

    Filters: not already rostered, has every required skill, available for the whole window.
    Ordering: eligible first; then fewest shifts in the last 30 days (fairness); then higher
    reliability; then longest time since last shift. Minors on a shift without a confirmed
    supervisor are kept in the list but flagged ``needs_supervisor`` and sorted last, so the
    agent can see them and act (ask a supervisor or escalate) rather than silently drop them.
    """
    rules = rules or {}
    minor_age = int(rules.get("minor_age", 18))
    minors_need_supervisor = bool(rules.get("minors_need_supervisor", True))
    by_id = {v["id"]: v for v in volunteers}
    supervisor_present = shift_has_supervisor(shift, by_id)
    already = set(roster_ids(shift))

    ranked: list[dict[str, Any]] = []
    for v in volunteers:
        if v["id"] in already:
            continue
        if not has_skills(v, shift.get("required_skills", [])):
            continue
        if not is_available(v, shift):
            continue
        minor = is_minor(v, minor_age)
        needs_supervisor = minor and minors_need_supervisor and not supervisor_present
        recent = recent_shift_count(v, today)
        since = days_since_last_shift(v, today)
        reasons = [f"{recent} shift(s) in last 30 days", f"reliability {v.get('reliability', 0):.2f}"]
        if since < 999:
            reasons.append(f"last shift {since} days ago")
        else:
            reasons.append("no recent shifts")
        if needs_supervisor:
            reasons.append("minor: needs a confirmed supervisor on this shift first")
        ranked.append(
            {
                "volunteer_id": v["id"],
                "name": v["name"],
                "skills": v.get("skills", []),
                "reliability": v.get("reliability", 0),
                "recent_shifts_30d": recent,
                "days_since_last_shift": since,
                "minor": minor,
                "needs_supervisor": needs_supervisor,
                "eligible_now": not needs_supervisor,
                "preferred_contact": v.get("preferred_contact", "sms"),
                "why": "; ".join(reasons),
            }
        )

    ranked.sort(
        key=lambda c: (
            c["needs_supervisor"],
            c["recent_shifts_30d"],
            -float(c["reliability"]),
            -c["days_since_last_shift"],
        )
    )
    for i, c in enumerate(ranked, start=1):
        c["rank"] = i
    return ranked


def in_quiet_hours(now: datetime, quiet: dict[str, str] | None) -> bool:
    """True if ``now`` falls inside the org's quiet hours (which may wrap midnight)."""
    if not quiet:
        return False
    start, end = config.parse_hhmm(quiet["start"]), config.parse_hhmm(quiet["end"])
    t: time = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end
