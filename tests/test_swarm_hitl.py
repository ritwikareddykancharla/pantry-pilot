"""End-to-end: Swarm handoffs, conversational escalation, and gated approvals with scripted models.

This is the most important test file. It drives ``service.run_sweep`` and ``service.decide``
with deterministic per-agent scripts and asserts on node_history, the audit trail, decisions,
the coordinator's reply loop, and the approved-execution path for gated tools.
"""

from __future__ import annotations

from strands import Agent

from pantrypilot import service
from pantrypilot.brief import WeeklyBrief
from pantrypilot.store import Store
from pantrypilot.tools import READ_ONLY_TOOLS
from scripted_model import ScriptedModel, model_for_factory

BRIEF = {
    "period_start": "2026-09-11",
    "period_end": "2026-09-18",
    "coverage_pct": 0,
    "summary": "Saturday is covered; the turkey offer needs your call.",
    "coordinator_actions": ["Answer the turkey question"],
}


def test_scripted_model_with_trivial_agent(store: Store) -> None:
    model = ScriptedModel([{"tool": "today", "input": {}}, {"text": "It is Friday."}])
    agent = Agent(model=model, tools=READ_ONLY_TOOLS, callback_handler=None, name="probe")
    result = agent("what day is it?")
    assert result.stop_reason == "end_turn" and str(result).strip() == "It is Friday."
    assert len(model.calls) == 2 and "today" in model.calls[0]["tool_names"]
    assert model.remaining == 0


def _sweep_models() -> dict[str, ScriptedModel]:
    dispatcher = ScriptedModel(
        [
            {"tool": "today", "input": {}},
            {"tool": "list_inbound_messages", "input": {}},
            {
                "tool": "handoff_to_agent",
                "input": {
                    "agent_name": "roster",
                    "message": "M-007: Jorge offers to drive Thursday delivery S-0917-DELIVERY; "
                    "assign confirmed and reply. Then send reminders for tomorrow's shifts.",
                    "context": {"message_ids": ["M-007"]},
                },
            },
            {"text": "Handed off to roster."},
            {
                "tool": "handoff_to_agent",
                "input": {
                    "agent_name": "steward",
                    "message": "M-005: Riverside offers 30 frozen turkeys Friday. "
                    "Check freezer capacity; escalate if short.",
                },
            },
            {"text": "Handed off to steward."},
            {"text": "Cycle done. Jorge confirmed for Thursday; reminders sent; turkey offer escalated (D-001)."},
        ],
        name="dispatcher",
    )
    roster = ScriptedModel(
        [
            {
                "tool": "assign_volunteer",
                "input": {"slot_id": "S-0917-DELIVERY", "volunteer_id": "V-04", "status": "confirmed"},
            },
            {
                "tool": "send_message",
                "input": {
                    "to_volunteer_id": "V-04",
                    "body": "Hi Jorge, yes the Thursday run is on, 4-6pm. You are confirmed as driver. "
                    "Pantry Pilot (for Aisha)",
                },
            },
            {
                "tool": "mark_message_handled",
                "input": {"message_id": "M-007", "note": "Jorge confirmed on S-0917-DELIVERY"},
            },
            {"tool": "send_shift_reminders", "input": {"slot_id": "S-0912-INTAKE"}},
            {
                "tool": "handoff_to_agent",
                "input": {"agent_name": "dispatcher", "message": "Done: Jorge confirmed; reminders sent."},
            },
            {"text": "Handing back."},
        ],
        name="roster",
    )
    steward = ScriptedModel(
        [
            {"tool": "check_storage_capacity", "input": {"kind": "frozen", "qty": 30, "unit": "turkeys"}},
            {
                "tool": "escalate_to_coordinator",
                "input": {
                    "summary": "Riverside Church offers 30 frozen turkeys Friday. "
                    "The chest freezer has 7.5 cuft free, room for 10.",
                    "options": [
                        "Accept 10 turkeys that fit",
                        "Decline the offer",
                        "Borrow freezer space and accept all 30",
                    ],
                    "recommendation": "Accept 10 turkeys that fit; no partner freezer is confirmed.",
                },
            },
            {
                "tool": "send_message",
                "input": {
                    "to_contact": "C-RIVERSIDE",
                    "body": "Thank you! Checking freezer space with Aisha, will confirm today.",
                },
            },
            {"tool": "mark_message_handled", "input": {"message_id": "M-005", "note": "Escalated turkeys (D-001)"}},
            {
                "tool": "handoff_to_agent",
                "input": {"agent_name": "dispatcher", "message": "Done: turkeys escalated as D-001."},
            },
            {"text": "Handing back."},
        ],
        name="steward",
    )
    briefer = ScriptedModel(name="briefer", structured=BRIEF)
    return {"dispatcher": dispatcher, "roster": roster, "steward": steward, "briefer": briefer}


