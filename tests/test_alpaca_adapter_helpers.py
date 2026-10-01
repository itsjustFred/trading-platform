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