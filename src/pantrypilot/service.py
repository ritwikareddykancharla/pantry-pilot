"""Core orchestration used by ``main.py`` (AgentCore), ``app/server.py`` and the CLI.

Public functions: ``run_sweep``, ``process_inbound``, ``decide``, ``ask``, ``status``,
``state_snapshot``, ``ensure_seeded``.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

from strands.models.model import Model

from . import config
from .agents import ModelFactory, build_approved_executor, build_ask_agent, build_swarm
from .brief import generate_brief
from .hooks import AuditHook
from .model import build_model
from .seed import seed_store
from .sessions import build_session_manager
from .store import Store, get_store
from .tools import _ctx

logger = logging.getLogger(__name__)

# Sweeps and inbound runs mutate the same state; never run two swarm cycles concurrently.
CYCLE_LOCK = threading.Lock()

YES_WORDS = {"y", "yes", "approve", "approved", "ok", "okay", "send", "go", "do it", "true"}
NO_WORDS = {"n", "no", "deny", "denied", "decline", "declined", "reject", "cancel", "false"}


def ensure_seeded(store: Store | None = None) -> bool:
    """Seed the database from ``data/`` if it is empty. Returns True if seeding happened."""
    store = store or get_store()
    if store.counts()["volunteers"] == 0:
        seed_store(store, reset=True)
        logger.info("seeded empty database from %s", config.DATA_DIR)
        return True
    return False


# ----------------------------------------------------------------------------- cycles
def _final_text(result: Any) -> str:
    try:
        if result.node_history:
            last = result.node_history[-1].node_id
            node_result = result.results.get(last)
            if node_result is not None:
                return str(node_result.result).strip()
    except Exception:  # pragma: no cover - defensive
        pass
    return ""


def _run_cycle(kind: str, task: str, *, model_for: ModelFactory | None = None) -> dict[str, Any]:
    store = get_store()
    ensure_seeded(store)
    with CYCLE_LOCK:
        cycle = store.start_cycle(kind, task)
        cycle_id = cycle["id"]
        logger.info("cycle %s (%s) starting", cycle_id, kind)
        session_manager = build_session_manager(session_id=f"pantrypilot-{cycle_id}")
        audit = AuditHook(store)
        swarm = build_swarm(cycle_id, session_manager, model_for=model_for, audit=audit)
        status = "completed"
        trail: list[str] = []
        final_text = ""
        try:
            result = swarm(task)
            trail = [node.node_id for node in result.node_history]
            status = str(getattr(result.status, "value", result.status)).lower()
            final_text = _final_text(result)
        except Exception as exc:
            logger.exception("cycle %s failed", cycle_id)
            status = "failed"
            final_text = f"Cycle failed: {exc}"
        store.add_audit(
            cycle_id=cycle_id,
            agent="swarm",
            tool="handoff_trail",
            input={"trail": trail},
            result=" -> ".join(trail) if trail else "(no nodes ran)",
            status=status,
            kind="trail",
        )
        store.finish_cycle(cycle_id, status=status, handoff_trail=trail, summary=final_text)
        store.set_meta(f"last_{kind}_at", config.now_iso())
        store.set_meta("last_cycle_id", cycle_id)
        return {
            "cycle_id": cycle_id,
            "status": status,
            "handoff_trail": trail,
            "final_text": final_text,
            "actions_taken": [a for a in store.list_audit(cycle_id=cycle_id) if a["kind"] != "trail"],
            "pending_decisions": store.list_decisions(status="pending"),
            "outbound": store.list_messages(direction="outbound", cycle_id=cycle_id),
        }


def sweep_task(store: Store) -> str:
    today = config.today()
    unhandled = len(store.list_messages(direction="inbound", handled=False))
    return (
        f"Today is {today.strftime('%A')} {today.isoformat()}. There are {unhandled} unhandled inbound message(s). "
        "Process every unhandled message, then run the daily checklist."
    )


def run_sweep(*, model_for: ModelFactory | None = None, brief_model: Model | None = None) -> dict[str, Any]:
    """Run one background cycle: process the inbox, run the checklist, write the weekly brief."""
    store = get_store()
    ensure_seeded(store)
    outcome = _run_cycle("sweep", sweep_task(store), model_for=model_for)
    model = brief_model or (model_for("briefer") if model_for else build_model())
    brief = generate_brief(store, model, cycle_id=outcome["cycle_id"])
    report = {"cycle_id": outcome["cycle_id"], "generated_at": config.now_iso(), **brief.model_dump()}
    store.save_report(outcome["cycle_id"], report)
    store.set_meta("last_sweep_at", config.now_iso())
    return {"ok": True, "report": report, **outcome}


def enqueue_inbound(
    text: str,
    *,
    from_id: str | None = None,
    from_name: str | None = None,
    channel: str | None = None,
    kind: str = "message",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add an inbound message to the queue without running the swarm."""
    store = get_store()
    ensure_seeded(store)
    person = _ctx.resolve_person(from_id or from_name)
    resolved_id = person["id"] if person else (from_id or None)
    resolved_name = from_name or (person["name"] if person else from_id) or "Unknown sender"
    if not channel and person:
        channel = person.get("preferred_contact", "sms")
    return store.add_message(
        direction="inbound",
        body=text.strip(),
        from_id=resolved_id,
        from_name=resolved_name,
        to_id="pantry",
        to_name=store.org().get("short_name", "Pantry"),
        channel=channel or "sms",
        kind=kind,
        meta=meta or {},
    )


