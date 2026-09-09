"""Amazon Bedrock AgentCore Runtime entrypoint for Pantry Pilot.

Payload contract (JSON):
  {"action": "sweep"}
  {"action": "decide", "decision_id": "D-001", "response": "yes"|"no"|"<option or free text>", "edits": {...}}
  {"action": "ask", "prompt": "who is on Saturday morning?"}
  {"action": "status"}
  {"action": "inbound", "from": "V-04", "text": "I can drive Thursday"}      # project-specific
  {"action": "state"}                                                          # console snapshot
Unknown actions return {"ok": false, "error": "..."}; the entrypoint never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from pantrypilot import config, service

config.configure_logging()
logger = logging.getLogger("pantrypilot.main")

app = BedrockAgentCoreApp()


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a payload to the service layer. Shared by the AgentCore entrypoint and tests."""
    action = str((payload or {}).get("action", "")).lower()
    if action == "sweep":
        out = service.run_sweep()
        return {
            "ok": True,
            "cycle_id": out["cycle_id"],
            "report": out["report"],
            "pending_decisions": out["pending_decisions"],
            "actions_taken": out["actions_taken"],
            "handoff_trail": out["handoff_trail"],
        }
    if action == "decide":
        decision_id = payload.get("decision_id")
        if not decision_id:
            return {"ok": False, "error": "decision_id is required"}
        return service.decide(str(decision_id), str(payload.get("response", "")), payload.get("edits") or {})
    if action == "ask":
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        return {"ok": True, "answer": service.ask(prompt)}
    if action == "status":
        return service.status()
    if action == "inbound":
        text = str(payload.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        out = service.process_inbound(
            text, from_id=payload.get("from") or payload.get("from_id"), from_name=payload.get("from_name")
        )
        return {
            "ok": True,
            "cycle_id": out.get("cycle_id"),
            "message": out.get("message"),
            "replies": out.get("replies", []),
            "pending_decisions": out.get("pending_decisions", []),
            "handoff_trail": out.get("handoff_trail", []),
        }
    if action == "state":
        return {"ok": True, **service.state_snapshot()}
    return {"ok": False, "error": f"Unknown action '{action}'. Use sweep, decide, ask, status, inbound, state."}


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    """AgentCore entrypoint: dispatch on payload["action"]; never raise."""
    try:
        return dispatch(payload or {})
    except Exception as exc:  # noqa: BLE001 - contract: never raise out of the entrypoint
        logger.exception("invoke failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    app.run()  # serves POST /invocations and GET /ping on 0.0.0.0:8080