def test_sweep_handoffs_actions_and_escalation(store: Store) -> None:
    models = _sweep_models()
    out = service.run_sweep(model_for=model_for_factory(models))

    assert out["ok"] and out["status"] == "completed"
    # Swarm topology: dispatcher -> roster -> dispatcher -> steward -> dispatcher
    assert out["handoff_trail"] == ["dispatcher", "roster", "dispatcher", "steward", "dispatcher"]
    assert store.last_cycle()["handoff_trail"] == out["handoff_trail"]
    assert all(m.remaining == 0 for m in models.values())

    # Audit trail has the agent name on every tool call, including the handoffs.
    audit = store.list_audit(cycle_id=out["cycle_id"])
    by_agent = {(a["agent"], a["tool"]) for a in audit}
    assert ("dispatcher", "list_inbound_messages") in by_agent
    assert ("roster", "assign_volunteer") in by_agent
    assert ("steward", "check_storage_capacity") in by_agent
    assert ("steward", "escalate_to_coordinator") in by_agent
    handoffs = [a for a in audit if a["kind"] == "handoff"]
    assert [(h["agent"], h["input"]["agent_name"]) for h in handoffs] == [
        ("dispatcher", "roster"),
        ("roster", "dispatcher"),
        ("dispatcher", "steward"),
        ("steward", "dispatcher"),
    ]
    trail_rows = [a for a in audit if a["kind"] == "trail"]
    assert trail_rows and trail_rows[0]["result"] == "dispatcher -> roster -> dispatcher -> steward -> dispatcher"

    # Routine work happened silently: Jorge is confirmed, messages went out, inbox items closed.
    roster_entry = store.shift("S-0917-DELIVERY")["roster"]
    assert roster_entry == [{"volunteer_id": "V-04", "status": "confirmed"}]
    outbound = store.list_messages(direction="outbound", cycle_id=out["cycle_id"])
    assert {m["to_name"] for m in outbound} >= {
        "Jorge Alvarez",
        "Dana Whitfield",
        "Maria Santos",
        "Ellie Chen",
        "Pastor Dave Kim",
    }
    assert store.get_message("M-007")["handled"] and store.get_message("M-005")["handled"]
    assert len(store.list_messages(direction="inbound", handled=False)) == 5

    # Exactly one decision surfaced, with options and a recommendation.
    pending = out["pending_decisions"]
    assert len(pending) == 1 and pending[0]["kind"] == "escalation" and pending[0]["created_by"] == "steward"
    assert len(pending[0]["options"]) == 3 and "Accept 10" in pending[0]["recommendation"]

    # Weekly brief: numbers from the store, prose from the model.
    report = out["report"]
    assert report["summary"] == BRIEF["summary"]
    assert report["coverage_pct"] == 75.0  # 9 of 12 spots filled in the next 7 days after Jorge
    assert report["escalations_pending"] == 1 and any("Tuesday distribution" in s for s in report["open_slots"])
    assert store.last_report()["cycle_id"] == out["cycle_id"]
    assert WeeklyBrief(**{k: v for k, v in report.items() if k in WeeklyBrief.model_fields})


def test_decide_escalation_feeds_back_as_coordinator_message(store: Store) -> None:
    models = _sweep_models()
    service.run_sweep(model_for=model_for_factory(models))
    decision_id = store.list_decisions(status="pending")[0]["id"]

    out = service.decide(decision_id, "0")
    assert out["ok"] and out["decision"]["status"] == "resolved"
    assert out["result"]["chosen"] == "Accept 10 turkeys that fit"
    queued = store.get_message(out["result"]["queued_message_id"])
    assert queued["direction"] == "inbound" and queued["from_id"] == "coordinator" and not queued["handled"]
    assert queued["kind"] == "coordinator_decision" and queued["meta"]["decision_id"] == decision_id
    assert "Accept 10 turkeys" in queued["body"]

    # A second decide on the same id is rejected.
    assert not service.decide(decision_id, "1")["ok"]

    # Next cycle: the swarm treats the coordinator's answer like any other message and carries it out.
    dispatcher = ScriptedModel(
        [
            {"tool": "list_inbound_messages", "input": {}},
            {
                "tool": "handoff_to_agent",
                "input": {
                    "agent_name": "steward",
                    "message": f"{queued['id']}: coordinator decided: accept 10 turkeys. Log and reply.",
                },
            },
            {"text": "ok"},
            {"text": "Coordinator decision carried out."},
        ],
        name="dispatcher",
    )
    steward = ScriptedModel(
        [
            {
                "tool": "log_donation",
                "input": {
                    "donor": "C-RIVERSIDE",
                    "items": [{"item": "Frozen turkeys", "qty": 10, "unit": "each"}],
                    "scheduled_dropoff": "2026-09-18 16:00-17:00",
                },
            },
            {
                "tool": "send_message",
                "input": {"to_contact": "C-RIVERSIDE", "body": "We can take 10 turkeys on Friday 4pm. Thank you."},
            },
            {
                "tool": "mark_message_handled",
                "input": {"message_id": queued["id"], "note": "Accepted 10 turkeys per coordinator"},
            },
            {"tool": "handoff_to_agent", "input": {"agent_name": "dispatcher", "message": "Done."}},
            {"text": "back"},
        ],
        name="steward",
    )
    second = service.process_message(
        queued["id"], model_for=model_for_factory({"dispatcher": dispatcher, "steward": steward})
    )
    assert second["ok"] and second["handoff_trail"] == ["dispatcher", "steward", "dispatcher"]
    assert store.get_message(queued["id"])["handled"]
    assert store.list_donations()[0]["items"][0]["qty"] == 10
    assert any("10 turkeys" in m["body"] for m in second["replies"])
    assert store.counts()["decisions_resolved"] == 1 and store.counts()["decisions_pending"] == 0