def inbound_task(message: dict[str, Any]) -> str:
    today = config.today()
    who = f"{message.get('from_name')} ({message.get('from_id')})"
    return (
        f"Today is {today.strftime('%A')} {today.isoformat()}. A new inbound message just arrived, id {message['id']} "
        f'from {who}: "{message["body"]}". Handle only this message now (reply, assign, log, or escalate as the '
        "rules say), mark it handled, and skip the daily checklist."
    )


def process_inbound(
    text: str,
    *,
    from_id: str | None = None,
    from_name: str | None = None,
    channel: str | None = None,
    model_for: ModelFactory | None = None,
) -> dict[str, Any]:
    """Enqueue one inbound message and run the swarm on just that message."""
    message = enqueue_inbound(text, from_id=from_id, from_name=from_name, channel=channel)
    return process_message(message["id"], model_for=model_for)


def process_message(message_id: str, *, model_for: ModelFactory | None = None) -> dict[str, Any]:
    """Run the swarm on one already-queued inbound message."""
    store = get_store()
    message = store.get_message(message_id)
    if message is None:
        return {"ok": False, "error": f"No message {message_id}"}
    outcome = _run_cycle("inbound", inbound_task(message), model_for=model_for)
    return {"ok": True, "message": store.get_message(message_id), "replies": outcome["outbound"], **outcome}


# ----------------------------------------------------------------------------- decisions
def _is_yes(response: str) -> bool:
    return response.strip().lower() in YES_WORDS


def _is_no(response: str) -> bool:
    return response.strip().lower() in NO_WORDS


def _resolve_option(decision: dict[str, Any], response: str) -> tuple[str, int | None]:
    """Map a response (option index, option text, yes/no, or free text) to the chosen option text."""
    options: list[str] = decision.get("options") or []
    raw = (response or "").strip()
    if raw.isdigit() and options:
        idx = int(raw)
        if 0 <= idx < len(options):
            return options[idx], idx
        if 1 <= idx <= len(options):
            return options[idx - 1], idx - 1
    for i, opt in enumerate(options):
        if raw.lower() == opt.lower():
            return opt, i
    if options and _is_yes(raw):
        rec = decision.get("recommendation") or ""
        for i, opt in enumerate(options):
            if opt.lower() in rec.lower():
                return opt, i
        return options[0], 0
    if options and _is_no(raw):
        for i, opt in enumerate(options):
            if opt.lower().startswith(("decline", "deny", "no ", "do not", "don't")):
                return opt, i
    return raw, None


