"""Agent and Swarm construction.

- ``build_swarm`` wires dispatcher (entry point), roster and steward into a Strands ``Swarm``
  with a shared session manager and the ``AuditHook``.
- ``build_ask_agent`` is a lightweight read-only agent for coordinator questions.
- ``build_approved_executor`` is a dispatcher instance whose state carries ``approved=True``;
  ``service.decide`` uses it for direct tool calls that execute gated actions after approval.

``model_for`` lets tests inject a scripted model per agent name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.model import Model
from strands.multiagent import Swarm
from strands.session.session_manager import SessionManager

from . import prompts, tools
from .hooks import AuditHook, ProgressHook
from .model import build_model

ModelFactory = Callable[[str], Model]

SWARM_CONFIG: dict[str, Any] = {
    "max_handoffs": 12,
    "max_iterations": 16,
    "execution_timeout": 600.0,
    "node_timeout": 180.0,
    "repetitive_handoff_detection_window": 6,
    "repetitive_handoff_min_unique_agents": 2,
}


def _default_model_for() -> ModelFactory:
    shared = build_model()
    return lambda _name: shared


def _agent(
    name: str,
    system_prompt: str,
    agent_tools: list[Any],
    model: Model,
    *,
    cycle_id: str | None,
    approved: bool = False,
    hooks: list[Any] | None = None,
    agent_id: str | None = None,
) -> Agent:
    return Agent(
        model=model,
        name=name,
        description=prompts.AGENT_DESCRIPTIONS.get(name, name),
        agent_id=agent_id or f"pantrypilot-{name}",
        system_prompt=system_prompt,
        tools=agent_tools,
        callback_handler=None,
        hooks=hooks or [],
        state={"cycle_id": cycle_id, "approved": approved, "role": name},
        conversation_manager=SlidingWindowConversationManager(window_size=40),
    )


def build_agents(
    cycle_id: str,
    model_for: ModelFactory | None = None,
    audit: AuditHook | None = None,
) -> dict[str, Agent]:
    """Build the three swarm agents keyed by name."""
    model_for = model_for or _default_model_for()
    audit = audit or AuditHook()
    hooks = [audit, ProgressHook()]
    return {
        "dispatcher": _agent(
            "dispatcher",
            prompts.DISPATCHER_PROMPT,
            tools.DISPATCHER_TOOLS,
            model_for("dispatcher"),
            cycle_id=cycle_id,
            hooks=hooks,
        ),
        "roster": _agent(
            "roster", prompts.ROSTER_PROMPT, tools.ROSTER_TOOLS, model_for("roster"), cycle_id=cycle_id, hooks=hooks
        ),
        "steward": _agent(
            "steward",
            prompts.STEWARD_PROMPT,
            tools.STEWARD_TOOLS,
            model_for("steward"),
            cycle_id=cycle_id,
            hooks=hooks,
        ),
    }


def build_swarm(
    cycle_id: str,
    session_manager: SessionManager | None,
    model_for: ModelFactory | None = None,
    audit: AuditHook | None = None,
) -> Swarm:
    """Create the dispatcher/roster/steward Swarm for one cycle."""
    audit = audit or AuditHook()
    agents = build_agents(cycle_id, model_for, audit)
    return Swarm(
        nodes=[agents["dispatcher"], agents["roster"], agents["steward"]],
        entry_point=agents["dispatcher"],
        session_manager=session_manager,
        hooks=[audit],
        id=f"pantrypilot-swarm-{cycle_id}",
        **SWARM_CONFIG,
    )


def build_ask_agent(model: Model | None = None) -> Agent:
    """Read-only agent for 'who is on Saturday morning?' style questions."""
    return Agent(
        model=model or build_model(),
        name="ask",
        description="Read-only assistant for coordinator questions.",
        agent_id="pantrypilot-ask",
        system_prompt=prompts.ASK_PROMPT,
        tools=tools.READ_ONLY_TOOLS,
        callback_handler=None,
        state={"cycle_id": None, "approved": False, "role": "ask"},
        conversation_manager=SlidingWindowConversationManager(window_size=40),
    )


def build_approved_executor(cycle_id: str | None, decision_id: str, model: Model | None = None) -> Agent:
    """Dispatcher instance with ``approved=True`` in state, used only for direct gated tool calls."""
    return Agent(
        model=model or build_model(),
        name="dispatcher",
        description=prompts.AGENT_DESCRIPTIONS["dispatcher"],
        agent_id=f"pantrypilot-dispatcher-approved-{decision_id}",
        system_prompt=prompts.DISPATCHER_PROMPT,
        tools=list(tools.GATED_TOOLS.values()),
        callback_handler=None,
        hooks=[AuditHook()],
        record_direct_tool_call=False,
        state={"cycle_id": cycle_id, "approved": True, "approved_decision_id": decision_id, "role": "dispatcher"},
    )
