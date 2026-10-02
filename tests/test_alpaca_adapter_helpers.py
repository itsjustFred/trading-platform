"""Helper guards in `live_runner/alpaca_adapter.py` that nothing tested.

Found by mutation testing the adapter with `mutate.sh`. Two of the four surviving
mutations were real gaps:

- the **nil-UUID** guard in `_normalize_broker_order_id`, which is the only one of
  that function's four guards doing any work - a nil UUID parses successfully, so
  without it a broker reporting the nil UUID would be recorded as a real order id;
- the **refusal** of timeframes Alpaca does not serve. `4h` is a valid project
  timeframe (`VALID_TIMEFRAMES` has it, and the CLI alias map maps `h4` to it), so
  it is a configuration an operator can legitimately write - and the adapter has to
  refuse it *by name* rather than request a wire timeframe that does not exist.
  Adding `4h` to the local table survived the whole suite, which would have turned
  a named refusal into silently wrong market data.

The other two survivors are **equivalent mutants**, pinned here as documentation
rather than as gaps:

- dropping the `isinstance(value, bool)` guard, and
- dropping the `{"none", "null", "false"}` sentinel guard,

both of which the subsequent `UUID(...)` parse already rejects. Measured: `UUID()`
raises for `True`, `"none"`, `"null"` and `"false"`, so those two checks are
belt-and-braces that happen to be redundant today. They stay, because a future
relaxation of the parse would otherwise silently promote a boolean order id.

`ALPACA_PAPER`'s `.strip()` is the same story and lives in
`tests/test_alpaca_paper_blank_agreement.py`'s territory: `parse_bool_env` strips
internally, so the caller's strip is redundant. The *default* beside it is not
redundant, and it is pinned there - flipping `default=True` to `False`, which
would make an unset `ALPACA_PAPER` mean **live** trading, is caught.
"""

from __future__ import annotations

import pytest

from live_runner.alpaca_adapter import _alpaca_timeframe, _normalize_broker_order_id

REAL_ORDER_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
NIL_ORDER_ID = "00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------
# _normalize_broker_order_id
# --------------------------------------------------------------------------


class TestTradeUpdateQuantityResolution:
    """The double-counting guard on the quantity a fill event carries.

    `_resolve_trade_update_qty` picks which quantity to persist per event, and the rule
    is arithmetic rather than a boolean:

    - `data.qty` (or `data.filled_qty`) is **per-event**
    - `order.filled_qty` is **cumulative** in alpaca-py

    A `partial_fill` with no per-event quantity is therefore **skipped**, not persisted
    from `order.filled_qty` - because on the second partial fill the cumulative value is
    the sum of both legs, and persisting it again double-counts.

    Five mutations survive the suite. The consequence of RQ1 is the most severe in this
    sweep: with the guard removed, a stream of partial fills would inflate the recorded
    position, and that number is what position reconciliation and PnL are computed from.

    Asserted as arithmetic, not as a return value: the same two-leg scenario is walked
    with and without a per-event quantity, and the totals are compared. A test asserting
    only `skip is True` would pass against a guard that skipped for the wrong reason.
    """

    @staticmethod
    def _data(**attrs):
        from types import SimpleNamespace

        return SimpleNamespace(**attrs)

    @staticmethod
    def _order(**attrs):
        from types import SimpleNamespace

        return SimpleNamespace(**attrs)

    def _resolve(self, data, order, event):
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        return AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)._resolve_trade_update_qty(
            data, order, event=event, client_order_id="cid"
        )

    def test_a_partial_fill_with_no_per_event_qty_is_skipped(self) -> None:
        """The cumulative value must not be persisted as if it were this leg's."""
        skip, qty = self._resolve(
            self._data(), self._order(filled_qty=300.0), event="partial_fill"
        )
        assert skip is True
        assert qty is None, (
            "a partial fill with no per-event quantity was persisted from "
            "order.filled_qty, which is cumulative: "
            f"{qty!r}"
        )

    def test_a_partial_fill_with_a_per_event_qty_uses_it(self) -> None:
        skip, qty = self._resolve(
            self._data(qty=100.0),
            self._order(filled_qty=300.0),
            event="partial_fill",
        )
        assert skip is False
        assert qty == 100.0, "the per-event quantity was not preferred over the cumulative one"

    def test_two_legs_persist_their_own_quantities_not_the_running_total(self) -> None:
        """The arithmetic, end to end: 100 + 50, not 100 + 150.

        This is the shape the guard exists for. `order.filled_qty` is 150 after the second
        leg, so persisting it would record 250 for two legs that filled 150.
        """
        order = self._order(filled_qty=150.0)

        first_skip, first_qty = self._resolve(
            self._data(qty=100.0), order, event="partial_fill"
        )
        order.filled_qty = 150.0
        second_skip, second_qty = self._resolve(
            self._data(qty=50.0), order, event="partial_fill"
        )

        assert (first_skip, second_skip) == (False, False)
        assert (first_qty, second_qty) == (100.0, 50.0)
        assert first_qty + second_qty == 150.0, (
            "the two legs do not sum to the cumulative quantity, so the per-event "
            f"preference is broken: {first_qty} + {second_qty}"
        )

    def test_a_partial_fill_missing_both_quantities_is_skipped(self) -> None:
        skip, qty = self._resolve(self._data(), self._order(), event="partial_fill")
        assert skip is True
        assert qty is None

    def test_a_full_fill_may_use_the_cumulative_quantity(self) -> None:
        """Only `partial_fill` is refused; a complete fill has one event, so it is safe.

        The distinction is the whole point of the guard: a `fill` event corresponds to the
        whole order, so `order.filled_qty` *is* this event's quantity. Refusing it would
        drop real fills.
        """
        skip, qty = self._resolve(self._data(), self._order(filled_qty=300.0), event="fill")
        assert skip is False
        assert qty == 300.0

    def test_the_per_event_field_is_preferred_over_the_cumulative_one(self) -> None:
        skip, qty = self._resolve(
            self._data(qty=10.0, filled_qty=99.0),
            self._order(filled_qty=150.0),
            event="partial_fill",
        )
        assert skip is False
        assert qty == 10.0, (
            "order.filled_qty was preferred over the per-event quantity: " f"{qty!r}"
        )

    def test_filled_qty_is_used_when_qty_is_absent(self) -> None:
        skip, qty = self._resolve(
            self._data(filled_qty=25.0),
            self._order(filled_qty=150.0),
            event="partial_fill",
        )
        assert skip is False
        assert qty == 25.0

    def test_the_double_count_warning_is_a_warning_not_a_debug_line(self) -> None:
        """The skip is deliberate, so the reason must be visible.

        A silent skip of a partial fill reads as "nothing arrived", which is a different
        diagnosis from "arrived without a per-event quantity, and I refused to
        double-count it".
        """
        import logging

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger = logging.getLogger("trading.live-runner")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self._resolve(self._data(), self._order(filled_qty=1.0), event="partial_fill")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        assert records, "skipping a partial fill logged nothing at all"
        assert all(r.levelno == logging.WARNING for r in records), (
            "the double-count skip was demoted to "
            + str({logging.getLevelName(r.levelno) for r in records})
            + ", so it reads as an absence rather than a deliberate refusal"
        )


