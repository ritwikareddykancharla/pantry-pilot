"""Strands hook providers: the audit trail.

``AuditHook`` records every tool call (with the calling agent's name and the current cycle id
from the agent's state) so the console can show the handoff trail between dispatcher, roster
and steward and the list of routine actions the swarm took on its own.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from .store import Store, get_store

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 600


def _summarize_result(result: dict[str, Any] | None) -> tuple[str, str]:
    if not result:
        return "", "unknown"
    status = str(result.get("status", "unknown"))
    parts: list[str] = []
    for block in result.get("content", []) or []:
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"], default=str))
    text = " ".join(parts).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[: MAX_RESULT_CHARS - 3] + "..."
    return text, status


class AuditHook(HookProvider):
    """Writes one audit row per tool call, tagged with the agent name and cycle id."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store or get_store()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        name = str(tool_use.get("name", "?"))
        agent = getattr(event, "agent", None)
        agent_name = getattr(agent, "name", None) or "agent"
        cycle_id = None
        try:
            cycle_id = agent.state.get("cycle_id") if agent is not None else None
        except Exception:  # pragma: no cover - defensive
            cycle_id = None
        text, status = _summarize_result(event.result)
        kind = "handoff" if name == "handoff_to_agent" else "tool"
        try:
            self.store.add_audit(
                cycle_id=cycle_id,
                agent=agent_name,
                tool=name,
                input=dict(tool_use.get("input") or {}),
                result=text,
                status=status,
                kind=kind,
            )
        except Exception:  # pragma: no cover - never break the agent loop because of auditing
            logger.exception("audit write failed for %s.%s", agent_name, name)
