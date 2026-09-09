"""Deterministic scripted model for offline tests.

``ScriptedModel`` implements ``strands.models.Model``. It is given a list of turns; each call
to ``stream()`` plays the next turn as Bedrock-style stream events:

- ``{"text": "..."}``                 -> assistant text, stopReason end_turn
- ``{"tool": "name", "input": {...}}`` -> a toolUse block, stopReason tool_use
- ``{"tools": [{"tool":..., "input":...}, ...]}`` -> several toolUse blocks in one turn

When the script is exhausted the model answers with a short end_turn text so agent loops
always terminate. ``structured_output()`` yields ``{"output": OutputModel(**scripted)}``.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

T = TypeVar("T", bound=BaseModel)


class ScriptedModel(Model):
    """Plays back scripted turns. Records every request it received in ``calls``."""

    def __init__(
        self,
        turns: list[dict[str, Any]] | None = None,
        *,
        name: str = "scripted",
        default_text: str = "Done.",
        structured: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.turns: list[dict[str, Any]] = list(turns or [])
        self.default_text = default_text
        self.structured = structured or {}
        self.calls: list[dict[str, Any]] = []
        self._config: dict[str, Any] = {"model_id": f"scripted:{name}", "max_tokens": 1024}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ config
    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    # ------------------------------------------------------------------ helpers
    def _next_turn(self) -> dict[str, Any]:
        with self._lock:
            if self.turns:
                return self.turns.pop(0)
        return {"text": self.default_text}

    @property
    def remaining(self) -> int:
        return len(self.turns)

    @staticmethod
    def _tool_events(tool_calls: list[dict[str, Any]]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for index, call in enumerate(tool_calls):
            tool_use_id = call.get("toolUseId") or f"tooluse_{uuid.uuid4().hex[:12]}"
            events.append(
                {
                    "contentBlockStart": {
                        "contentBlockIndex": index,
                        "start": {"toolUse": {"toolUseId": tool_use_id, "name": call["tool"]}},
                    }
                }
            )
            events.append(
                {
                    "contentBlockDelta": {
                        "contentBlockIndex": index,
                        "delta": {"toolUse": {"input": json.dumps(call.get("input", {}))}},
                    }
                }
            )
            events.append({"contentBlockStop": {"contentBlockIndex": index}})
        return events

    # ------------------------------------------------------------------ Model API
    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[Any] | None = None,
        invocation_state: dict[str, Any] | None = None,
        cancel_signal: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        # When the agent loop forces structured output it passes the single structured-output
        # tool spec with tool_choice {"any": {}} (or {"tool": {"name": ...}}). If the script has
        # no explicit tool turn for it, synthesize one from ``self.structured``.
        turn = self._next_turn()
        if tool_choice and "tool" not in turn and "tools" not in turn:
            forced = None
            if "tool" in tool_choice:
                forced = tool_choice["tool"]["name"]
            elif tool_specs and len(tool_specs) == 1:
                forced = tool_specs[0]["name"]
            if forced:
                turn = {"tool": forced, "input": self.structured}
        self.calls.append(
            {
                "messages": messages,
                "tool_names": [t["name"] for t in (tool_specs or [])],
                "system_prompt": system_prompt,
                "turn": turn,
            }
        )

        yield {"messageStart": {"role": "assistant"}}
        if "tool" in turn or "tools" in turn:
            calls = turn.get("tools") or [{"tool": turn["tool"], "input": turn.get("input", {})}]
            for event in self._tool_events(calls):
                yield event
            stop_reason = "tool_use"
        else:
            text = str(turn.get("text", self.default_text))
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            stop_reason = "end_turn"
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "metrics": {"latencyMs": 5},
            }
        }

    async def structured_output(
        self, output_model: type[T], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        yield {"output": output_model(**self.structured)}


def model_for_factory(models: dict[str, ScriptedModel], default: ScriptedModel | None = None):
    """Return a ``model_for(name)`` callable for ``agents.build_swarm``."""

    def model_for(name: str) -> ScriptedModel:
        if name in models:
            return models[name]
        if default is not None:
            return default
        models[name] = ScriptedModel(name=name)
        return models[name]

    return model_for
