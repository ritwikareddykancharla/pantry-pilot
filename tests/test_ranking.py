"""Candidate ranking: skills, availability, fairness, minor rule, reliability tiebreak."""

from __future__ import annotations

from datetime import date, datetime

from pantrypilot import ranking
from pantrypilot.store import Store

TODAY = date(2026, 9, 11)


def _rules(store: Store) -> dict:
    return store.org()["rules"]


def test_skills_and_availability_filter(store: Store) -> None:
    delivery = store.shift("S-0917-DELIVERY")  # Thu 16:00-18:00, needs driver
    ranked = ranking.rank_candidates(delivery, store.volunteers(), TODAY, _rules(store))
    ids = [c["volunteer_id"] for c in ranked]
    # Drivers available Thursday evening: Jorge (V-04), Tom (V-07), Luis (V-09). Ravi drives but is Saturday-only.
    assert set(ids) == {"V-04", "V-07", "V-09"}
    assert "V-13" not in ids
    for c in ranked:
        assert "driver" in c["skills"]


def test_fairness_orders_fewest_recent_shifts_first(store: Store) -> None:
    intake = store.shift("S-0912-INTAKE")
    intake["roster"] = [r for r in intake["roster"] if r["volunteer_id"] != "V-01"]  # Dana cancelled
    ranked = ranking.rank_candidates(intake, store.volunteers(), TODAY, _rules(store))
    ids = [c["volunteer_id"] for c in ranked]
    # Priya (1 shift, 27 days ago) ranks ahead of Marcus (3 shifts in the last 30 days).
    assert ids.index("V-02") < ids.index("V-03")
    priya = next(c for c in ranked if c["volunteer_id"] == "V-02")
    marcus = next(c for c in ranked if c["volunteer_id"] == "V-03")
    assert priya["recent_shifts_30d"] == 1 and marcus["recent_shifts_30d"] == 3
    # Already-rostered volunteers are excluded.
    assert "V-10" not in ids and "V-14" not in ids


def test_minor_needs_supervisor_flag(store: Store) -> None:
    tue = store.shift("S-0915-EVE")  # only Grace confirmed, no supervisor
    tue["required_skills"] = ["sorting"]
    ranked = ranking.rank_candidates(tue, store.volunteers(), TODAY, _rules(store))
    sam = next(c for c in ranked if c["volunteer_id"] == "V-05")
    assert sam["minor"] is True and sam["needs_supervisor"] is True and sam["eligible_now"] is False
    assert ranked[-1]["volunteer_id"] == "V-05"  # sorted last
    # Once a supervisor is confirmed on the shift, Sam becomes eligible.
    tue["roster"].append({"volunteer_id": "V-06", "status": "confirmed"})
    ranked2 = ranking.rank_candidates(tue, store.volunteers(), TODAY, _rules(store))
    sam2 = next(c for c in ranked2 if c["volunteer_id"] == "V-05")
    assert sam2["needs_supervisor"] is False and sam2["eligible_now"] is True


def test_reliability_tiebreak() -> None:
    shift = {
        "id": "X",
        "date": "2026-09-12",
        "start": "09:00",
        "end": "13:00",
        "required_skills": ["intake"],
        "needed": 1,
        "roster": [],
    }
    window = [{"day": "sat", "start": "09:00", "end": "13:00"}]
    a = {
        "id": "A",
        "name": "A",
        "skills": ["intake"],
        "availability": window,
        "reliability": 0.7,
        "recent_shifts": ["2026-09-01"],
    }
    b = {
        "id": "B",
        "name": "B",
        "skills": ["intake"],
        "availability": window,
        "reliability": 0.95,
        "recent_shifts": ["2026-09-01"],
    }
    ranked = ranking.rank_candidates(shift, [a, b], TODAY, {})
    assert [c["volunteer_id"] for c in ranked] == ["B", "A"]
    assert ranked[0]["rank"] == 1


def test_helpers_quiet_hours_and_hours_until(store: Store) -> None:
    quiet = {"start": "21:00", "end": "08:00"}
    assert ranking.in_quiet_hours(datetime(2026, 9, 11, 22, 30), quiet)
    assert ranking.in_quiet_hours(datetime(2026, 9, 11, 7, 59), quiet)
    assert not ranking.in_quiet_hours(datetime(2026, 9, 11, 12, 0), quiet)
    assert not ranking.in_quiet_hours(datetime(2026, 9, 11, 12, 0), None)
    sat = store.shift("S-0912-INTAKE")
    assert 0 < ranking.hours_until(sat, datetime(2026, 9, 11, 9, 0)) <= 24
    assert ranking.shift_open_count(sat) == 0
    assert ranking.shift_has_supervisor(sat, {v["id"]: v for v in store.volunteers()})  # Maria is a supervisor
