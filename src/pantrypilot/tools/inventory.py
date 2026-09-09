"""Steward tools: inventory, donations, drop-off windows, storage capacity, supply orders."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from strands import tool

from .. import config
from . import _ctx


def _item_view(item: dict[str, Any], today: date) -> dict[str, Any]:
    expiry = item.get("expiry")
    days_to_expiry = (date.fromisoformat(expiry) - today).days if expiry else None
    return {
        "id": item["id"],
        "item": item["item"],
        "qty": item["qty"],
        "unit": item["unit"],
        "par": item["par"],
        "below_par": item["qty"] < item["par"],
        "shortfall": max(0, item["par"] - item["qty"]),
        "category": item.get("category"),
        "expiry": expiry,
        "days_to_expiry": days_to_expiry,
    }


@tool
def get_inventory() -> dict[str, Any]:
    """Return the full inventory with par levels, shortfalls and expiry dates.

    Returns:
        items: list of id, item, qty, unit, par, below_par, shortfall, category, expiry, days_to_expiry.
    """
    today = config.today()
    items = [_item_view(i, today) for i in _ctx.store().inventory()]
    return {"count": len(items), "items": items}


@tool
def get_below_par() -> dict[str, Any]:
    """Return only inventory items below their par level, biggest shortfall first.

    Use for the weekly brief, the donor "most needed" draft, and supply orders.

    Returns:
        items (same shape as get_inventory) sorted by shortfall descending.
    """
    today = config.today()
    items = [_item_view(i, today) for i in _ctx.store().inventory() if i["qty"] < i["par"]]
    items.sort(key=lambda x: (-x["shortfall"] / max(1, x["par"]), x["item"]))
    return {"count": len(items), "items": items}


@tool
def get_expiring(days: int = 7) -> dict[str, Any]:
    """Return items expiring within N days (soonest first) so they can be moved out first.

    Args:
        days: Horizon in days (default 7).

    Returns:
        items with days_to_expiry, soonest first.
    """
    today = config.today()
    out = []
    for item in _ctx.store().inventory():
        view = _item_view(item, today)
        if view["days_to_expiry"] is not None and view["days_to_expiry"] <= int(days):
            out.append(view)
    out.sort(key=lambda x: x["days_to_expiry"])
    return {"count": len(out), "items": out}


@tool
def check_storage_capacity(kind: str, qty: float, unit: str = "cuft") -> dict[str, Any]:
    """Check whether a quantity of dry, cold or frozen goods fits in free storage.

    Call before accepting any large or cold/frozen donation. For frozen turkeys pass
    unit="turkeys" (converted with the org's per-turkey volume); for other goods pass the
    storage unit (lbs for dry, cuft for cold/frozen).

    Args:
        kind: "dry", "cold" or "frozen".
        qty: Amount offered.
        unit: "cuft", "lbs" or "turkeys" (default "cuft").

    Returns:
        capacity, used, free, requested (in storage units), fits (bool), and max_units_that_fit
        expressed in the unit you passed.
    """
    s = _ctx.store()
    storage = (s.org().get("storage") or {}).get(kind)
    if not storage:
        return {"ok": False, "error": f"Unknown storage kind '{kind}'; use dry, cold or frozen"}
    per_unit = 1.0
    if unit == "turkeys":
        per_unit = float(storage.get("turkey_cuft", 0.75))
    used = 0.0
    for item in s.inventory():
        if item.get("storage_kind") == kind:
            used += float(item["qty"]) * float(item.get("storage_units_per_unit", 1.0))
    capacity = float(storage["capacity"])
    free = max(0.0, capacity - used)
    requested = float(qty) * per_unit
    return {
        "ok": True,
        "kind": kind,
        "label": storage.get("label"),
        "storage_unit": storage.get("unit"),
        "capacity": capacity,
        "used": round(used, 1),
        "free": round(free, 1),
        "requested": round(requested, 1),
        "fits": requested <= free,
        "max_units_that_fit": int(free // per_unit) if per_unit else 0,
        "unit": unit,
    }


def _open_windows(days: int, today: date, open_hours: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for offset in range(1, days + 1):
        d = today + timedelta(days=offset)
        key = config.day_key(d)
        for oh in open_hours:
            if oh["day"] == key:
                out.append({"date": d.isoformat(), "day": _ctx.pretty_date(d), "start": oh["start"], "end": oh["end"]})
    return out


@tool
def propose_dropoff_window(donor_contact: str, preferred: str | None = None) -> dict[str, Any]:
    """Propose the next drop-off windows inside open hours for a donor.

    Args:
        donor_contact: Contact id ("C-LINDA") or donor name.
        preferred: Optional free text such as "Saturday" or "tomorrow" to bias the choice.

    Returns:
        donor, up to three windows (date, label, start, end) with the first one recommended.
    """
    s = _ctx.store()
    today = config.today()
    person = _ctx.resolve_person(donor_contact) or {"name": donor_contact, "id": None}
    windows = _open_windows(7, today, s.org().get("open_hours", []))
    if preferred:
        p = preferred.lower()
        if "tomorrow" in p:
            target = (today + timedelta(days=1)).isoformat()
            windows.sort(key=lambda w: w["date"] != target)
        else:
            windows.sort(key=lambda w: p[:3] not in w["day"].lower())
    proposals = []
    for w in windows[:3]:
        start = config.parse_hhmm(w["start"])
        first_hour_end = f"{(start.hour + 1) % 24:02d}:{start.minute:02d}"
        proposals.append(
            {
                "date": w["date"],
                "label": f"{w['day']} {_ctx.pretty_time(w['start'])}-{_ctx.pretty_time(first_hour_end)}",
                "start": w["start"],
                "end": first_hour_end,
                "open_until": w["end"],
            }
        )
    return {
        "donor": person.get("name"),
        "contact_id": person.get("id"),
        "recommended": proposals[:1],
        "alternatives": proposals[1:],
    }


@tool
def log_donation(
    donor: str,
    items: list[dict[str, Any]],
    scheduled_dropoff: str | None = None,
    received: bool = False,
    agent: Any = None,
) -> dict[str, Any]:
    """Record a donation (pledged or received) and, if received, add it to inventory.

    Args:
        donor: Donor name or contact id.
        items: List of {"item": "Rice", "qty": 40, "unit": "lbs"}.
        scheduled_dropoff: When they will drop off, e.g. "2026-09-13 09:00-10:00".
        received: True if the goods are already here (updates inventory quantities).

    Returns:
        donation id, status ("scheduled" or "received"), and inventory updates applied.
    """
    s = _ctx.store()
    if not items:
        return {"ok": False, "error": "items is empty"}
    person = _ctx.resolve_person(donor)
    donor_name = person["name"] if person else donor
    updates = []
    if received:
        inventory = s.inventory()
        for it in items:
            name = str(it.get("item", "")).lower()
            match = next(
                (inv for inv in inventory if name and (name in inv["item"].lower() or inv["item"].lower() in name)),
                None,
            )
            if match:
                match["qty"] = float(match["qty"]) + float(it.get("qty", 0))
                if float(match["qty"]).is_integer():
                    match["qty"] = int(match["qty"])
                s.put_doc("inventory", match["id"], match)
                updates.append({"id": match["id"], "item": match["item"], "new_qty": match["qty"]})
    donation = s.add_donation(
        donor=donor_name,
        items=items,
        scheduled_dropoff=scheduled_dropoff,
        status="received" if received else "scheduled",
        cycle_id=_ctx.cycle_id_of(agent),
    )
    return {
        "ok": True,
        "donation_id": donation["id"],
        "donor": donor_name,
        "status": donation["status"],
        "inventory_updates": updates,
    }


@tool
def place_supply_order(items: list[dict[str, Any]], vendor: str, est_cost: float, agent: Any = None) -> dict[str, Any]:
    """Place a supply purchase. GATED above the org's threshold: needs coordinator approval.

    Below the threshold (see get_org_rules, default 50 USD) the order is logged immediately.
    Above it, calling this only files an approval request with the exact order; nothing is
    bought until the coordinator approves in the console.

    Args:
        items: List of {"item": "Diapers size 4", "qty": 8, "unit": "packs", "unit_cost": 24.99}.
        vendor: Vendor name or contact id, e.g. "Restaurant Depot".
        est_cost: Estimated total cost in USD.

    Returns:
        status "placed" with an order id, or "pending_approval" with a decision id.
    """
    s = _ctx.store()
    if not items:
        return {"ok": False, "error": "items is empty"}
    threshold = float((s.org().get("rules") or {}).get("purchase_approval_threshold_usd", 50))
    est_cost = float(est_cost)
    if est_cost > threshold and not _ctx.is_approved(agent):
        summary = f"Supply order from {vendor} for about ${est_cost:,.2f}"
        existing = s.find_pending_decision("approval", summary)
        if existing:
            return {"ok": True, "status": "pending_approval", "decision_id": existing["id"], "already_filed": True}
        decision = s.create_decision(
            kind="approval",
            summary=summary,
            options=["Approve order", "Decline"],
            recommendation="Approve order" if est_cost <= 4 * threshold else "Decline unless urgent",
            payload={
                "tool": "place_supply_order",
                "input": {"items": items, "vendor": vendor, "est_cost": est_cost},
                "table": items,
            },
            created_by=_ctx.agent_name(agent),
            cycle_id=_ctx.cycle_id_of(agent),
        )
        return {"ok": True, "status": "pending_approval", "decision_id": decision["id"], "note": "Nothing ordered yet."}
    orders = s.get_meta("orders", [])
    order = {
        "id": f"PO-{len(orders) + 1:03d}",
        "ts": config.now_iso(),
        "vendor": vendor,
        "items": items,
        "est_cost": est_cost,
        "approved_by": "coordinator" if _ctx.is_approved(agent) else "auto (under threshold)",
    }
    orders.append(order)
    s.set_meta("orders", orders)
    return {"ok": True, "status": "placed", "order": order}


@tool
def draft_donor_ask(agent: Any = None) -> dict[str, Any]:
    """Draft a "most needed items" message for donors from the below-par list (draft only).

    Routine: the draft is saved to the outbox as a draft for the coordinator to copy or edit.
    It is never sent automatically.

    Returns:
        draft text and the items it lists.
    """
    s = _ctx.store()
    today = config.today()
    below = sorted(
        (_item_view(i, today) for i in s.inventory() if i["qty"] < i["par"]),
        key=lambda x: -x["shortfall"] / max(1, x["par"]),
    )
    if not below:
        return {"ok": True, "draft": None, "note": "Nothing is below par; no ask needed this week."}
    org = s.org()
    lines = [f"- {i['item']}: need about {i['shortfall']} {i['unit']}" for i in below[:6]]
    open_hours = ", ".join(
        f"{oh['day'].title()} {_ctx.pretty_time(oh['start'])}-{_ctx.pretty_time(oh['end'])}"
        for oh in org.get("open_hours", [])
    )
    draft = (
        f"{org.get('name', 'Our pantry')} most-needed items this week:\n"
        + "\n".join(lines)
        + f"\n\nDrop-offs: {open_hours} at {org.get('address', 'the pantry')}. Thank you for keeping shelves full."
    )
    msg = s.add_message(
        direction="outbound",
        body=draft,
        from_id="pantry",
        from_name="Pantry Pilot",
        to_id="donors",
        to_name="Donor list (draft)",
        channel="draft",
        kind="draft",
        cycle_id=_ctx.cycle_id_of(agent),
        meta={"status": "draft", "by": _ctx.agent_name(agent)},
    )
    return {"ok": True, "draft_id": msg["id"], "draft": draft, "items": [i["item"] for i in below[:6]]}
