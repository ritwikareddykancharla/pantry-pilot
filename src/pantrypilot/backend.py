"""Backends for the web app: run the agent in-process or call an AgentCore Runtime.

Select with env ``AGENT_BACKEND=local|agentcore``. The AgentCore backend needs
``AGENT_RUNTIME_ARN`` and AWS credentials in the environment (never in code or config).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Protocol

from . import service


class Backend(Protocol):
    """What the web server needs from an agent backend."""

    def sweep(self) -> dict[str, Any]: ...

    def decide(self, decision_id: str, response: str, edits: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def ask(self, prompt: str) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def inbound(self, text: str, from_id: str | None = None, from_name: str | None = None) -> dict[str, Any]: ...

    def state(self) -> dict[str, Any]: ...


class LocalBackend:
    """Runs the Strands swarm in this process against the local SQLite store."""

    def sweep(self) -> dict[str, Any]:
        return service.run_sweep()

    def decide(self, decision_id: str, response: str, edits: dict[str, Any] | None = None) -> dict[str, Any]:
        return service.decide(decision_id, response, edits)

    def ask(self, prompt: str) -> dict[str, Any]:
        return {"ok": True, "answer": service.ask(prompt)}

    def status(self) -> dict[str, Any]:
        return service.status()

    def inbound(self, text: str, from_id: str | None = None, from_name: str | None = None) -> dict[str, Any]:
        return service.process_inbound(text, from_id=from_id, from_name=from_name)

    def state(self) -> dict[str, Any]:
        return service.state_snapshot()


class AgentCoreBackend:
    """Invokes the deployed Bedrock AgentCore Runtime with the same payload contract as ``main.py``."""

    def __init__(self, runtime_arn: str | None = None, region: str | None = None, client: Any | None = None) -> None:
        self.runtime_arn = runtime_arn or os.environ["AGENT_RUNTIME_ARN"]
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self._client = client
        # AgentCore requires a runtime session id of at least 33 characters; keep it stable per process.
        self.session_id = os.getenv("AGENT_RUNTIME_SESSION_ID") or f"pantrypilot-console-{uuid.uuid4().hex}"

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-agentcore", region_name=self.region)
        return self._client

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=self.session_id,
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = response["response"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body) if body else {"ok": False, "error": "empty response"}

    def sweep(self) -> dict[str, Any]:
        return self.invoke({"action": "sweep"})

    def decide(self, decision_id: str, response: str, edits: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.invoke({"action": "decide", "decision_id": decision_id, "response": response, "edits": edits or {}})

    def ask(self, prompt: str) -> dict[str, Any]:
        return self.invoke({"action": "ask", "prompt": prompt})

    def status(self) -> dict[str, Any]:
        return self.invoke({"action": "status"})

    def inbound(self, text: str, from_id: str | None = None, from_name: str | None = None) -> dict[str, Any]:
        return self.invoke({"action": "inbound", "from": from_id or from_name, "from_name": from_name, "text": text})

    def state(self) -> dict[str, Any]:
        return self.invoke({"action": "state"})


def build_backend() -> Backend:
    kind = os.getenv("AGENT_BACKEND", "local").lower()
    if kind == "agentcore":
        return AgentCoreBackend()
    return LocalBackend()