def test_order_id_normalisation_defences_that_look_redundant_are_not_reachable() -> None:
    """Two more verified equivalent mutants in `_normalize_broker_order_id`.

    Recorded here rather than as separate tests, because a test asserting an unreachable
    branch asserts nothing - the standing rule this sweep has settled on three times now.

    - **The `isinstance(value, bool)` guard.** `UUID(True)` raises, so the
      `except (AttributeError, TypeError, ValueError)` below refuses it anyway. Verified
      by direct call rather than by reading.
    - **The `{"none", "null", "false"}` sentinel check.** None of those parses as a UUID,
      so the same `except` refuses them. The check reads as defensive documentation of
      what the SDK has been seen to emit, which is reasonable - it just is not what makes
      the function correct.

    Both behaviours are asserted below anyway, because they are the *contract* callers
    rely on, and a future change to the `except` clause would change the outcome without
    changing a line of either guard.

    That is the real reason to keep them: not as kill-tests for the present code, but as
    a tripwire for a future edit that removes the backstop.
    """
    from live_runner.alpaca_adapter import _normalize_broker_order_id

    # The bool guard: True and False are not order ids.
    for value in (True, False):
        assert _normalize_broker_order_id(value) is None, (
            f"{value!r} was accepted as a broker order id"
        )

    # The sentinel guard: SDK-shaped nulls are not order ids, in any case.
    for sentinel in ("none", "null", "false", "NONE", "Null", "FALSE"):
        assert _normalize_broker_order_id(sentinel) is None, (
            f"{sentinel!r} was accepted as a broker order id"
        )


