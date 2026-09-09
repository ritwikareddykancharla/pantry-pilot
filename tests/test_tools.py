"""Domain tools against the seeded data (called directly as functions)."""

from __future__ import annotations

from types import SimpleNamespace

from strands.agent.state import AgentState

from pantrypilot.store import Store
from pantrypilot.tools import common, inventory, roster


def fake_agent(name: str = "roster", approved: bool = False, cycle_id: str = "C-0001") -> SimpleNamespace:
    return SimpleNamespace(name=name, state=AgentState({"cycle_id": cycle_id, "approved": approved}))


def test_schedule_and_open_slots(store: Store) -> None:
    sched = roster.get_schedule(days=7)
    assert sched["count"] == 5  # Sep 12 x2, Sep 15, Sep 17 x2 (Sep 19 is outside 7 days)
    open_slots = roster.get_open_slots(days=7)
    ids = {s["slot_id"]: s for s in open_slots["open_slots"]}
    assert set(ids) == {"S-0915-EVE", "S-0917-DELIVERY", "S-0917-EVE"}
    assert ids["S-0917-DELIVERY"]["open"] == 1 and ids["S-0917-DELIVERY"]["urgent"] is False
    view = roster.get_volunteer("Priya")
    assert view["id"] == "V-02" and "phone" not in view and view["recent_shifts_30d"] == 1


def test_find_candidates_and_assign_rules(store: Store) -> None:
    cands = roster.find_candidates("S-0917-DELIVERY", limit=3)
    # Jorge, Tom and Luis each have one shift in the last 30 days; reliability breaks the tie (Jorge 0.90).
    assert [c["volunteer_id"] for c in cands["candidates"]] == ["V-04", "V-07", "V-09"]
    # Jorge: driver, confirmed
    out = roster.assign_volunteer("S-0917-DELIVERY", "V-04", "confirmed", agent=fake_agent())
    assert out["ok"] and out["slot"]["filled"] == 1 and out["slot"]["open"] == 0
    # Slot now full: a second person is refused with an escalation hint.
    full = roster.assign_volunteer("S-0917-DELIVERY", "V-07", "confirmed", agent=fake_agent())
    assert not full["ok"] and "escalate" in full["error"]
    # Skill rule: Priya (no driver skill) cannot take the delivery run.
    bad = roster.assign_volunteer("S-0917-DELIVERY", "V-02", "tentative", agent=fake_agent())
    assert not bad["ok"] and "skills" in bad["error"]
    # Minor rule: Sam cannot be confirmed on Tuesday (no supervisor), tentative is allowed with a warning.
    tue = store.shift("S-0915-EVE")
    tue["required_skills"] = ["sorting"]
    store.put_doc("shift", tue["id"], tue)
    refused = roster.assign_volunteer("S-0915-EVE", "V-05", "confirmed", agent=fake_agent())
    assert not refused["ok"] and "minor" in refused["error"].lower()
    tentative = roster.assign_volunteer("S-0915-EVE", "V-05", "tentative", agent=fake_agent())
    assert tentative["ok"] and tentative["warnings"] and "Sam Okafor" in tentative["slot"]["minors_on_roster"]


def test_unassign_and_last_minute_cover(store: Store) -> None:
    out = roster.unassign_volunteer("S-0912-INTAKE", "V-01", "kid sick", agent=fake_agent())
    assert out["ok"] and out["slot"]["open"] == 1 and out["slot"]["filled"] == 2
    cover = roster.assign_volunteer("S-0912-INTAKE", "V-02", "confirmed", agent=fake_agent())
    assert cover["ok"]
    entry = next(r for r in store.shift("S-0912-INTAKE")["roster"] if r["volunteer_id"] == "V-02")
    assert entry.get("last_minute") is True
    missing = roster.unassign_volunteer("S-0912-INTAKE", "V-09", "n/a", agent=fake_agent())
    assert not missing["ok"]


def test_reminders_availability_hours(store: Store) -> None:
    out = roster.send_shift_reminders("S-0912-INTAKE", agent=fake_agent())
    assert out["ok"] and out["sent"] == 3
    again = roster.send_shift_reminders("S-0912-INTAKE", agent=fake_agent())
    assert again["sent"] == 0
    outbound = store.list_messages(direction="outbound")
    assert len(outbound) == 3 and all(m["kind"] == "reminder" for m in outbound)
    avail = roster.record_availability("V-02", [{"day": "tue", "start": "16:00", "end": "19:00"}], agent=fake_agent())
    assert avail["ok"] and store.volunteer("V-02")["availability"][0]["day"] == "tue"
    bad = roster.record_availability("V-02", [{"day": "someday", "start": "1", "end": "2"}], agent=fake_agent())
    assert not bad["ok"]
    hrs = roster.log_hours("V-04", "S-0917-DELIVERY", 2, "run", agent=fake_agent())
    assert hrs["ok"] and "2026-09-17" in store.volunteer("V-04")["recent_shifts"]


def test_storage_capacity_turkeys(store: Store) -> None:
    out = inventory.check_storage_capacity("frozen", 30, unit="turkeys")
    assert out["ok"] and out["fits"] is False
    assert out["capacity"] == 18 and out["used"] == 10.5 and out["free"] == 7.5
    assert out["max_units_that_fit"] == 10
    ok = inventory.check_storage_capacity("dry", 60, unit="lbs")
    assert ok["fits"] is True
    assert not inventory.check_storage_capacity("attic", 1)["ok"]


