"""Two of `_is_working_order`'s "cannot tell" branches cancelled silently. They now do not.

Item 2's decision - which way an unreadable status resolves - is a trading judgement, and it
is unchanged. What is decidable is the *visibility* of the branch that makes it, because that
branch moves live money: both cases here return `True`, so a sweep sends a cancel.

The third "cannot tell" case - a status this client has no name for - is already logged by
`_unrecognised_status_wire_value`. These two were not:

| branch | returns | logged before |
|---|---|---|
| client exposes no order-status enum | `True` - cancel everything | **nothing** |
| the order carries no status field | `True` - ACCEPTED, cancel | **nothing** |

The second is the one that matters most, because it is indistinguishable on the wire from the third:
protobuf gives a bare `orderStatus` field the declared default, which is
`ORDER_STATUS_ACCEPTED` = 1, so "no status was sent" and "a status this client cannot name"
both read back as ACCEPTED. One was logged. The other was not - and the unlogged one is the
one that fires on a client upgrade or a proto change rather than on bad data.

So: same decision, now countable. Both messages say what will happen, so an operator reading a
log can see a cancel was decided on missing information rather than on a status anybody sent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from live_runner.ctrader_adapter import CTraderLiveAdapter


class _Enum:
    """Enough of the protobuf status enum for the resolver to compare against."""

    ORDER_STATUS_ACCEPTED = 1
    ORDER_STATUS_FILLED = 2
    ORDER_STATUS_REJECTED = 3


def _adapter(monkeypatch: pytest.MonkeyPatch, *, with_enum: bool) -> Any:
    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter.ctid_trader_account_id = "acct-1"
    if with_enum:
        adapter._ProtoOAOrderStatus = _Enum
    else:
        # `getattr(self, ..., None)` must find nothing at all, so no class attribute is set.
        assert not hasattr(CTraderLiveAdapter, "_ProtoOAOrderStatus")
    return adapter


def test_a_missing_status_field_still_cancels_and_now_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The decision is pinned first: a status-less order is still treated as working."""
    adapter = _adapter(monkeypatch, with_enum=True)
    order = SimpleNamespace(orderId=4242)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert adapter._is_working_order(order) is True

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, (
        "an order with no status field is treated as ACCEPTED and will be cancelled, and nothing "
        "said so - the decision is invisible and uncountable"
    )
    assert "no status field" in warnings[-1], warnings[-1]


def test_the_log_carries_the_order_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """"An order will be cancelled" is only actionable if it says which one."""
    adapter = _adapter(monkeypatch, with_enum=True)
    order = SimpleNamespace(orderId=987654)

    with caplog.at_level("WARNING", logger="live-runner"):
        adapter._is_working_order(order)

    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert records, "nothing was logged"
    data = getattr(records[-1], "data", None)
    assert data == {"account_id": "acct-1", "order_id": 987654}, data


def test_a_client_with_no_status_enum_still_cancels_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other branch: no enum means every order looks working, so a sweep cancels all."""
    adapter = _adapter(monkeypatch, with_enum=False)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert adapter._is_working_order(SimpleNamespace(orderId=1)) is True

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a client with no status enum cancels everything silently"
    assert "no order-status enum" in warnings[-1], warnings[-1]
    assert "cancel" in warnings[-1], (
        "the message must say what this leads to - a cancel for every order - not just that the "
        "enum is missing"
    )


def test_a_real_status_is_not_logged_as_a_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**The cry-wolf direction.** A warning on every ordinary order would be one nobody reads."""
    adapter = _adapter(monkeypatch, with_enum=True)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert adapter._is_working_order(SimpleNamespace(orderId=7, orderStatus=1)) is True
        assert adapter._is_working_order(SimpleNamespace(orderId=8, orderStatus=2)) is False

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not warnings, (
        f"an order carrying a real status was logged as a cannot-tell: {warnings}"
    )


def test_the_decision_is_unchanged_for_every_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: only the visibility changed.

    Every branch's return value is asserted here, so a future edit that "fixes" the ambiguity by
    changing which way it resolves fails this test rather than quietly moving live-money behaviour.
    """
    adapter = _adapter(monkeypatch, with_enum=True)
    cases = [
        (SimpleNamespace(orderId=1, orderStatus=1), True, "ACCEPTED is working"),
        (SimpleNamespace(orderId=2, orderStatus=2), False, "FILLED is terminal"),
        (SimpleNamespace(orderId=3, orderStatus=3), False, "REJECTED is terminal"),
        (SimpleNamespace(orderId=4, status=1), True, "older proto: `status` is honoured"),
        (SimpleNamespace(orderId=5, status=2), False, "older proto: `status` FILLED"),
        (SimpleNamespace(orderId=6), True, "absent status resolves to cancel"),
    ]
    for order, expected, why in cases:
        assert adapter._is_working_order(order) is expected, f"{why}: order_id={order.orderId}"

    enumless = _adapter(monkeypatch, with_enum=False)
    assert enumless._is_working_order(SimpleNamespace(orderId=7, orderStatus=2)) is True, (
        "with no enum even a FILLED order is treated as working; that is the pre-existing policy "
        "and this test pins it so it cannot drift unnoticed"
    )