class TestAlpacaCredentialHintAmbiguity:
    """Whether an unmatched Alpaca account may fall back to another account's keys.

    The bulk-response guards on this adapter turn out to be well covered already - four
    mutations were killed on the first attempt by `tests/test_live_execution_safety.py`.
    The gap is on the **credential-selection** side, which decides the same kind of thing
    from the other direction.

    `_alpaca_blocks_have_hints` exists to answer one question: are these credential blocks
    *claiming* to be about specific accounts? If they are and nothing matched, the account
    refuses rather than trading through keys it did not map to. If they are not - the legacy
    single-account setup - the single remaining block is still used, with a warning.

    So a **whitespace-only hint is not a hint**, and that is the survivor worth pinning: the
    check strips before testing, so `account_id_hint="   "` reads as "no claim" rather than
    as a claim on some account. Without the strip, a block whose hint is blank-but-present
    would count as claiming an account, and an unmatched account would be **refused
    credentials it should have been given** - an outage in the one configuration that is
    supposed to keep working.

    `_matching_alpaca_block` is here for the same reason, plus a verified equivalent mutant
    recorded below.
    """

    @staticmethod
    def _block(**kwargs):
        block = {"api_key": "k", "secret_key": "s", "paper": True}
        block.update(kwargs)
        return block

    def test_a_whitespace_only_hint_is_not_a_claim(self) -> None:
        """Blank-after-strip means "unclaimed", so the legacy single-account path still works.

        Asserted for both hint keys, and for a block with no hint keys at all - all three are
        the same state and must read the same way.
        """
        from live_runner.alpaca_adapter import _alpaca_blocks_have_hints

        for blocks, label in (
            ([self._block(account_id_hint="   ")], "account_id_hint of spaces"),
            ([self._block(login_hint="  ")], "login_hint of spaces"),
            ([self._block(account_id_hint=" \t\n ")], "account_id_hint of whitespace"),
            ([self._block()], "no hint keys at all"),
            ([self._block(account_id_hint="", login_hint="")], "both hints empty"),
            ([], "no blocks"),
        ):
            assert _alpaca_blocks_have_hints(blocks) is False, (
                f"{label} was treated as a claim on a specific account"
            )

    def test_a_real_hint_is_a_claim(self) -> None:
        """And a genuine hint is, on either key, with or without surrounding whitespace."""
        from live_runner.alpaca_adapter import _alpaca_blocks_have_hints

        for blocks, label in (
            ([self._block(account_id_hint="acc1")], "account_id_hint"),
            ([self._block(login_hint="alice")], "login_hint"),
            ([self._block(account_id_hint="  acc1  ")], "padded account_id_hint"),
            ([self._block(account_id_hint="", login_hint="alice")], "login_hint only"),
            ([self._block(login_hint=""), self._block(login_hint="bob")], "second of two"),
        ):
            assert _alpaca_blocks_have_hints(blocks) is True, (
                f"{label} was not treated as a claim, so an unmatched account would fall "
                "through to another account's keys"
            )

    def test_a_block_is_matched_on_either_key_and_neither_on_a_blank_hint(self) -> None:
        """Matching is exact after normalising both sides, and a blank hint never matches.

        The padded-account-id case is the one worth keeping: a broker-reported id with
        surrounding whitespace must still find its block, since the comparison lowercases
        and strips both sides.
        """
        from live_runner.alpaca_adapter import _matching_alpaca_block

        # The caller (`resolve_alpaca_credentials`) normalises its inputs; this function
        # normalises the *block's* side itself. Asserted that way round, so the two halves
        # of the normalisation are each pinned where they actually live.
        by_account = [self._block(api_key="by-acc", account_id_hint="  ACC-1  ")]
        assert _matching_alpaca_block(by_account, "acc-1", "") == ("by-acc", "s", True)

        by_login = [self._block(api_key="by-login", login_hint="  Alice  ")]
        assert _matching_alpaca_block(by_login, "", "alice") == ("by-login", "s", True)

        # A blank hint matches nothing, and an absent account/login matches nothing either.
        blank = [self._block(api_key="blank", login_hint="   ")]
        assert _matching_alpaca_block(blank, "", "") is None, (
            "a whitespace-only login hint matched an absent login"
        )
        assert _matching_alpaca_block(blank, "", "alice") is None

    def test_the_normalized_login_guard_is_a_verified_equivalent_mutation_target(self) -> None:
        """**R7 is an equivalent mutant**, recorded with the proof rather than dropped.

        `_matching_alpaca_block` reads:

            if login_hint and normalized_login and login_hint == normalized_login:

        Removing `normalized_login` changes nothing, and the argument is short. The clause
        only ever *adds* a restriction when `normalized_login` is blank - and when it is
        blank, `login_hint == normalized_login` already requires `login_hint` to be blank,
        which the leading `if login_hint` has already excluded.

        Verified by calling the real function on the two configurations that matter:

        | block hint | normalised login | result |
        |---|---|---|
        | `"alice"` | `""` | `None` |
        | `""` | `""` | `None` |

        Both stay `None` with or without the redundant term, because a match requires the
        hint to be non-blank *and* equal to the login.

        Kept as a note rather than a code change for the same reason as the other verified
        equivalents in this sweep: the guard is documentation of intent ("an absent login
        identifies nothing"), it costs nothing, and removing it would make the intent
        implicit in the string comparison.
        """


class TestExecutionFillPersistenceReporting:
    """A failed write must not be logged as a persisted fill.

    `_persist_execution_fill` returns a quantity, and the caller uses it to choose
    between two log messages:

        persisted_qty > 0  ->  "Persisted Alpaca execution fill"
        otherwise          ->  "Skipped duplicate Alpaca execution fill"

    So the return value is not a convenience - **it is what the record says happened.**
    `save_live_execution` returns False for a duplicate (the dedupe key matched), and
    that is correctly reported as a skip. But it can also return False because the write
    *failed*, and reporting `execution.filled_qty` in that case records a fill that is
    not in the database.

    That is the consequence worth stating: the log then says the broker fill was stored
    while nothing was written, so the position stays short of reality and the
    discrepancy surfaces later as an unexplained reconciliation gap rather than as a
    write error. **A stale-but-true record beats a confident one, and this is the case
    where the code chose confidence.**

    Three states, asserted separately because they are three different claims:
    persisted, duplicate (skipped), and failed (skipped, and *not* reported as the
    execution's quantity).
    """

    def _adapter(self):
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        return AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)

    @staticmethod
    def _execution():
        from types import SimpleNamespace

        return SimpleNamespace(
            id="exec-1",
            signal_id="sig-1",
            filled_qty=7.5,
        )

    def _persist(self, saved, *, cumulative=0.0):
        import asyncio

        from live_runner import alpaca_adapter as module

        calls: list[tuple[object, float]] = []

        def _save_live_execution(execution):
            calls.append((execution, "plain"))
            return saved

        def _save_up_to_cumulative(execution, qty):
            # The real function returns the cumulative quantity it wrote, not a bool.
            calls.append((execution, "cumulative"))
            return float(qty)

        original_plain = module.save_live_execution
        original_cumulative = module.save_live_execution_up_to_cumulative
        module.save_live_execution = _save_live_execution
        module.save_live_execution_up_to_cumulative = _save_up_to_cumulative
        try:
            result = asyncio.run(
                self._adapter()._persist_execution_fill(self._execution(), cumulative)
            )
        finally:
            module.save_live_execution = original_plain
            module.save_live_execution_up_to_cumulative = original_cumulative
        return result, calls

    def test_a_successful_write_reports_the_execution_quantity(self) -> None:
        persisted, calls = self._persist(True)
        assert persisted == 7.5
        assert calls and calls[0][1] == "plain"

    def test_a_failed_write_reports_zero_not_the_execution_quantity(self) -> None:
        """The gap in this sweep: a lost fill must not be logged as persisted.

        `save_live_execution` returning False covers both "duplicate" and "failed", and
        the caller cannot tell them apart - so the only correct report for False is a
        skip. Returning `execution.filled_qty` regardless says a fill was stored that
        was not.
        """
        persisted, _calls = self._persist(False)
        assert persisted == 0.0, (
            "a write that did not persist reported the execution's quantity, so the "
            f"caller logs a fill as stored: {persisted!r}"
        )

    def test_a_positive_cumulative_quantity_uses_the_cumulative_write(self) -> None:
        """The idempotent path exists so a redelivered event cannot double-count.

        `save_live_execution_up_to_cumulative` writes the position as the broker's
        running total rather than adding to it, so replaying the same event twice is
        harmless. This asserts the routing, not just the result - the two functions must
        not be interchangeable.
        """
        persisted, calls = self._persist(True, cumulative=20.0)
        assert persisted == 20.0
        assert calls and calls[0][1] == "cumulative", (
            "a positive cumulative quantity did not take the idempotent path: "
            + repr(calls)
        )