def test_free_text_and_yes_responses_map_to_options(store: Store) -> None:
    d = store.create_decision(
        kind="escalation",
        summary="Priya vs Marcus",
        options=["Give it to Priya", "Give it to Marcus"],
        recommendation="Give it to Priya (fewest recent shifts)",
        created_by="roster",
    )
    yes = service.decide(d["id"], "yes")
    assert yes["result"]["chosen"] == "Give it to Priya" and yes["result"]["option_index"] == 0
    d2 = store.create_decision(
        kind="escalation", summary="Other", options=["A", "B"], recommendation="B", created_by="roster"
    )
    free = service.decide(d2["id"], "Split the shift: Priya 9-11, Marcus 11-1")
    assert free["result"]["option_index"] is None and free["result"]["chosen"].startswith("Split the shift")
    assert len(store.list_messages(direction="inbound", handled=False)) == 7 + 2


def test_gated_broadcast_requires_approval_then_executes(store: Store) -> None:
    # A dispatcher run that tries to broadcast: the tool only files an approval.
    dispatcher = ScriptedModel(
        [
            {
                "tool": "send_broadcast",
                "input": {
                    "body": "Maple Street Pantry is closed Saturday Sep 12 for a water leak. Pantry Pilot (for Aisha)"
                },
            },
            {"text": "Broadcast filed for approval."},
        ],
        name="dispatcher",
    )
    out = service.run_sweep(
        model_for=model_for_factory({"dispatcher": dispatcher}, default=ScriptedModel(structured=BRIEF))
    )
    assert out["handoff_trail"] == ["dispatcher"]
    pending = store.list_decisions(status="pending")
    assert len(pending) == 1 and pending[0]["kind"] == "approval" and pending[0]["payload"]["tool"] == "send_broadcast"
    assert store.list_messages(direction="outbound") == []  # nothing sent yet

    # Decline: nothing goes out.
    declined = service.decide(pending[0]["id"], "no")
    assert declined["ok"] and declined["result"] == {"executed": False}
    assert store.list_messages(direction="outbound") == []

    # File again, approve with an edit: executes via a direct tool call on an approved dispatcher.
    again = ScriptedModel(
        [{"tool": "send_broadcast", "input": {"body": "Closed Saturday."}}, {"text": "filed"}], name="dispatcher"
    )
    service.run_sweep(model_for=model_for_factory({"dispatcher": again}, default=ScriptedModel(structured=BRIEF)))
    approval = store.list_decisions(status="pending")[0]
    approved = service.decide(approval["id"], "yes", {"body": "Closed Saturday Sep 12; reopening Tuesday 4pm."})
    assert approved["ok"] and approved["result"]["executed"] is True
    sent = store.list_messages(direction="outbound")
    assert len(sent) == 14 and all(m["kind"] == "broadcast" for m in sent)
    assert all(m["body"].startswith("Closed Saturday Sep 12; reopening") for m in sent)
    # The direct call was audited under the dispatcher's name.
    assert any(a["agent"] == "dispatcher" and a["tool"] == "send_broadcast" for a in store.list_audit())
    assert store.get_decision(approval["id"])["status"] == "resolved"


def test_ask_uses_read_only_agent(store: Store) -> None:
    model = ScriptedModel(
        [{"tool": "get_schedule", "input": {"days": 2}}, {"text": "Saturday intake: Dana, Maria, Ellie."}]
    )
    answer = service.ask("who is on Saturday morning?", model=model)
    assert answer == "Saturday intake: Dana, Maria, Ellie."
    assert set(model.calls[0]["tool_names"]) >= {"get_schedule", "get_inventory", "list_inbound_messages"}
    assert "send_message" not in model.calls[0]["tool_names"]
