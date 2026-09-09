"""Strands hook providers: the audit trail and live narration.

``AuditHook`` records every tool call (with the calling agent's name and the current cycle id
from the agent's state) so the console can show the handoff trail between dispatcher, roster
and steward and the list of routine actions the swarm took on its own.

``ProgressHook`` records what each agent is doing *while* it does it ("thinking", what it said,
which tool it is about to call, each result) so the console can show a live strip during a
cycle instead of a silent spinner.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)

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


def _agent_and_cycle(event: Any) -> tuple[str, str | None]:
    agent = getattr(event, "agent", None)
    name = getattr(agent, "name", None) or "agent"
    try:
        cycle_id = agent.state.get("cycle_id") if agent is not None else None
    except Exception:  # pragma: no cover - defensive
        cycle_id = None
    return name, cycle_id


def _short_input(tool_input: dict[str, Any], limit: int = 160) -> str:
    text = json.dumps(tool_input, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class ProgressHook(HookProvider):
    """Live narration per agent, tagged with the agent's name and cycle id."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store or get_store()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(MessageAddedEvent, self._message_added)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _write(self, event: Any, kind: str, text: str) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        agent, cycle_id = _agent_and_cycle(event)
        try:
            self.store.add_progress(cycle_id=cycle_id, agent=agent, kind=kind, text=text)
        except Exception:  # pragma: no cover - never break the agent loop because of narration
            logger.exception("progress write failed")

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        self._write(event, "thinking", "Thinking...")

    def _message_added(self, event: MessageAddedEvent) -> None:
        message = event.message or {}
        if message.get("role") != "assistant":
            return
        for block in message.get("content") or []:
            if "text" in block:
                self._write(event, "said", block["text"])
            elif "toolUse" in block:
                tool_use = block["toolUse"] or {}
                name = str(tool_use.get("name") or "tool")
                tool_input = dict(tool_use.get("input") or {})
                if name == "handoff_to_agent":
                    self._write(event, "handoff", f"Handing off to {tool_input.get('agent_name', '?')}")
                else:
                    self._write(event, "calling", f"{name} {_short_input(tool_input)}")

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = str((event.tool_use or {}).get("name") or "tool")
        if name == "handoff_to_agent":
            return
        status = str((event.result or {}).get("status", "done"))
        self._write(event, "done", f"{name}: {status}")