def test_donation_logging_and_dropoff(store: Store) -> None:
    windows = inventory.propose_dropoff_window("C-LINDA", preferred="Saturday")
    assert windows["donor"] == "Linda Moreno"
    assert windows["recommended"][0]["date"] == "2026-09-12" and windows["recommended"][0]["start"] == "09:00"
    pledged = inventory.log_donation(
        "Linda",
        [{"item": "Rice", "qty": 40, "unit": "lbs"}, {"item": "Canned beans", "qty": 20, "unit": "cans"}],
        scheduled_dropoff="2026-09-12 09:00-10:00",
        agent=fake_agent("steward"),
    )
    assert pledged["ok"] and pledged["status"] == "scheduled" and pledged["inventory_updates"] == []
    before = next(i for i in store.inventory() if i["id"] == "I-02")["qty"]
    received = inventory.log_donation(
        "Linda", [{"item": "canned beans", "qty": 20, "unit": "cans"}], received=True, agent=fake_agent("steward")
    )
    assert received["status"] == "received"
    after = next(i for i in store.inventory() if i["id"] == "I-02")["qty"]
    assert after == before + 20
    assert len(store.list_donations()) == 2


def test_inventory_views_and_donor_draft(store: Store) -> None:
    below = inventory.get_below_par()
    names = [i["item"] for i in below["items"]]
    assert "Diapers size 4" in names and "Canned beans" in names and "Rice (5 lb bags)" not in names
    expiring = inventory.get_expiring(7)
    assert [i["item"] for i in expiring["items"]][:2] == ["Milk (gallons)", "Yogurt cups"]
    assert expiring["items"][0]["days_to_expiry"] == 2
    draft = inventory.draft_donor_ask(agent=fake_agent("steward"))
    assert draft["ok"] and "Diapers size 4" in draft["draft"]
    drafts = [m for m in store.list_messages(direction="outbound") if m["kind"] == "draft"]
    assert len(drafts) == 1 and drafts[0]["meta"]["status"] == "draft"


def test_send_message_and_inbox(store: Store) -> None:
    inbox = common.list_inbound_messages()
    assert inbox["count"] == 7 and inbox["messages"][0]["from_type"] == "volunteer"
    assert inbox["messages"][3]["from_type"] == "contact"
    sent = common.send_message("Hi Jorge, yes the run is on.", to_volunteer_id="V-04", agent=fake_agent())
    assert sent["ok"] and sent["to"] == "Jorge Alvarez" and sent["status"] in ("sent", "queued_until_morning")
    by_name = common.send_message("Thanks Linda.", to_contact="Linda Moreno", agent=fake_agent("steward"))
    assert by_name["ok"] and by_name["to_id"] == "C-LINDA"
    assert not common.send_message("x", to_contact="Nobody Real")["ok"]
    assert not common.send_message("x", to_contact="coordinator")["ok"]
    assert common.mark_message_handled("M-007", "replied", agent=fake_agent())["ok"]
    assert common.list_inbound_messages()["count"] == 6
    rules = common.get_org_rules()
    assert rules["rules"]["purchase_approval_threshold_usd"] == 50 and "phone" not in rules["coordinator"]
    assert common.today()["weekday"] == "Friday"


def test_escalation_and_gated_tools_create_decisions_only(store: Store) -> None:
    esc = common.escalate_to_coordinator(
        "Priya and Marcus both want the last Saturday intake spot.",
        ["Give it to Priya", "Give it to Marcus", "Add a 4th spot"],
        "Give it to Priya: fewest recent shifts.",
        agent=fake_agent("roster"),
    )
    assert esc["ok"] and esc["decision_id"] == "D-001"
    d = store.get_decision("D-001")
    assert d["kind"] == "escalation" and len(d["options"]) == 3 and d["created_by"] == "roster"
    dup = common.escalate_to_coordinator(
        "Priya and Marcus both want the last Saturday intake spot.", ["a", "b"], "a", agent=fake_agent("roster")
    )
    assert dup["already_filed"] and dup["decision_id"] == "D-001"
    assert not common.escalate_to_coordinator("x", ["only one"], "x")["ok"]

    bc = common.send_broadcast("Pantry closed Saturday for a water leak.", agent=fake_agent("dispatcher"))
    assert bc["status"] == "pending_approval" and bc["decision_id"] == "D-002"
    assert store.list_messages(direction="outbound") == []  # nothing sent
    assert store.get_decision("D-002")["payload"]["tool"] == "send_broadcast"

    small = inventory.place_supply_order(
        [{"item": "Tape", "qty": 2, "unit": "rolls"}], "Restaurant Depot", 12.5, agent=fake_agent("steward")
    )
    assert small["status"] == "placed" and small["order"]["approved_by"].startswith("auto")
    big = inventory.place_supply_order(
        [{"item": "Diapers size 4", "qty": 8, "unit": "packs", "unit_cost": 24.99}],
        "Restaurant Depot",
        199.92,
        agent=fake_agent("steward"),
    )
    assert big["status"] == "pending_approval" and store.get_decision(big["decision_id"])["kind"] == "approval"
    approved = inventory.place_supply_order(
        [{"item": "Diapers size 4", "qty": 8, "unit": "packs"}],
        "Restaurant Depot",
        199.92,
        agent=fake_agent("dispatcher", approved=True),
    )
    assert approved["status"] == "placed" and approved["order"]["approved_by"] == "coordinator"
