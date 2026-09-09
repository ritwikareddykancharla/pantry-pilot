"""FastAPI console endpoints via the httpx-based TestClient (local backend, scripted models)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import server
from pantrypilot import agents, service
from pantrypilot.store import Store
from scripted_model import ScriptedModel

BRIEF = {"period_start": "2026-09-11", "period_end": "2026-09-18", "coverage_pct": 0, "summary": "brief"}


@pytest.fixture
def client(store: Store, monkeypatch):
    model = ScriptedModel(structured=BRIEF, default_text="Nothing to do.")
    monkeypatch.setattr(agents, "build_model", lambda: model)
    monkeypatch.setattr(service, "build_model", lambda: model)
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SWEEP_ON_START", "0")
    server.runner.last_error = None
    with TestClient(server.app) as c:
        yield c


def _wait_idle(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not server.runner.lock.locked() and server.runner.running is None:
            return
        time.sleep(0.05)
    raise AssertionError("background job did not finish")


def test_health_and_state(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["ok"] and health["backend"] == "LocalBackend"
    res = client.get("/api/state")
    assert res.status_code == 200
    state = res.json()
    assert state["org"]["name"] == "Maple Street Community Pantry"
    assert state["counts"]["volunteers"] == 14 and len(state["shifts"]) == 5
    assert len(state["messages"]["inbound"]) == 7 and state["decisions"]["pending"] == []
    assert state["runner"]["running"] is None
    assert client.get("/").status_code == 200 and "Pantry Pilot" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_sweep_runs_in_background_and_updates_state(client: TestClient) -> None:
    res = client.post("/api/sweep")
    assert res.status_code == 200 and res.json()["started"] is True
    _wait_idle()
    state = client.get("/api/state").json()
    assert state["last_cycle"]["kind"] == "sweep" and state["last_cycle"]["handoff_trail"] == ["dispatcher"]
    assert state["last_report"]["summary"] == "brief"
    assert state["last_sweep_at"]


def test_inbound_then_decision_endpoints(client: TestClient, store: Store) -> None:
    res = client.post("/api/inbound", json={"from_id": "V-03", "text": "I could also do Sat 9"})
    assert res.status_code == 200 and res.json()["message"]["from_id"] == "V-03"
    _wait_idle()
    assert store.get_message(res.json()["message"]["id"]) is not None
    assert client.get("/api/state").json()["last_cycle"]["kind"] == "inbound"

    d = store.create_decision(
        kind="escalation",
        summary="Priya vs Marcus",
        options=["Priya", "Marcus"],
        recommendation="Priya",
        created_by="roster",
    )
    res = client.post(f"/api/decisions/{d['id']}", json={"response": "0", "edits": {}})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] and body["decision"]["status"] == "resolved" and body["result"]["chosen"] == "Priya"
    assert body.get("follow_up") in ("started", "queued")
    _wait_idle()
    assert client.post(f"/api/decisions/{d['id']}", json={"response": "1"}).status_code == 400
    assert client.post("/api/decisions/D-404", json={"response": "yes"}).status_code == 400


def test_ask_endpoint(client: TestClient, monkeypatch) -> None:
    model = ScriptedModel([{"text": "Dana, Maria, Ellie."}])
    monkeypatch.setattr(agents, "build_model", lambda: model)
    res = client.post("/api/ask", json={"prompt": "who is on Saturday?"})
    assert res.status_code == 200 and res.json() == {"ok": True, "answer": "Dana, Maria, Ellie."}
    assert client.post("/api/ask", json={"prompt": "  "}).status_code == 400