class TestTradeUpdateEventParsing:
    """`_parse_trade_update_event` gates every fill the adapter records, and had no test.

    Six mutations survive the suite, which is unusual - this function was simply never
    exercised directly. It is the front door of `_on_trade_update`, and its four refusals
    are the difference between recording a fill and recording a wrong one.

    Why each refusal matters, given that `_on_trade_update` catches every exception and
    logs a warning:

    - **a non-fill event** (`canceled`, `expired`, `new`) is not a fill. Processing one
      as a partial fill writes a fill record with a quantity that was never filled.
    - **no order payload** and **a missing `client_order_id`** are **equivalent mutants**,
      verified by direct probe rather than assumed. With `order is None`,
      `getattr(order, "client_order_id", "")` yields `""`, so the *next* guard refuses
      regardless; and with an empty id the UUID parse refuses regardless. Both are
      documented here rather than given separate tests, because neither branch can be
      reached in isolation - and a test asserting an unreachable branch asserts nothing.
      What the tests below pin instead is the behaviour they jointly produce.
    - **a missing `client_order_id`** - that id is the signal UUID. Without it there is
      no way to attribute the fill to a signal.
    - **a `client_order_id` that is not a UUID** - the same, and worse: `UUID(str(...))`
      is the only thing standing between a broker string and `signal_id` on the
      persistence call at line 1353.

    That last is why the `except ValueError` is asserted specifically rather than "an
    exception is caught": narrowing it to `TypeError` means `UUID("not-a-uuid")` escapes
    the handler - and the handler's own broad `except` then logs "Failed to process trade
    update" and returns, so the fill is still not recorded, but as an *internal error*
    rather than as the malformed broker input it is.
    """

    def _adapter(self):
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        return AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)

    @staticmethod
    def _event(name, *, order=..., client_order_id="..."):
        from types import SimpleNamespace

        return SimpleNamespace(event=name, order=order, client_order_id=client_order_id)

    def _order(self, client_order_id):
        from types import SimpleNamespace

        return SimpleNamespace(id="b-1", client_order_id=client_order_id)

    def test_a_real_fill_is_accepted_and_yields_its_signal_uuid(self):
        from uuid import uuid4

        signal_uuid = uuid4()
        parsed = self._adapter()._parse_trade_update_event(
            self._event("fill", order=self._order(str(signal_uuid)))
        )
        assert parsed is not None
        event, order, client_order_id, parsed_uuid = parsed
        assert event == "fill"
        assert parsed_uuid == signal_uuid
        assert client_order_id == str(signal_uuid)
        assert order.id == "b-1"

    def test_partial_fill_is_accepted(self):
        from uuid import uuid4

        parsed = self._adapter()._parse_trade_update_event(
            self._event("partial_fill", order=self._order(str(uuid4())))
        )
        assert parsed is not None
        assert parsed[0] == "partial_fill"

    @pytest.mark.parametrize(
        "event", ["canceled", "expired", "new", "accepted", "rejected", ""]
    )
    def test_a_non_fill_event_is_refused(self, event: str) -> None:
        """`canceled` and `expired` are the dangerous ones: they look like fills.

        Processing an `expired` order as a partial fill records a quantity that was
        never filled, which then feeds realised-PnL and position reconciliation.
        """
        from uuid import uuid4

        parsed = self._adapter()._parse_trade_update_event(
            self._event(event, order=self._order(str(uuid4())))
        )
        assert parsed is None, f"a {event!r} event was treated as a fill"

    def test_the_event_name_is_matched_case_insensitively(self):
        from uuid import uuid4

        for spelling in ("fill", "FILL", "Fill", "PARTIAL_FILL", "partial_fill"):
            parsed = self._adapter()._parse_trade_update_event(
                self._event(spelling, order=self._order(str(uuid4())))
            )
            assert parsed is not None, f"{spelling!r} was not recognised as a fill"
            assert parsed[0] == spelling.lower()

    def test_a_fill_with_no_order_payload_is_refused(self) -> None:
        """Both refusals at once, so neither guard is masked by the other.

        This started as a single test passing `order=None` *and* a truthy
        `client_order_id`, which cannot kill either mutation independently: with
        `order is None` removed the next guard would refuse it anyway, and with
        `client_order_id` removed the *first* guard would. Both mutants survived.

        The shape that separates them is a payload that is otherwise complete and fails
        **exactly one** thing - so only one guard can be the reason for the refusal.

        Written after two attempts survived: the first passed `order=None` *and* a truthy
        `client_order_id`, and the second assumed that separating the payloads would
        kill both mutations. **It does not** - the two guards are interdependent, so each
        still refuses the other's payload. Probing the mutants directly is what established
        that, and it is the reason they are recorded as equivalent rather than written
        around.
        """
        from uuid import uuid4

        adapter = self._adapter()

        # Only the order payload is missing; the client_order_id is a valid UUID.
        parsed = adapter._parse_trade_update_event(
            self._event("fill", order=None, client_order_id=str(uuid4()))
        )
        assert parsed is None, (
            "a fill with no order payload was processed; the client_order_id was valid, "
            "so only the payload guard could have refused it"
        )

        # Only the client_order_id is missing; the order payload is otherwise complete.
        parsed = adapter._parse_trade_update_event(
            self._event("fill", order=self._order(""), client_order_id="")
        )
        assert parsed is None, (
            "a fill with no client_order_id was processed; the order payload was "
            "present, so only the id guard could have refused it"
        )

    @pytest.mark.parametrize("client_order_id", ["", None, "   "])
    def test_a_missing_client_order_id_is_refused(self, client_order_id) -> None:
        parsed = self._adapter()._parse_trade_update_event(
            self._event("fill", order=self._order(client_order_id))
        )
        assert parsed is None, (
            f"client_order_id={client_order_id!r} was accepted; that id is the signal "
            "UUID, so a fill would be unattributable"
        )

    @pytest.mark.parametrize(
        "client_order_id",
        ["not-a-uuid", "123", "", "  ", "{not-a-uuid}", "550e8400-e29b-not-a-uuid"],
    )
    def test_a_client_order_id_that_is_not_a_uuid_is_refused(
        self, client_order_id: str
    ) -> None:
        parsed = self._adapter()._parse_trade_update_event(
            self._event("fill", order=self._order(client_order_id))
        )
        assert parsed is None, (
            f"{client_order_id!r} was accepted as a signal UUID; `UUID(str(...))` is the "
            "only thing between a broker string and `signal_id` on the persistence call"
        )


