"""Tools shared by the dispatcher and the specialists: inbox, messaging, escalation, rules."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from strands import tool

from .. import config, ranking
from . import _ctx


@tool
def today() -> dict[str, Any]:
    """Return the current demo date/time and the 7-day planning horizon.

    Call this first in a cycle so day names ("tomorrow", "Saturday") resolve correctly.

    Returns:
        date (YYYY-MM-DD), weekday, now (ISO), tomorrow, horizon_end, and whether it is
        currently quiet hours (no volunteer texts allowed).
    """
    org = _ctx.store().org()
    now = config.now()
    d = config.today()
    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "now": now.isoformat(timespec="minutes"),
        "tomorrow": (d + timedelta(days=1)).isoformat(),
        "horizon_end": (d + timedelta(days=config.LOOKAHEAD_DAYS)).isoformat(),
        "quiet_hours_now": ranking.in_quiet_hours(now, (org.get("rules") or {}).get("quiet_hours")),
    }


@tool
def get_org_rules() -> dict[str, Any]:
    """Return the pantry's profile and operating rules.

    Use this to check safety rules (minors, forklift), approval gates (broadcasts, purchases),
    open hours for drop-offs, storage capacity, quiet hours, and the message tone guide.

    Returns:
        name, coordinator, open_hours, storage, rules (thresholds + plain-language text), tone.
    """
    org = _ctx.store().org()
    return {
        "name": org.get("name"),
        "coordinator": {k: v for k, v in (org.get("coordinator") or {}).items() if k != "phone"},
        "open_hours": org.get("open_hours", []),
        "storage": org.get("storage", {}),
        "rules": org.get("rules", {}),
        "tone": org.get("tone", []),
    }


@tool
def list_inbound_messages(include_handled: bool = False) -> dict[str, Any]:
    """List inbound messages from volunteers, donors, partners and the coordinator.

    Messages with from_id "coordinator" are the coordinator's decisions on earlier
    escalations: carry them out exactly, then notify the people affected.

    Args:
        include_handled: Also return messages already marked handled (default False).

    Returns:
        count and a list of messages: id, ts, from_id, from_name, from_type, text, decision_id.
    """
    s = _ctx.store()
    msgs = s.list_messages(direction="inbound", handled=None if include_handled else False)
    out = []
    for m in msgs:
        person = _ctx.resolve_person(m.get("from_id")) or {}
        out.append(
            {
                "id": m["id"],
                "ts": m["ts"],
                "from_id": m.get("from_id"),
                "from_name": m.get("from_name") or person.get("name"),
                "from_type": person.get("type") or ("coordinator" if m.get("from_id") == "coordinator" else "unknown"),
                "kind": m.get("kind"),
                "text": m["body"],
                "handled": m["handled"],
                "decision_id": (m.get("meta") or {}).get("decision_id"),
            }
        )
    return {"count": len(out), "messages": out}


@tool
def mark_message_handled(message_id: str, note: str, agent: Any = None) -> dict[str, Any]:
    """Mark an inbound message as handled, with a one-line note of what was done.

    Call this once per message after the reply/assignment/escalation for it is complete.

    Args:
        message_id: The inbound message id (e.g. "M-003").
        note: What was done, e.g. "Assigned Jorge to S-0918-DELIVERY (confirmed) and replied".

    Returns:
        ok flag and the message id.
    """
    s = _ctx.store()
    ok = s.mark_message_handled(message_id, note, cycle_id=_ctx.cycle_id_of(agent))
    if not ok:
        return {"ok": False, "error": f"No message with id {message_id}"}
    return {"ok": True, "message_id": message_id, "note": note}


def _deliver(*, to: dict[str, Any], body: str, kind: str, agent: Any, channel: str | None = None) -> dict[str, Any]:
    s = _ctx.store()
    org = s.org()
    now = config.now()
    quiet = (org.get("rules") or {}).get("quiet_hours")
    status = "queued_until_morning" if ranking.in_quiet_hours(now, quiet) else "sent"
    msg = s.add_message(
        direction="outbound",
        body=body.strip(),
        from_id="pantry",
        from_name="Pantry Pilot",
        to_id=to.get("id"),
        to_name=to.get("name"),
        channel=channel or to.get("preferred_contact", "sms"),
        kind=kind,
        cycle_id=_ctx.cycle_id_of(agent),
        meta={"status": status, "by": _ctx.agent_name(agent)},
    )
    return {"ok": True, "message_id": msg["id"], "to": to.get("name"), "to_id": to.get("id"), "status": status}


@tool
def send_message(
    body: str,
    to_volunteer_id: str | None = None,
    to_contact: str | None = None,
    agent: Any = None,
) -> dict[str, Any]:
    """Send a routine 1:1 text to one volunteer, donor, or partner (goes to the outbox).

    Use for replies, confirmations, asks ("can you cover Saturday 9am?"), and reminders.
    Keep it short and warm; sign as "Pantry Pilot (for Aisha)". Never use this to message
    everyone: that is send_broadcast, which needs approval. Never message the coordinator
    with this; use escalate_to_coordinator instead.

    Args:
        body: The message text.
        to_volunteer_id: Volunteer id such as "V-04" (preferred for volunteers).
        to_contact: Contact id ("C-LINDA"), or a name for donors/partners ("Linda Moreno").

    Returns:
        ok, message_id, recipient, and status ("sent" or "queued_until_morning" during quiet hours).
    """
    if not body or not body.strip():
        return {"ok": False, "error": "body is empty"}
    handle = to_volunteer_id or to_contact
    person = _ctx.resolve_person(handle)
    if person is None:
        return {"ok": False, "error": f"Unknown recipient '{handle}'. Use a volunteer id, contact id, or full name."}
    if person.get("type") == "coordinator":
        return {"ok": False, "error": "Do not text the coordinator directly; use escalate_to_coordinator."}
    return _deliver(to=person, body=body, kind="message", agent=agent)


@tool
def send_broadcast(body: str, agent: Any = None) -> dict[str, Any]:
    """Send a message to ALL volunteers. GATED: requires the coordinator's approval.

    Calling this does not send anything. It files an approval request with the exact text;
    the coordinator sees it in the console and approves or declines. Tell affected people
    the broadcast is pending approval rather than promising it. Use only when a message
    really must reach everyone (e.g. pantry closed for weather).

    Args:
        body: The full broadcast text as it should be sent.

    Returns:
        If approved by the coordinator: recipients count and message ids. Otherwise a pending
        decision id with status "pending_approval".
    """
    s = _ctx.store()
    if not body or not body.strip():
        return {"ok": False, "error": "body is empty"}
    if _ctx.is_approved(agent):
        volunteers = s.volunteers()
        ids = [_deliver(to=v, body=body, kind="broadcast", agent=agent)["message_id"] for v in volunteers]
        return {"ok": True, "status": "sent", "recipients": len(ids), "message_ids": ids}
    summary = f"Broadcast to all {len(s.volunteers())} volunteers"
    existing = s.find_pending_decision("approval", summary)
    if existing and existing["payload"].get("input", {}).get("body") == body.strip():
        return {"ok": True, "status": "pending_approval", "decision_id": existing["id"], "already_filed": True}
    decision = s.create_decision(
        kind="approval",
        summary=summary,
        options=["Approve and send", "Decline"],
        recommendation="Approve and send",
        payload={"tool": "send_broadcast", "input": {"body": body.strip()}, "preview": body.strip()},
        created_by=_ctx.agent_name(agent),
        cycle_id=_ctx.cycle_id_of(agent),
    )
    return {
        "ok": True,
        "status": "pending_approval",
        "decision_id": decision["id"],
        "note": "Nothing was sent. The coordinator must approve this broadcast in the console.",
    }


@tool
def escalate_to_coordinator(
    summary: str,
    options: list[str],
    recommendation: str,
    agent: Any = None,
) -> dict[str, Any]:
    """Ask the coordinator to decide something only a human should decide.

    Use for: two volunteers wanting the same last slot; a shift still short within 24h after
    two rounds of asks; a donation that may not fit storage; anything involving minors or
    safety rules; any other real conflict. Do NOT use for routine questions you can answer
    from the schedule or inventory. After escalating, do not act on the matter; tell the
    people involved you are checking with the coordinator. The coordinator's answer arrives
    later as an inbound message from "coordinator".

    Args:
        summary: The situation in 2-3 plain sentences, with names, dates and numbers.
        options: 2-3 concrete choices, each a short imperative sentence.
        recommendation: Which option you recommend and why (one sentence).

    Returns:
        decision_id and status "pending" (or the existing id if an identical one is open).
    """
    s = _ctx.store()
    summary = summary.strip()
    options = [o.strip() for o in options if o and o.strip()][:4]
    if not summary or len(options) < 2:
        return {"ok": False, "error": "Provide a summary and at least two options."}
    existing = s.find_pending_decision("escalation", summary)
    if existing:
        return {"ok": True, "decision_id": existing["id"], "status": "pending", "already_filed": True}
    decision = s.create_decision(
        kind="escalation",
        summary=summary,
        options=options,
        recommendation=recommendation.strip(),
        payload={"raised_by": _ctx.agent_name(agent)},
        created_by=_ctx.agent_name(agent),
        cycle_id=_ctx.cycle_id_of(agent),
    )
    return {
        "ok": True,
        "decision_id": decision["id"],
        "status": "pending",
        "note": "The coordinator will answer via the console; their reply arrives as an inbound message.",
    }
