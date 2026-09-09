"""AgentCore entrypoint: payload dispatch through ``main.invoke`` with the model factory patched."""

from __future__ import annotations

import pytest

import main
from pantrypilot import agents, service
from pantrypilot.store import Store
from scripted_model import ScriptedModel

BRIEF = {"period_start": "2026-09-11", "period_end": "2026-09-18", "coverage_pct": 0, "summary": "ok"}


@pytest.fixture
def patched_model(monkeypatch):
    """Every agent gets a scripted model that ends its turn immediately (except where a test overrides)."""
    holder = {"model": ScriptedModel(structured=BRIEF, default_text="Nothing to do.")}

    def factory():
        return holder["model"]

    monkeypatch.setattr(agents, "build_model", factory)
    monkeypatch.setattr(service, "build_model", factory)
    return holder


def test_invoke_sweep_and_status(store: Store, patched_model) -> None:
    out = main.invoke({"action": "sweep"})
    assert out["ok"] and out["cycle_id"].startswith("C-0001-")
    assert out["handoff_trail"] == ["dispatcher"] and out["report"]["coverage_pct"] == 66.7
    assert out["pending_decisions"] == [] and isinstance(out["actions_taken"], list)
    status = main.invoke({"action": "status"})
    assert status["ok"] and status["counts"]["cycles"] == 1 and status["last_sweep_at"]
    assert status["last_report"]["cycle_id"] == out["cycle_id"]


def test_invoke_inbound_decide_ask_state(store: Store, patched_model) -> None:
    patched_model["model"] = ScriptedModel(
        [
            {
                "tool": "escalate_to_coordinator",
                "input": {
                    "summary": "Marcus wants the last Saturday spot too.",
                    "options": ["Give it to Priya", "Give it to Marcus"],
                    "recommendation": "Give it to Priya",
                },
            },
            {"text": "Escalated."},
        ],
        structured=BRIEF,
    )
    inbound = main.invoke({"action": "inbound", "from": "V-03", "text": "I could also do Sat 9"})
    assert inbound["ok"] and inbound["message"]["from_id"] == "V-03" and inbound["message"]["channel"] == "sms"
    assert len(inbound["pending_decisions"]) == 1
    decision_id = inbound["pending_decisions"][0]["id"]

    decided = main.invoke({"action": "decide", "decision_id": decision_id, "response": "1"})
    assert decided["ok"] and decided["result"]["chosen"] == "Give it to Marcus"
    assert not main.invoke({"action": "decide", "response": "yes"})["ok"]
    assert not main.invoke({"action": "decide", "decision_id": "D-999", "response": "yes"})["ok"]

    patched_model["model"] = ScriptedModel([{"text": "Dana, Maria and Ellie are on Saturday intake."}])
    asked = main.invoke({"action": "ask", "prompt": "who is on Saturday morning?"})
    assert asked == {"ok": True, "answer": "Dana, Maria and Ellie are on Saturday intake."}
    assert not main.invoke({"action": "ask", "prompt": ""})["ok"]

    state = main.invoke({"action": "state"})
    assert (
        state["ok"] and len(state["shifts"]) == 5 and state["counts"]["inbound_unhandled"] == 9
    )  # 7 seeded + Marcus + coordinator reply


def test_invoke_unknown_action_and_errors_never_raise(store: Store, patched_model, monkeypatch) -> None:
    bad = main.invoke({"action": "explode"})
    assert bad["ok"] is False and "Unknown action" in bad["error"]
    assert main.invoke({})["ok"] is False
    assert main.invoke(None)["ok"] is False

    def boom():
        raise RuntimeError("model down")

    monkeypatch.setattr(service, "run_sweep", boom)
    out = main.invoke({"action": "sweep"})
    assert out == {"ok": False, "error": "RuntimeError: model down"}
