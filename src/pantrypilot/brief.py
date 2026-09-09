"""Weekly brief: deterministic facts from the store plus a model-written summary (structured output)."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field
from strands import Agent
from strands.models.model import Model

from . import config, ranking
from .prompts import BRIEFER_PROMPT
from .store import Store

logger = logging.getLogger(__name__)


class WeeklyBrief(BaseModel):
    """The coordinator's weekly brief (Strands structured output model)."""

    period_start: str = Field(description="First day covered, YYYY-MM-DD")
    period_end: str = Field(description="Last day covered, YYYY-MM-DD")
    coverage_pct: float = Field(description="Percent of needed volunteer spots filled in the next 7 days")
    open_slots: list[str] = Field(default_factory=list, description="Open slots as 'when title: filled/needed'")
    volunteer_hours_logged: float = Field(default=0, description="Hours logged since seeding")
    donations_received: list[str] = Field(default_factory=list, description="Donations logged (pledged or received)")
    items_below_par: list[str] = Field(default_factory=list, description="Items below par with shortfall")
    expiring_soon: list[str] = Field(default_factory=list, description="Items expiring within 7 days")
    escalations_pending: int = 0
    escalations_resolved: int = 0
    thank_you: list[str] = Field(default_factory=list, description="Volunteers who covered last-minute")
    summary: str = Field(default="", description="2-3 plain sentences for the coordinator")
    coordinator_actions: list[str] = Field(default_factory=list, description="Short imperatives only she can do")


def compute_facts(store: Store) -> dict[str, Any]:
    """Compute every number in the brief deterministically from the store."""
    today = config.today()
    end = today + timedelta(days=config.LOOKAHEAD_DAYS)
    volunteers = {v["id"]: v for v in store.volunteers()}
    needed = filled = 0
    open_slots: list[str] = []
    thank_you: set[str] = set()
    for sh in store.shifts():
        d = date.fromisoformat(sh["date"])
        if not (today <= d <= end):
            continue
        n = int(sh.get("needed", 0))
        f = min(n, len(ranking.roster_ids(sh)))
        needed += n
        filled += f
        if f < n:
            when = f"{d.strftime('%a %b %d').replace(' 0', ' ')} {sh['start']}"
            open_slots.append(f"{when} {sh.get('title')}: {f}/{n}")
        # assign_volunteer tags entries added after a cancellation as last_minute.
        for entry in sh.get("roster", []):
            if entry.get("last_minute") and entry.get("status") == "confirmed":
                v = volunteers.get(entry["volunteer_id"])
                if v:
                    thank_you.add(v["name"])

    hours = sum(float(h["hours"]) for h in store.list_hours())
    donations = [
        f"{d['donor']}: "
        + ", ".join(f"{i.get('qty')} {i.get('unit')} {i.get('item')}" for i in d["items"])
        + f" ({d['status']}{', ' + d['scheduled_dropoff'] if d.get('scheduled_dropoff') else ''})"
        for d in store.list_donations()
    ]
    below = [
        f"{i['item']}: {i['qty']}/{i['par']} {i['unit']} (short {i['par'] - i['qty']})"
        for i in store.inventory()
        if i["qty"] < i["par"]
    ]
    expiring = []
    for i in store.inventory():
        if i.get("expiry"):
            days = (date.fromisoformat(i["expiry"]) - today).days
            if days <= config.LOOKAHEAD_DAYS:
                expiring.append(f"{i['item']}: {i['qty']} {i['unit']} expire in {days} day(s) ({i['expiry']})")
    pending = store.list_decisions(status="pending")
    resolved = store.list_decisions(status="resolved")
    return {
        "period_start": today.isoformat(),
        "period_end": end.isoformat(),
        "coverage_pct": round(100.0 * filled / needed, 1) if needed else 100.0,
        "open_slots": open_slots,
        "volunteer_hours_logged": hours,
        "donations_received": donations,
        "items_below_par": below,
        "expiring_soon": expiring,
        "escalations_pending": len(pending),
        "escalations_resolved": len(resolved),
        "thank_you": sorted(thank_you),
        "pending_summaries": [f"{d['id']}: {d['summary']}" for d in pending],
    }


def fallback_brief(facts: dict[str, Any]) -> WeeklyBrief:
    pending = facts.get("escalations_pending", 0)
    summary = (
        f"Coverage for the next 7 days is {facts['coverage_pct']}% with {len(facts['open_slots'])} open slot(s). "
        f"{len(facts['items_below_par'])} item(s) are below par and {len(facts['expiring_soon'])} expire soon. "
        + (f"{pending} decision(s) are waiting for you." if pending else "Nothing is waiting on you.")
    )
    actions = [f"Answer: {p}" for p in facts.get("pending_summaries", [])]
    data = {k: v for k, v in facts.items() if k in WeeklyBrief.model_fields}
    return WeeklyBrief(**data, summary=summary, coordinator_actions=actions)


def generate_brief(store: Store, model: Model, cycle_id: str | None = None) -> WeeklyBrief:
    """Ask the ``briefer`` agent for a WeeklyBrief via structured output; fall back to computed facts."""
    facts = compute_facts(store)
    try:
        agent = Agent(
            model=model,
            name="briefer",
            description="Writes the weekly brief from computed facts.",
            system_prompt=BRIEFER_PROMPT,
            callback_handler=None,
            state={"cycle_id": cycle_id},
        )
        prompt = "Facts:\n" + json.dumps(facts, indent=2, default=str) + "\n\nProduce the WeeklyBrief."
        result = agent(prompt, structured_output_model=WeeklyBrief)
        brief = result.structured_output
        if isinstance(brief, WeeklyBrief):
            # Numbers are authoritative from the store; only prose comes from the model.
            merged = brief.model_dump()
            for key in (
                "period_start",
                "period_end",
                "coverage_pct",
                "open_slots",
                "volunteer_hours_logged",
                "donations_received",
                "items_below_par",
                "expiring_soon",
                "escalations_pending",
                "escalations_resolved",
            ):
                merged[key] = facts[key]
            if not merged.get("thank_you"):
                merged["thank_you"] = facts["thank_you"]
            return WeeklyBrief(**merged)
    except Exception:
        logger.exception("briefer failed; using computed brief")
    return fallback_brief(facts)