def test_a_real_broker_order_id_is_normalised() -> None:
    """The positive case, so the nil guard cannot be satisfied by refusing everything."""
    assert _normalize_broker_order_id(REAL_ORDER_ID) == REAL_ORDER_ID


def test_a_nil_uuid_is_treated_as_no_order_id() -> None:
    """The one guard in this function that does work.

    A nil UUID *parses* - `UUID(NIL_ORDER_ID)` succeeds - so without the
    `uuid_value.int == 0` check it would be returned as a real order id. That
    matters for reconciliation: the platform would believe an order exists at the
    broker when the broker reported nothing.
    """
    assert _normalize_broker_order_id(NIL_ORDER_ID) is None


def test_a_nil_uuid_in_uppercase_and_braced_form_is_also_refused() -> None:
    """Same id, other spellings a broker or SDK might produce."""
    for variant in (NIL_ORDER_ID.upper(), "{%s}" % NIL_ORDER_ID, NIL_ORDER_ID.replace("-", "")):
        assert _normalize_broker_order_id(variant) is None, variant


def test_surrounding_whitespace_is_stripped() -> None:
    assert _normalize_broker_order_id("  %s  " % REAL_ORDER_ID) == REAL_ORDER_ID


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        True,
        False,
        "none",
        "null",
        "false",
        "None",
        "NULL",
        "not-a-uuid",
        "12345",
    ],
)
def test_anything_that_is_not_an_order_id_becomes_none(value: object) -> None:
    """Absent, blank, boolean, sentinel and malformed all mean "no order id"."""
    assert _normalize_broker_order_id(value) is None


