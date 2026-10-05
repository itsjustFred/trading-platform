"""Every order status the installed protos expose, and what the code does with each.

`_is_working_order` treats **only** `ORDER_STATUS_ACCEPTED` as working:

    working_statuses = {int(status_enum.ORDER_STATUS_ACCEPTED)}

Anything else is not working, so a cancel sweep leaves it alone. That is a safe default for the
five statuses the installed protos expose - ACCEPTED, FILLED, REJECTED, EXPIRED, CANCELLED - where
four are settled. It is **not** safe for a status nobody here has seen.

**The risk this file closes.** If the broker adds a sixth value that is still working in
substance - a `PENDING_NEW` or an equivalent - the code would classify it as not working, the
sweep would skip it, and the order would be left resting with nothing wrong visible anywhere.
The values are read as bare ints (`orderStatus` has presence, so it is always an `int`), so
there is no enum lookup that would fail loudly; an unknown int simply is not in the set.

`_unrecognised_status_wire_value` does log an unrecognised wire value, so the *event* is
visible. But a warning about a new status does not say whether it should have been swept,
and nobody is watching that line. A test that fails when the enum grows is the difference
between noticing at merge time and noticing in production.
noticing in production.

So the inventory is asserted exactly rather than tolerated. If a proto update adds a value, this
suite goes red and the new status has to be classified deliberately - which is item 2's whole
subject, one level down from the decision about *direction*.
"""

from __future__ import annotations

import pytest

# The five values measured against the installed protos, 2026-10-05. Written out so a change is a
# diff a reader can judge, not a number that silently moves.
KNOWN_STATUSES = {
    "ORDER_STATUS_ACCEPTED": 1,
    "ORDER_STATUS_FILLED": 2,
    "ORDER_STATUS_REJECTED": 3,
    "ORDER_STATUS_EXPIRED": 4,
    "ORDER_STATUS_CANCELLED": 5,
}

# Only ACCEPTED is cancellable, so only ACCEPTED is treated as working. Every other known value must
# be settled, or a sweep would be cancelling orders the broker has already finished with.
SETTLED_STATUSES = {
    "ORDER_STATUS_FILLED",
    "ORDER_STATUS_REJECTED",
    "ORDER_STATUS_EXPIRED",
    "ORDER_STATUS_CANCELLED",
}


@pytest.fixture(scope="module")
def status_enum():
    """The installed proto module that declares the order-status enum."""
    pb = pytest.importorskip(
        "ctrader_open_api.messages.OpenApiModelMessages_pb2",
        reason="the installed protos are what make this inventory meaningful",
    )
    return pb


def _values(status_enum) -> dict[str, int]:
    return {
        name: int(getattr(status_enum, name))
        for name in dir(status_enum)
        if name.startswith("ORDER_STATUS_")
    }


def test_the_installed_protos_expose_exactly_the_five_known_statuses(status_enum) -> None:
    """The inventory, pinned. A proto update that adds a value must fail here."""
    measured = _values(status_enum)
    assert measured == KNOWN_STATUSES, (
        f"the installed protos expose {measured}, and the known inventory is "
        f"{KNOWN_STATUSES}. A new order status is not automatically safe: anything "
        f"outside ACCEPTED is classified as not working and therefore never swept, so a "
        f"still-working new status would leave orders resting. Classify it deliberately - "
        f"that is item 2 - rather than inheriting the default."
        f"order status is not automatically safe: anything outside "
        f"{{ORDER_STATUS_ACCEPTED}} is classified as not working and therefore never swept, so a "
        f"still-working new status would leave orders resting. Classify it deliberately - that is "
        f"item 2 - rather than inheriting the default."
    )


def test_every_status_has_a_name_the_protos_can_report(status_enum) -> None:
    """A nameless value cannot be logged by name, which is how an unknown one stays unknown."""
    wrapper = getattr(status_enum, "OrderStatus", None)
    for name, value in KNOWN_STATUSES.items():
        reported = wrapper.Name(value) if wrapper is not None else None
        assert reported or name, f"{name} ({value}) has no reportable name"
        if reported:
            assert reported == name, f"{name} reports as {reported}"


def test_only_accepted_is_treated_as_working_and_the_rest_are_settled(status_enum) -> None:
    """The semantic the sweep relies on, asserted rather than assumed from the comment."""
    working = {"ORDER_STATUS_ACCEPTED"}
    assert KNOWN_STATUSES.keys() - working == SETTLED_STATUSES, (
        "a status is neither declared working nor declared settled; every known status must be "
        "classified on one side, because the sweep's behaviour follows from that classification"
    )
    for name in SETTLED_STATUSES:
        assert name in KNOWN_STATUSES, f"{name} is claimed settled but is not in the inventory"


def test_the_code_still_treats_only_accepted_as_working() -> None:
    """If this changes, the semantic above has changed with it and both need re-reviewing."""
    from pathlib import Path

    adapter = Path(__file__).resolve().parents[1] / "live_runner" / "ctrader_adapter.py"
    text = adapter.read_text(encoding="utf-8")
    assert (
        "working_statuses = {int(status_enum.ORDER_STATUS_ACCEPTED)}" in text
    ), (
        "the working-status set is no longer exactly {ACCEPTED}. If a status was added to it, the "
        "sweep now cancels it, and the sweep-abort containment in item 2 has to be re-measured"
    )