def decide(
    decision_id: str,
    response: str,
    edits: dict[str, Any] | None = None,
    *,
    model: Model | None = None,
) -> dict[str, Any]:
    """Resolve a decision.

    - ``approval`` (gated tools): a yes executes the stored tool call via a direct tool call on
      a dispatcher instance whose state carries ``approved=True``; a no records the denial.
    - ``escalation``: the chosen option (or free text) is queued as an inbound message from
      ``coordinator`` so the swarm carries it out on the next cycle.
    """
    store = get_store()
    decision = store.get_decision(decision_id)
    if decision is None:
        return {"ok": False, "error": f"No decision {decision_id}"}
    if decision["status"] != "pending":
        return {"ok": False, "error": f"Decision {decision_id} is already {decision['status']}", "decision": decision}
    edits = edits or {}
    response = (response or "").strip()

    if decision["kind"] == "approval":
        payload = decision.get("payload") or {}
        tool_name = payload.get("tool")
        approved = _is_yes(response) or response.lower().startswith("approve")
        if not approved:
            resolved = store.resolve_decision(decision_id, "resolved", response or "no", {"executed": False})
            store.add_audit(
                cycle_id=decision.get("cycle_id"),
                agent="coordinator",
                tool=f"declined:{tool_name}",
                input=payload.get("input", {}),
                result="Declined by coordinator; nothing sent.",
                status="success",
                kind="decision",
            )
            return {"ok": True, "decision": resolved, "result": {"executed": False}}
        tool_input = {**(payload.get("input") or {}), **edits}
        executor = build_approved_executor(decision.get("cycle_id"), decision_id, model=model)
        try:
            tool_result = getattr(executor.tool, tool_name)(**tool_input)
        except Exception as exc:
            logger.exception("approved execution failed for %s", decision_id)
            resolved = store.resolve_decision(decision_id, "failed", response, {"executed": False, "error": str(exc)})
            return {"ok": False, "error": str(exc), "decision": resolved}
        text = " ".join(b.get("text", "") for b in tool_result.get("content", []) if "text" in b)
        result = {"executed": True, "tool": tool_name, "input": tool_input, "output": text}
        resolved = store.resolve_decision(decision_id, "resolved", response, result)
        return {"ok": True, "decision": resolved, "result": result}

    # Escalation: conversational path.
    chosen, index = _resolve_option(decision, response)
    if not chosen:
        return {"ok": False, "error": "Empty response"}
    note = edits.get("note") if isinstance(edits.get("note"), str) else None
    text = f"Coordinator decided on {decision_id} ({decision['summary'][:140]}): {chosen}"
    if note:
        text += f". Note: {note}"
    message = enqueue_inbound(
        text,
        from_id="coordinator",
        from_name=store.org().get("coordinator", {}).get("name", "Coordinator"),
        channel="console",
        kind="coordinator_decision",
        meta={"decision_id": decision_id, "option_index": index},
    )
    result = {"chosen": chosen, "option_index": index, "queued_message_id": message["id"]}
    resolved = store.resolve_decision(decision_id, "resolved", chosen, result)
    store.add_audit(
        cycle_id=decision.get("cycle_id"),
        agent="coordinator",
        tool="decision",
        input={"decision_id": decision_id, "response": response},
        result=f"Chose: {chosen}. Queued as inbound {message['id']} for the next cycle.",
        status="success",
        kind="decision",
    )
    return {"ok": True, "decision": resolved, "result": result}


# ----------------------------------------------------------------------------- ask / status
def ask(prompt: str, *, model: Model | None = None) -> str:
    """Answer a coordinator question with the read-only agent."""
    ensure_seeded()
    agent = build_ask_agent(model=model)
    result = agent(prompt)
    return str(result).strip()


def status() -> dict[str, Any]:
    store = get_store()
    ensure_seeded(store)
    last_cycle = store.last_cycle()
    return {
        "ok": True,
        "today": config.today().isoformat(),
        "counts": store.counts(),
        "last_sweep_at": store.get_meta("last_sweep_at"),
        "last_cycle": last_cycle,
        "last_report": store.last_report(),
        "pending_decisions": store.list_decisions(status="pending"),
    }


def state_snapshot() -> dict[str, Any]:
    """Everything the coordinator console needs in one call."""
    store = get_store()
    ensure_seeded(store)
    by_id = {v["id"]: v for v in store.volunteers()}
    shifts = [_ctx.shift_view(sh, by_id) for sh in _ctx.shifts_in_window(config.LOOKAHEAD_DAYS)]
    today = config.today()
    inventory = []
    for item in store.inventory():
        days = None
        if item.get("expiry"):
            days = (date.fromisoformat(item["expiry"]) - today).days
        inventory.append({**item, "below_par": item["qty"] < item["par"], "days_to_expiry": days})
    last_cycle = store.last_cycle()
    people = [{"id": v["id"], "name": v["name"], "type": "volunteer"} for v in store.volunteers()] + [
        {"id": c["id"], "name": c["name"], "type": c.get("kind", "contact")} for c in store.contacts()
    ]
    return {
        "org": {k: v for k, v in store.org().items() if k != "coordinator"}
        | {"coordinator": {"name": store.org().get("coordinator", {}).get("name")}},
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "counts": store.counts(),
        "last_sweep_at": store.get_meta("last_sweep_at"),
        "last_cycle": last_cycle,
        "cycle_running": CYCLE_LOCK.locked(),
        "decisions": {
            "pending": store.list_decisions(status="pending"),
            "resolved": [d for d in store.list_decisions() if d["status"] != "pending"][-20:],
        },
        "shifts": shifts,
        "inventory": inventory,
        "messages": {
            "inbound": store.list_messages(direction="inbound", limit=60),
            "outbound": store.list_messages(direction="outbound", limit=60),
        },
        "audit": store.list_audit(limit=80),
        "last_report": store.last_report(),
        "donations": store.list_donations(),
        "people": people,
    }
