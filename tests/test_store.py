"""Store CRUD against the seeded temp database."""

from __future__ import annotations

from pantrypilot.store import Store


def test_seed_counts(store: Store) -> None:
    counts = store.counts()
    assert counts["volunteers"] == 14
    assert counts["shifts"] == 8
    assert counts["inventory_items"] == 18
    assert counts["inbound_unhandled"] == 7
    assert store.org()["name"] == "Maple Street Community Pantry"


def test_doc_roundtrip_and_delete(store: Store) -> None:
    store.put_doc("volunteer", "V-99", {"name": "Test Person", "skills": ["intake"]})
    doc = store.get_doc("volunteer", "V-99")
    assert doc is not None and doc["id"] == "V-99" and doc["name"] == "Test Person"
    doc["skills"].append("driver")
    store.put_doc("volunteer", "V-99", doc)
    assert store.volunteer("V-99")["skills"] == ["intake", "driver"]
    store.delete_doc("volunteer", "V-99")
    assert store.get_doc("volunteer", "V-99") is None
    assert len(store.volunteers()) == 14


def test_messages_add_list_mark_handled(store: Store) -> None:
    msg = store.add_message(direction="outbound", body="hello", to_id="V-01", to_name="Dana", kind="reminder")
    assert msg["id"].startswith("M-") and msg["direction"] == "outbound" and msg["handled"] is False
    assert len(store.list_messages(direction="outbound")) == 1
    assert store.mark_message_handled("M-001", "answered", cycle_id="C-0001")
    handled = store.get_message("M-001")
    assert handled["handled"] is True and handled["handled_note"] == "answered" and handled["cycle_id"] == "C-0001"
    assert len(store.list_messages(direction="inbound", handled=False)) == 6
    assert not store.mark_message_handled("M-999", "nope")


def test_decisions_create_find_resolve(store: Store) -> None:
    d = store.create_decision(
        kind="escalation",
        summary="Two people want Saturday intake",
        options=["Priya", "Marcus"],
        recommendation="Priya",
        created_by="roster",
        cycle_id="C-0001",
    )
    assert d["id"] == "D-001" and d["status"] == "pending" and d["options"] == ["Priya", "Marcus"]
    assert store.find_pending_decision("escalation", "Two people want Saturday intake")["id"] == "D-001"
    resolved = store.resolve_decision("D-001", "resolved", "Priya", {"chosen": "Priya"})
    assert resolved["status"] == "resolved" and resolved["result"] == {"chosen": "Priya"}
    assert store.find_pending_decision("escalation", "Two people want Saturday intake") is None
    assert store.counts()["decisions_resolved"] == 1


def test_audit_cycles_reports_meta(store: Store) -> None:
    cycle = store.start_cycle("sweep", "task text")
    assert cycle["id"].startswith("C-0001-") and cycle["status"] == "running"
    store.add_audit(cycle_id=cycle["id"], agent="roster", tool="assign_volunteer", input={"a": 1}, result="ok")
    store.add_audit(cycle_id=cycle["id"], agent="dispatcher", tool="handoff_to_agent", kind="handoff")
    entries = store.list_audit(cycle_id=cycle["id"])
    assert [e["tool"] for e in entries] == ["assign_volunteer", "handoff_to_agent"]
    assert entries[0]["input"] == {"a": 1}
    finished = store.finish_cycle(
        cycle["id"], status="completed", handoff_trail=["dispatcher", "roster"], summary="done"
    )
    assert finished["handoff_trail"] == ["dispatcher", "roster"] and finished["finished_at"]
    store.save_report(cycle["id"], {"coverage_pct": 75.0})
    assert store.last_report()["coverage_pct"] == 75.0
    store.set_meta("last_sweep_at", "2026-09-11T09:00:00")
    assert store.get_meta("last_sweep_at") == "2026-09-11T09:00:00"
    assert store.get_meta("missing", "dflt") == "dflt"
    assert store.last_cycle()["id"] == cycle["id"]


def test_hours_and_donations(store: Store) -> None:
    store.log_hours("V-04", "S-0917-DELIVERY", 2.0, "delivery run")
    assert sum(h["hours"] for h in store.list_hours()) == 2.0
    d = store.add_donation(
        donor="Linda Moreno", items=[{"item": "Rice", "qty": 40, "unit": "lbs"}], scheduled_dropoff="Sat 9am"
    )
    assert d["id"] == "DN-001" and d["status"] == "scheduled" and d["items"][0]["qty"] == 40
    assert len(store.list_donations()) == 1
