"""Strands tools for the three Pantry Pilot agents.

Tool groups:

- ``common``: messaging, escalation, org rules, inbox (dispatcher + shared)
- ``roster``: schedule, candidates, assignments, hours
- ``inventory``: stock, donations, storage capacity, supply orders
"""

from .common import (
    escalate_to_coordinator,
    get_org_rules,
    list_inbound_messages,
    mark_message_handled,
    send_broadcast,
    send_message,
    today,
)
from .inventory import (
    check_storage_capacity,
    draft_donor_ask,
    get_below_par,
    get_expiring,
    get_inventory,
    log_donation,
    place_supply_order,
    propose_dropoff_window,
)
from .roster import (
    assign_volunteer,
    find_candidates,
    get_open_slots,
    get_schedule,
    get_volunteer,
    log_hours,
    record_availability,
    send_shift_reminders,
    unassign_volunteer,
)

DISPATCHER_TOOLS = [
    list_inbound_messages,
    mark_message_handled,
    send_message,
    send_broadcast,
    escalate_to_coordinator,
    get_org_rules,
    today,
    get_schedule,
]

ROSTER_TOOLS = [
    get_schedule,
    get_open_slots,
    get_volunteer,
    find_candidates,
    assign_volunteer,
    unassign_volunteer,
    record_availability,
    send_shift_reminders,
    log_hours,
    send_message,
    escalate_to_coordinator,
    mark_message_handled,
    today,
]

STEWARD_TOOLS = [
    get_inventory,
    get_below_par,
    get_expiring,
    log_donation,
    propose_dropoff_window,
    check_storage_capacity,
    place_supply_order,
    draft_donor_ask,
    send_message,
    escalate_to_coordinator,
    mark_message_handled,
    today,
]

READ_ONLY_TOOLS = [
    today,
    get_org_rules,
    get_schedule,
    get_open_slots,
    get_volunteer,
    find_candidates,
    get_inventory,
    get_below_par,
    get_expiring,
    list_inbound_messages,
]

GATED_TOOLS = {"send_broadcast": send_broadcast, "place_supply_order": place_supply_order}

__all__ = [
    "DISPATCHER_TOOLS",
    "GATED_TOOLS",
    "READ_ONLY_TOOLS",
    "ROSTER_TOOLS",
    "STEWARD_TOOLS",
]
