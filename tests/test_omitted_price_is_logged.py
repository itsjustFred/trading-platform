"""Item 10: an omitted execution price resolved to 0.0 in silence.

One cause of a sentinel value was loud and the other was not, which is the same shape as item 2's
finding two commits earlier.

`_resolve_execution_price` returns 0.0 as its "could not read" sentinel, and a stored
row then carries `actual_price = 0.0`, from which any PnL is meaningless. There are
exactly two causes, and protobuf's presence makes them distinguishable:

| what the broker sent | before | now |
|---|---|---|
| a price of exactly `0` | logged loudly, deliberately | unchanged |
| **no price at all** (`executionPrice` absent on deal *and* order) | **silent** | logged |

The sent-zero warning explains itself as "the one case a reader cannot otherwise notice, because the
broker spoke and said zero". That is true of it, and it is exactly why the *other* case needed a
warning too: an omitted price leaves **nothing** in the payload, so the row simply carries 0.0 and a
reader cannot tell a missing price from a price of zero without re-deriving it from the wire.

The stored value is unchanged, and whether such a row should be refused, flagged or corrected
remains a trading decision. What changes is that both causes are countable, so that decision can
be made on evidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from live_runner.ctrader_adapter import CTraderLiveAdapter as _Adapter


class _Field:
    """A protobuf field with presence: `HasField` is what makes the two causes separable."""

    def __init__(self, **values: Any) -> None:
        self._values = values

    def HasField(self, name: str) -> bool:  # noqa: N802 - protobuf spelling
        return name in self._values

    def __getattr__(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        raise AttributeError(name)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_an_omitted_price_still_resolves_to_zero_and_now_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The value is unchanged; the silence is not."""
    deal = _Field(fillVolume=1.0)
    order = _Field(fillVolume=1.0)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=order, deal=deal) == 0.0

    warnings = _warnings(caplog)
    assert warnings, (
        "a price that was never sent resolves to 0.0, the row carries actual_price=0, and nothing "
        "said so - a reader cannot tell a missing price from a price of zero"
    )
    assert "no execution price at all" in warnings[-1], warnings[-1]


