"""Backends for the web app: run the agent in-process or call an AgentCore Runtime.

Select with env ``AGENT_BACKEND=local|agentcore``. The AgentCore backend needs
``AGENT_RUNTIME_ARN`` and AWS credentials in the environment (never in code or config).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Protocol

from . import service

# A sweep is one synchronous InvokeAgentRuntime call that can run for minutes. boto3's defaults
# (60 s read timeout, automatic retries) would time out and then re-run the sweep, so the client
# waits up to 15 minutes and never retries.
INVOKE_READ_TIMEOUT_SECONDS = 900
COLD_START_RETRIES = 3
COLD_START_RETRY_SECONDS = 4


def _is_cold_start_error(exc: Exception) -> bool:
    """AgentCore surfaces a runtime that is still booting as RuntimeClientError with a 502."""
    text = str(exc)
    return "RuntimeClientError" in text and "502" in text


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
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-agentcore",
                region_name=self.region,
                config=Config(
                    read_timeout=INVOKE_READ_TIMEOUT_SECONDS,
                    connect_timeout=10,
                    retries={"total_max_attempts": 1},
                ),
            )
        return self._client

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A brand-new session pays a cold start (a microVM boots and imports the code); the first
        # request in that window can bounce with a gateway 502 before our app ever saw it, so it
        # is safe to retry. Anything else is raised as-is, and botocore itself never retries.
        for attempt in range(COLD_START_RETRIES + 1):
            try:
                response = self.client.invoke_agent_runtime(
                    agentRuntimeArn=self.runtime_arn,
                    runtimeSessionId=self.session_id,
                    payload=json.dumps(payload).encode("utf-8"),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= COLD_START_RETRIES or not _is_cold_start_error(exc):
                    raise
                time.sleep(COLD_START_RETRY_SECONDS)
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
