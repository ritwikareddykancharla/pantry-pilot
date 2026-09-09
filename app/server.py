"""Coordinator console: FastAPI app serving the static UI and a small JSON API.

Run: ``uvicorn app.server:app --reload --port 8000``

Env:
  AGENT_BACKEND=local|agentcore   (agentcore needs AGENT_RUNTIME_ARN)
  SWEEP_INTERVAL_SECONDS=900      (0 disables the background scheduler)
  SWEEP_ON_START=1                (run one sweep when the server starts)
  DECISION_AUTORUN=1              (after an escalation answer, process the coordinator's reply immediately)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pantrypilot import config, service
from pantrypilot.backend import LocalBackend, build_backend

config.configure_logging()
logger = logging.getLogger("pantrypilot.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Runner:
    """Runs sweeps/inbound cycles in background threads, never two at once, and remembers outcomes."""

    def __init__(self) -> None:
        self.backend = build_backend()
        self.lock = threading.Lock()
        self.running: str | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.queue: list[tuple[str, dict[str, Any]]] = []
        self._scheduler: threading.Thread | None = None
        self._stop = threading.Event()

    # -- jobs -------------------------------------------------------------
    def _run(self, label: str, fn, *args: Any, **kwargs: Any) -> None:
        with self.lock:
            self.running = label
            try:
                self.last_result = fn(*args, **kwargs)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s failed", label)
                self.last_error = f"{label}: {exc}"
            finally:
                self.running = None

    def start(self, label: str, fn, *args: Any, **kwargs: Any) -> bool:
        """Start a background job. Returns False if one is already running (caller may retry)."""
        if self.lock.locked():
            return False
        threading.Thread(target=self._run, args=(label, fn, *args), kwargs=kwargs, daemon=True).start()
        return True

    def start_or_queue(self, label: str, fn, *args: Any, **kwargs: Any) -> str:
        """Start now, or queue behind the running job (a small worker drains the queue)."""
        if self.start(label, fn, *args, **kwargs):
            return "started"

        def wait_then_run() -> None:
            while self.lock.locked():
                time.sleep(0.5)
            self._run(label, fn, *args, **kwargs)

        threading.Thread(target=wait_then_run, daemon=True).start()
        return "queued"

    # -- scheduler --------------------------------------------------------
    def start_scheduler(self) -> None:
        interval = int(os.getenv("SWEEP_INTERVAL_SECONDS", "900") or 0)
        if os.getenv("SWEEP_ON_START", "0") == "1":
            self.start("sweep", self.backend.sweep)
        if interval <= 0:
            logger.info("background scheduler disabled (SWEEP_INTERVAL_SECONDS=0)")
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                if not self.start("sweep", self.backend.sweep):
                    logger.info("scheduled sweep skipped: a cycle is already running")

        self._scheduler = threading.Thread(target=loop, daemon=True, name="pantrypilot-scheduler")
        self._scheduler.start()
        logger.info("background scheduler: sweep every %ss", interval)

    def stop_scheduler(self) -> None:
        self._stop.set()


runner = Runner()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if isinstance(runner.backend, LocalBackend):
        service.ensure_seeded()
    runner.start_scheduler()
    yield
    runner.stop_scheduler()


app = FastAPI(title="Pantry Pilot console", version="0.1.0", lifespan=lifespan)


class DecisionBody(BaseModel):
    response: str = Field(default="", description="yes/no, option index, option text, or free text")
    edits: dict[str, Any] = Field(default_factory=dict)


class AskBody(BaseModel):
    prompt: str


class InboundBody(BaseModel):
    text: str
    from_id: str | None = None
    from_name: str | None = None


def _runner_state() -> dict[str, Any]:
    return {
        "backend": type(runner.backend).__name__,
        "running": runner.running,
        "last_error": runner.last_error,
        "sweep_interval_seconds": int(os.getenv("SWEEP_INTERVAL_SECONDS", "900") or 0),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "pantry-pilot", **_runner_state()}


@app.get("/api/state")
def state() -> dict[str, Any]:
    snapshot = runner.backend.state()
    return {**snapshot, "runner": _runner_state()}


@app.post("/api/sweep")
def sweep() -> dict[str, Any]:
    status = runner.start_or_queue("sweep", runner.backend.sweep)
    return {"started": True, "status": status}


@app.post("/api/decisions/{decision_id}")
def decide(decision_id: str, body: DecisionBody) -> dict[str, Any]:
    result = runner.backend.decide(decision_id, body.response, body.edits)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "decision failed"))
    decision = result.get("decision") or {}
    queued = (result.get("result") or {}).get("queued_message_id")
    autorun = os.getenv("DECISION_AUTORUN", "1") == "1"
    if decision.get("kind") == "escalation" and queued and autorun and isinstance(runner.backend, LocalBackend):
        result["follow_up"] = runner.start_or_queue(f"coordinator reply {queued}", service.process_message, queued)
    return result


@app.post("/api/ask")
def ask(body: AskBody) -> dict[str, Any]:
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    return runner.backend.ask(body.prompt.strip())


@app.post("/api/inbound")
def inbound(body: InboundBody) -> dict[str, Any]:
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if isinstance(runner.backend, LocalBackend):
        message = service.enqueue_inbound(body.text, from_id=body.from_id, from_name=body.from_name)
        status = runner.start_or_queue(f"inbound {message['id']}", service.process_message, message["id"])
        return {"ok": True, "message": message, "status": status}
    status = runner.start_or_queue("inbound", runner.backend.inbound, body.text, body.from_id, body.from_name)
    return {"ok": True, "status": status}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(Exception)
async def _unhandled(_, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