def test_an_explicit_zero_still_gets_its_own_distinct_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two causes must not collapse into one message.

    They are different facts with different fixes - a broker quoting zero is a broker problem, an
    absent price is a payload problem - so a reader needs to tell them apart.
    """
    deal = _Field(fillVolume=1.0, executionPrice=0.0)
    order = _Field(fillVolume=1.0)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=order, deal=deal) == 0.0

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "exactly 0" in warnings[-1], warnings[-1]
    assert "no execution price at all" not in warnings[-1], (
        "the explicit-zero case reported itself as an omitted price"
    )


def test_a_real_price_is_not_logged_at_all(caplog: pytest.LogCaptureFixture) -> None:
    """The cry-wolf direction: a warning on every ordinary fill is one nobody reads."""
    deal = _Field(fillVolume=1.0, executionPrice=261.7)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=None, deal=deal) == 261.7

    assert not _warnings(caplog), _warnings(caplog)


class TestExecutionPricePresence:
    """`_execution_price_presence` classifies the three states the flag column needs.

    The decision (backlog item 10, resolved 2026-10-11) is to flag an unresolvable price rather than
    refuse the row or substitute the signal's price:

    * refusing drops `filled_qty` from the quantity ledger, so a filled signal looks unfilled and is
      submitted again - a duplicate live order, not a logging nit;
    * substituting was measured to change 4 of 4 rows that have both values, by a consistent +3.6%, in
      the one column used to reconcile against the broker.

    `actual_price` therefore keeps its `NOT NULL` 0.0 sentinel, and a sibling `actual_price_observed`
    records which of these happened. These tests pin the classifier that populates it, so the column can
    be added without re-deciding the policy at the point of the write.

    The states are three rather than two because collapsing them is the failure this exists to prevent:
    a stored `actual_price = 0` is indistinguishable between "the broker sent zero" and "the broker sent
    nothing", and the reader has no way back to the wire.
    """

    @staticmethod
    def _classify(**payloads: Any) -> str:
        from live_runner.ctrader_adapter import CTraderLiveAdapter

        return CTraderLiveAdapter._execution_price_presence(
            order=payloads.get("order"), deal=payloads.get("deal")
        )

    def test_a_real_price_on_the_deal_is_observed(self) -> None:
        assert self._classify(deal=_Field(executionPrice=261.7)) == "observed"

    def test_a_real_price_on_the_order_only_is_still_observed(self) -> None:
        """The order is a fallback, and a fallback price is still an observed one."""
        assert self._classify(order=_Field(executionPrice=1.13978)) == "observed"

    def test_a_deal_price_takes_precedence_over_the_order(self) -> None:
        """A usable deal price wins over a usable order price - and a *sent-zero* deal price wins too.

        The first version of this test asserted `observed` for a deal at 261.7 and an order at 999.0,
        which passes whichever order the loop runs in, so inverting the precedence left it green. It is
        written to discriminate instead: the deal carries a price that is present but unusable and the
        order carries a good one, so the two possible precedences return different answers.

        The disagreement that matters: the resolver returns 0.0 in this case, because it checks the deal
        first and a sent-zero deal is not a usable price. A classifier that preferred the order would
        say `observed`, and the stored row would carry a flag saying a price was read when the stored
        `actual_price` is the 0.0 sentinel. A flag that contradicts its own column is worse than none.
        """
        # Deal sent-zero wins on precedence -> unusable.
        assert self._classify(
            deal=_Field(executionPrice=0.0), order=_Field(executionPrice=261.7)
        ) == "sent_bad"
        # Deal absent -> falls through to the order.
        assert self._classify(
            deal=_Field(), order=_Field(executionPrice=261.7)
        ) == "observed"

    def test_an_explicitly_sent_zero_is_sent_but_unusable(self) -> None:
        """Not "absent". The broker spoke and said zero; `HasField` is true.

        This is the distinction that makes the column worth having. The existing sent-zero warning calls
        this "the one case a reader cannot otherwise notice, because the broker spoke and said zero" -
        and an omitted price leaves nothing in the payload at all. Collapsing the two here would put the
        conflation straight back into the column the flag exists to disambiguate.
        """
        assert self._classify(deal=_Field(executionPrice=0.0)) == "sent_bad"

    def test_a_negative_price_is_sent_but_unusable(self) -> None:
        """The resolver rejects it (`finite and > 0`), so the row is unusable - but the broker sent one."""
        assert self._classify(deal=_Field(executionPrice=-3.5)) == "sent_bad"

    def test_nothing_sent_at_all_is_absent(self) -> None:
        assert self._classify(deal=_Field(), order=_Field()) == "absent"

    def test_no_payloads_is_absent(self) -> None:
        assert self._classify() == "absent"

    def test_the_three_states_are_distinct_for_the_measured_corpus(self) -> None:
        """The property the column depends on, stated over the whole set at once.

        Trivially true pairwise for any single input, and worth asserting anyway because the three-way
        distinction is the entire reason the method exists. A future "simplification" that collapses
        `sent_bad` into `absent` passes every single-input test above and fails only here.
        """
        observed = self._classify(deal=_Field(executionPrice=261.7))
        sent_bad = self._classify(deal=_Field(executionPrice=0.0))
        absent = self._classify(deal=_Field())
        assert len({observed, sent_bad, absent}) == 3, (
            "the three presence states collapsed into fewer; the column this populates would recreate "
            "exactly the conflation it exists to remove"
        )

    def test_it_agrees_with_the_resolver_on_every_case(self) -> None:
        """The flag must describe the stored value, not a parallel opinion about it.

        For all six payload shapes: whenever the resolver returns a real price the classifier says
        `observed`, and whenever it returns its 0.0 sentinel the classifier says one of the two non-
        observed states. If they ever disagree, a row would carry a flag contradicting its own
        `actual_price`, which is worse than having no flag.
        """
        from live_runner.ctrader_adapter import CTraderLiveAdapter

        shapes = [
            {"deal": _Field(executionPrice=261.7)},
            {"deal": _Field(executionPrice=0.0)},
            {"deal": _Field(executionPrice=-3.5)},
            {"deal": _Field()},
            {"order": _Field(executionPrice=1.13978), "deal": _Field()},
            {},
        ]
        for payloads in shapes:
            deal, order = payloads.get("deal"), payloads.get("order")
            price = CTraderLiveAdapter._resolve_execution_price(order=order, deal=deal)
            presence = CTraderLiveAdapter._execution_price_presence(order=order, deal=deal)
            if price > 0:
                assert presence == "observed", (
                    f"resolver returned {price} but presence said {presence!r} for {payloads}"
                )
            else:
                assert presence in {"sent_bad", "absent"}, (
                    f"resolver returned the 0.0 sentinel but presence said {presence!r} for {payloads}"
                )


def test_the_presence_states_map_onto_the_column_values_the_decision_names() -> None:
    """Pin the mapping, because the column's migration will have to use it and cannot re-derive it.

    The decision (backlog item 10) specifies `actual_price_observed` as a nullable bool, and its three
    values have to come from somewhere specific:

        observed -> TRUE     a real price was read
        sent_bad -> FALSE    the broker sent a price and it was unusable
        absent   -> NULL     nothing was sent at all

    **The `absent -> NULL` mapping is the one that is easy to get wrong**, and it is the reason this test
    exists rather than leaving the mapping in a note. `FALSE` is a *claim*: it asserts the broker spoke.
    For a row that never carried a price there is nothing to assert either way, and writing `FALSE` would
    claim a broker spoke when it did not - which is exactly the conflation a nullable column was chosen to
    avoid over a plain boolean.

    The backlog recorded this decision twice with contradictory answers about the writer. An earlier note
    said "let the existing `logger.warning` on the omitted-price branch write `False`"; the decision proper
    did not name a writer. Both cannot hold: a logger cannot write a column, and the resolver has only two
    exits, so a logger-driven flag could only ever say `False` - for both non-observed causes, which is the
    conflation the column exists to remove.

    So the mapping is asserted here, against the real classifier, and named in the note beside it. If the
    column's migration is written from this test rather than from memory, it will be right.
    """
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    # Spelled out as data rather than as a dict comprehension so a reader sees the intent per state.
    column_for = {
        "observed": True,    # a real price was read
        "sent_bad": False,   # sent, and unusable
        "absent": None,      # nothing was sent - unknown, not a negative claim
    }
    assert set(column_for) == {"observed", "sent_bad", "absent"}

    cases = [
        ("observed", {"deal": _Field(executionPrice=261.7)}),
        ("sent_bad", {"deal": _Field(executionPrice=0.0)}),
        ("sent_bad", {"deal": _Field(executionPrice=-3.5)}),
        ("absent", {"deal": _Field()}),
        ("absent", {}),
    ]

    for expected_state, payloads in cases:
        presence = CTraderLiveAdapter._execution_price_presence(
            order=payloads.get("order"), deal=payloads.get("deal")
        )
        assert presence == expected_state, f"{payloads} gave {presence!r}, expected {expected_state!r}"
        value = column_for[presence]
        assert value == column_for[expected_state], (
            f"presence {presence!r} maps to {value!r}, which is not the value the decision names for it"
        )

    # And the two negatives are genuinely different values - the whole point of a nullable column.
    assert column_for["sent_bad"] is False
    assert column_for["absent"] is None
    assert column_for["sent_bad"] != column_for["absent"], (
        "'sent but unusable' and 'never sent' collapsed to the same column value; the column would then "
        "record a broker speaking for rows where nothing was sent, which is the conflation it exists to "
        "prevent"
    )