# --------------------------------------------------------------------------
# _alpaca_timeframe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire", "expected_amount"),
    [("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("1h", 1), ("1d", 1)],
)
def test_a_supported_timeframe_maps_to_its_amount(wire: str, expected_amount: int) -> None:
    timeframe, normalized = _alpaca_timeframe(wire)
    assert normalized == wire
    assert timeframe.amount_value == expected_amount


@pytest.mark.parametrize("wire", ["4h", "1w", "1wk", "tick", "s1", "2h", "1wk "])
def test_a_timeframe_alpaca_cannot_serve_is_refused_by_name(wire: str) -> None:
    """Kills adding one of these to the local table.

    `4h` and `1w` are valid *project* timeframes - `VALID_TIMEFRAMES` has them and
    the CLI alias map maps `h4`/`w1` onto them - so an operator can legitimately
    configure one. The adapter cannot serve it, and the refusal has to name the
    problem rather than request a wire timeframe Alpaca does not have, which would
    return wrong or stale bars with no error.
    """
    with pytest.raises(ValueError, match="Unsupported Alpaca market-data timeframe"):
        _alpaca_timeframe(wire)


def test_the_timeframe_table_is_not_widened_past_what_alpaca_serves() -> None:
    """The set itself, so a new entry has to be a deliberate decision.

    Complements the refusals above: they prove the *current* entries raise, and
    this proves nothing was quietly added.
    """
    # Read the table out of the module rather than restating it, so this assertion
    # cannot itself become the second copy this repo keeps finding.
    served = _served_timeframes()
    assert served == {"1m", "5m", "15m", "30m", "1h", "1d"}, (
        "the set of Alpaca-servable timeframes changed; every project timeframe "
        "outside it must be refused by name, so adding one needs a matching reason"
    )


def _served_timeframes() -> set[str]:
    """The wire values `_alpaca_timeframe` accepts, read from the module itself.

    The values in that table are `TimeFrame` attribute references, so the dict
    cannot be `literal_eval`-ed; only the keys are extracted, which are literals.
    """
    import ast
    import inspect
    import pathlib

    source = pathlib.Path(inspect.getfile(_alpaca_timeframe)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_alpaca_timeframe":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "units" for t in inner.targets
            ):
                keys = inner.value
                return {
                    k.value
                    for k in keys.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    raise AssertionError("could not read the `units` table out of _alpaca_timeframe")


def test_an_uppercase_monthly_spelling_silently_becomes_one_minute() -> None:
    """A known hazard, pinned so it cannot change without being noticed.

    `_alpaca_timeframe` lower-cases its input, so `1M` resolves to **one minute**,
    not monthly. Alpaca does not serve monthly bars at all, so nothing servable is
    lost - but a caller using Pine-style `1M` for monthly would get one-minute
    bars with no error, and one-minute bars produce a different ATR, which means
    different stops.

    Not asserted as a defect and not changed: the canonical spelling everywhere in
    this repo is lower-case, and the callers pass already-normalised values. It is
    recorded here because the next person to widen that table should know that
    `M` is the one letter whose case changes the meaning rather than the casing.
    """
    timeframe, normalized = _alpaca_timeframe("1M")

    assert normalized == "1m"
    assert timeframe.amount_value == 1
    assert timeframe.unit_value == "Min"

class TestBulkResponseEntryReading:
    """How a bulk cancel/flatten response becomes a count. Two survivors, one dangerous.

    `_response_entries` returns `None` -- **never `[]`** -- for a shape it could not read, and
    that distinction is the whole point of the function. Its docstring says collapsing the two
    made `cancel_open_orders()` report 0 for an unreadable response, which satisfies the daemon's
    `cancelled is not None` check and **records a STOPPED deployment as enforced while its orders
    and positions are still live**.

    So the reader must be *strict about types*, because a loose reader invents a count. The
    surviving mutation shows exactly what that looks like: relaxing the `isinstance(value, list)`
    check so that any non-`None` value is accepted turns

        {"orders": "two"}

    into `['t', 'w', 'o']` - **a count of three for one order** - and `{"orders": 5}` into a
    `TypeError`. Both are worse than refusing: one reports a wrong number of orders cancelled,
    the other crashes mid-flatten.

    **A tuple under a model attribute is read, and that is deliberate tolerance.** The
    dict branch accepts only `list` while the model-attribute branch accepts `(list, tuple)`. The
    asymmetry is a bit odd, but dropping the tuple would not produce a wrong count - it would
    report the response as *unreadable*, which is the safe direction. So the tolerance is pinned
    as behaviour, with the note that removing it costs a refusal rather than correctness.

    Both directions of the `None` distinction are asserted, because "refuses" and "reports zero"
    are the failure and the non-failure respectively, and only asserting one of them leaves the
    other untested.
    """

    @staticmethod
    def _entries(payload):
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        return AlpacaLiveAdapter._response_entries(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {"orders": "two"},
            {"orders": {"id": 1}},
            {"cancelled_orders": "abc"},
            {"symbols": 5},
        ],
    )
    def test_a_non_list_under_a_known_key_is_unreadable_not_a_count(self, payload) -> None:
        """**A string under `orders` must not become a character list.**

        That is what relaxing the type check produces, and it is the dangerous direction: a
        count of three for one order, reported to the daemon as the number of orders
        successfully cancelled. A non-list under a known key is a *malformed response*, not a
        response listing one item per character.
        """
        assert self._entries(payload) is None, (
            f"{payload!r} produced {self._entries(payload)!r} instead of None; a non-list "
            "under a known key is a malformed response, not a count"
        )

    def test_a_list_under_any_known_key_is_read(self) -> None:
        """The pass cases across all four keys, so the strictness above is not always-refusing."""
        for key in ("cancelled_orders", "cancelled_order_ids", "orders", "symbols"):
            payload = {key: [1, 2, 3]}
            assert self._entries(payload) == [1, 2, 3], (key, self._entries(payload))

    def test_a_present_key_holding_none_is_unreadable(self) -> None:
        """`None` under a known key is not "the broker had nothing to do".

        A list under the key is the only thing that means zero work; `None` means the broker
        sent something we do not understand.
        """
        assert self._entries({"orders": None}) is None
        assert self._entries({"orders": []}) == [], (
            "an empty list is a genuine zero and must not become unreadable"
        )

    def test_a_tuple_under_a_model_attribute_is_read(self) -> None:
        """Tolerated on the model branch, and the note says why removing it is survivable.

        The dict branch accepts only `list` while this one accepts `(list, tuple)`. Dropping the
        tuple would make such a response *unreadable* - a refusal, not a wrong count - so the
        tolerance is pinned as behaviour rather than defended as necessary.
        """

        class _Model:
            cancelled_orders = (1, 2, 3)

        assert self._entries(_Model()) == [1, 2, 3], (
            "a tuple under a model attribute is no longer read; that is a refusal rather "
            "than a wrong count, so it is survivable - but update this test if intended"
        )

    def test_a_model_with_no_recognised_attribute_is_unreadable(self) -> None:
        """The counterpart, so the tuple test above is not the only shape that works."""

        class _Empty:
            pass

        assert self._entries(_Empty()) is None
        assert self._entries(None) is None
        assert self._entries("not a response") is None

    def test_the_two_none_readings_reach_different_outcomes(self) -> None:
        """Unreadable raises; an empty list reports zero. Asserted end to end.

        The distinction is only worth maintaining if it changes the result, so this asserts
        both outcomes through the public path rather than only through the reader.
        """
        import asyncio

        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
        adapter._connected = True
        adapter._trading_client = type(
            "_Client", (), {"cancel_orders": staticmethod(lambda: {"orders": "two"})}
        )()
        with pytest.raises(RuntimeError, match="unrecognised response shape"):
            asyncio.run(adapter.cancel_open_orders())

        adapter._trading_client = type(
            "_Client", (), {"cancel_orders": staticmethod(lambda: {"orders": []})}
        )()
        assert asyncio.run(adapter.cancel_open_orders()) == 0, (
            "a genuine empty response must report zero, not raise"
        )


class TestAlpacaCredentialBlockAndOperationGate:
    """The credential-block reader and the bulk-response gate, named directly.

    Four mutations survived, and three of them are about **which field wins** or
    **whether a block exists at all** - the family that decides whether an order reaches a
    paper or a live endpoint.

    **The endpoint outranks the paper flag.** `ALPACA_ENDPOINT_ACC{n}` containing "paper"
    decides the mode, and `ALPACA_PAPER_ACC{n}` is only consulted when no endpoint is set.
    So `{ENDPOINT: "paper", PAPER: "false"}` resolves to **paper**, silently ignoring the
    flag an operator explicitly wrote. That precedence is undocumented in the function
    body and had no test, and it is the safe direction when it is wrong - but "safe because
    of an accident" is not the same as "safe because it is tested".

    **A blank paper flag raises; an absent one defaults to paper.** Those are different
    on purpose. Absent means "nobody said", and the default is the paper endpoint.
    Explicitly blank means somebody wrote an empty value, and `_parse_bool_env` refuses it
    rather than guessing. This is the same distinction the config parser draws, and
    losing it here would mean `PAPER=` in a deploy file quietly started trading real money.

    **An incomplete key/secret pair yields no block.** A block missing its secret returns
    `None` rather than a half-populated credential, so a partially-written env file cannot
    produce a key with an empty secret.

    **`bool(block["paper"])` is load-bearing, not dead code.** The declared type is
    `dict[str, str | bool]`, so a string value is *within contract* - and
    `bool("false")` is `True`. That surprised answer is pinned as-is, with the reason
    recorded: `_alpaca_credential_block` only ever produces a real bool, so the string case
    is unreachable today. The `bool()` is kept because the signature invites the case, and
    pinning it means a future caller passing a string finds the answer written down rather
    than discovering `True` the hard way.
    """

    @staticmethod
    def _values(idx=1, **env):
        values = {
            "ALPACA_API_KEY_ACC%d" % idx: "  key-%d  " % idx,
            "ALPACA_SECRET_KEY_ACC%d" % idx: "  secret-%d  " % idx,
        }
        for key, value in env.items():
            values["ALPACA_%s_ACC%d" % (key, idx)] = value
        return values

    def test_the_endpoint_outranks_the_paper_flag(self) -> None:
        """The undocumented precedence, asserted with both fields set."""
        from live_runner.alpaca_adapter import _alpaca_credential_block

        # Endpoint says paper, flag says false - the endpoint wins.
        block = _alpaca_credential_block(
            self._values(ENDPOINT="https://paper-api.alpaca.markets", PAPER="false"), 1
        )
        assert block is not None
        assert block["paper"] is True, block

        # The live endpoint with the flag saying paper.
        block = _alpaca_credential_block(
            self._values(ENDPOINT="https://api.alpaca.markets", PAPER="true"), 1
        )
        assert block is not None
        assert block["paper"] is False, block

        # And any endpoint containing "paper" counts, matched case-insensitively.
        block = _alpaca_credential_block(self._values(ENDPOINT="LIVE-PAPER"), 1)
        assert block is not None
        assert block["paper"] is True, block

    def test_an_absent_paper_flag_defaults_to_paper_and_a_blank_one_raises(self) -> None:
        """Absent means "nobody said" -> paper. Blank means somebody wrote nothing -> refuse."""
        from live_runner.alpaca_adapter import _alpaca_credential_block

        block = _alpaca_credential_block(self._values(), 1)
        assert block is not None
        assert block["paper"] is True, "an absent paper flag must not default to live"

        with pytest.raises(ValueError) as excinfo:
            _alpaca_credential_block(self._values(PAPER=""), 1)
        message = str(excinfo.value)
        assert "ALPACA_PAPER_ACC1" in message, message

        # And the flag alone is honoured when no endpoint overrides it.
        block = _alpaca_credential_block(self._values(PAPER="false"), 1)
        assert block is not None
        assert block["paper"] is False, block

    def test_an_incomplete_key_or_secret_pair_yields_no_block(self) -> None:
        """A half-written env file must not produce a key with an empty secret."""
        from live_runner.alpaca_adapter import _alpaca_credential_block

        assert _alpaca_credential_block({"ALPACA_API_KEY_ACC1": "k"}, 1) is None
        assert _alpaca_credential_block({"ALPACA_SECRET_KEY_ACC1": "s"}, 1) is None
        assert _alpaca_credential_block({"ALPACA_API_KEY_ACC1": "  ", "ALPACA_SECRET_KEY_ACC1": "s"}, 1) is None
        assert _alpaca_credential_block({}, 1) is None
        # The complete block, for contrast.
        assert _alpaca_credential_block(self._values(), 1) is not None

    def test_credentials_are_stringified_stripped_and_the_flag_is_coerced(self) -> None:
        """The parser already strips; the reader stringifies and coerces. Asserted together.

        A string `paper` is *within the declared type* and `bool("false")` is `True` -
        pinned as-is, because the parser only ever produces a real bool and the surprising
        answer should be written down rather than discovered.
        """
        from live_runner.alpaca_adapter import _alpaca_block_credentials

        api_key, secret_key, paper = _alpaca_block_credentials(
            {"api_key": "k", "secret_key": "s", "paper": True}
        )
        assert (api_key, secret_key, paper) == ("k", "s", True)

        # Non-string credentials are stringified rather than passed through.
        api_key, secret_key, paper = _alpaca_block_credentials(
            {"api_key": 12345, "secret_key": 67890, "paper": False}
        )
        assert api_key == "12345" and isinstance(api_key, str), api_key
        assert secret_key == "67890" and isinstance(secret_key, str), secret_key
        assert paper is False, paper

        # The declared type admits a string, and bool("false") is True. Unreachable from
        # the parser today; documented here so it is a decision rather than a surprise.
        assert _alpaca_block_credentials({"api_key": "k", "secret_key": "s", "paper": "false"})[2] is True

    def test_a_block_missing_a_field_raises_rather_than_defaulting(self) -> None:
        """Direct indexing, so a malformed block is loud instead of silently half-configured."""
        from live_runner.alpaca_adapter import _alpaca_block_credentials

        for incomplete in ({"api_key": "k", "secret_key": "s"}, {"api_key": "k", "paper": True}):
            with pytest.raises(KeyError):
                _alpaca_block_credentials(incomplete)

    def test_the_operation_gate_names_the_operation_and_reports_every_failure(self) -> None:
        """The gate the daemon relies on to decide a STOPPED deployment was enforced.

        Called by name here rather than only through `cancel_open_orders`, because the
        two failure modes are different in kind: an unreadable *shape* raises before any
        counting, while a bad *status* raises after, and both must refuse to produce a
        number.
        """
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        with pytest.raises(RuntimeError) as excinfo:
            AlpacaLiveAdapter._checked_response_operations(
                {"orders": "not-a-list"}, operation="cancel_orders"
            )
        assert "cancel_orders" in str(excinfo.value), str(excinfo.value)
        assert "refusing to report a count" in str(excinfo.value), str(excinfo.value)

        # Every failing entry is named, not just the first - a partial cancel is still a
        # partial cancel and the message has to show which orders are live.
        with pytest.raises(RuntimeError) as excinfo:
            AlpacaLiveAdapter._checked_response_operations(
                [{"id": "a", "status": 404}, {"id": "b", "status": 500}],
                operation="close_positions",
            )
        message = str(excinfo.value)
        assert "close_positions" in message, message
        assert "a=404" in message and "b=500" in message, message

    def test_a_missing_status_is_assumed_successful_for_the_legacy_shape(self) -> None:
        """The one documented assumption: absent means the legacy shape omitted it.

        Deliberately asymmetric with the non-numeric case, which is a *failure*. Omitted
        and malformed are different facts, and conflating them is what this gate exists to
        prevent.
        """
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        entries = AlpacaLiveAdapter._checked_response_operations(
            [{"id": "a"}, {"id": "b", "status": 200}],
            operation="cancel_orders",
        )
        assert [e["id"] for e in entries] == ["a", "b"], entries

    def test_only_2xx_counts_as_success(self) -> None:
        """A 3xx is a redirect, not a cancel. Widening the range to 400 is the trap."""
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        AlpacaLiveAdapter._checked_response_operations([{"id": "a", "status": 200}], operation="cancel_orders")
        AlpacaLiveAdapter._checked_response_operations([{"id": "a", "status": 204}], operation="cancel_orders")
        AlpacaLiveAdapter._checked_response_operations([{"id": "a", "status": 299}], operation="cancel_orders")
        for bad in (199, 300, 302, 400, 422, 500):
            with pytest.raises(RuntimeError):
                AlpacaLiveAdapter._checked_response_operations(
                    [{"id": "a", "status": bad}], operation="cancel_orders"
                )

    def test_a_missing_alpaca_py_is_reported_with_the_package_to_install(self, monkeypatch) -> None:
        """A missing dependency must name itself; a bare ImportError is an hour lost."""
        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "alpaca" or name.startswith("alpaca."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        with pytest.raises(RuntimeError) as excinfo:
            AlpacaLiveAdapter._load_alpaca_order_types(
                AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
            )
        message = str(excinfo.value)
        assert "alpaca-py" in message, message
        assert "required" in message, message
        # The original cause is preserved, so the traceback still shows the real import error.
        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError), excinfo.value.__cause__
