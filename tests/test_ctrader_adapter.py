import asyncio
import logging
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_runner.ctrader_adapter import (
    CTraderApiError,
    CTraderAuthTerminalError,
    CTraderLiveAdapter,
    _is_already_logged_in_error,
    _is_terminal_auth_error,
)


class TestExecutionPriceIsWeakerThanVolume:
    """The price resolver has no guard where the volume resolver, 3 lines below, has one.

    `_resolve_execution_price` tests truthiness only, so it returns whatever is there.
    `_resolve_filled_volume` tests `math.isfinite(value) and value > 0`. Measured, side by side
    on the same deal objects:

    | deal attribute | `_resolve_execution_price` | `_resolve_filled_volume` |
    |---|---|---|
    | price 261.7, volume 5 | 261.7 | 5.0 |
    | price omitted | 0.0 | 5.0 |
    | price -5.0 | 0.0 | 5.0 |
    | price NaN | 0.0 | 5.0 |
    | volume -3 | 261.7 | **0.0** |

    **This asymmetry has been fixed.** Price used to test truthiness alone and returned whatever
    the payload held, so three bad values reached the caller while volume's guard rejected the
    same three on its own field. It now carries the identical `isfinite and > 0` check, and the two
    resolvers agree.

    NaN was the one that mattered: `if value:` is true for NaN, so it did not fall through to the
    order-level fallback and no downstream `> 0` guard would catch it either. A NaN deal price
    beside a perfectly good order price now resolves to the order's price instead of poisoning it.

    `executionPrice` is a protobuf `double` **with presence** whose unset value is `0.0`, so
    "the broker sent no price" and "the broker sent zero" are the same number by the time the
    truthiness test sees them - the same shape as the unreachable enum case in item 2.

    NaN is the one worth naming separately: `if value:` is **true** for NaN, so a NaN price does
    not even fall through to the order-level fallback, and any downstream `> 0` guard rejects it
    while any truthiness guard accepts it. A single NaN row makes `avg(actual_price)` NaN and
    makes `WHERE actual_price > x` silently skip that row, which is harder to notice than a 0.0.

    What remains open is the **zero**, and only the zero. It is not a price: it is the resolver's
    "could not be read" answer, produced when the broker sends no `executionPrice` at all. Whether
    such a row should be refused, flagged, or stored as-is is a trading decision - backlog item 10
    - and answering it needs the broker behaviour, which is not knowable from here.
    """

    @staticmethod
    def _deal(**kwargs):
        return SimpleNamespace(**kwargs)

    @pytest.mark.parametrize(
        ("label", "deal", "expected"),
        [
            ("price present", _deal(executionPrice=261.7, filledVolume=5), 261.7),
            ("price omitted reads 0.0", _deal(filledVolume=5), 0.0),
            ("negative price is refused, not passed through", _deal(executionPrice=-5.0), 0.0),
            ("NaN price is refused, not passed through", _deal(executionPrice=float("nan")), 0.0),
            ("infinite price is refused", _deal(executionPrice=float("inf")), 0.0),
        ],
    )
    def test_an_unusable_price_becomes_the_unreadable_sentinel_not_a_value(
        self, label: str, deal, expected: float
    ) -> None:
        """The impossible values are now refused rather than handed to the money path.

        0.0 is the point of the table: every unusable input collapses onto the same
        "could not be read" answer the resolver already used for an omitted price, so a caller
        cannot mistake a nonsense price for a real one.
        """
        price = CTraderLiveAdapter._resolve_execution_price(order=None, deal=deal)
        assert price == expected, (
            f"_resolve_execution_price({label}) returns {price!r} rather than {expected!r}. If an "
            "impossible price is flowing again, the model constraint should have rejected it and "
            "this resolver is not the last line of defence - re-check both."
        )

    def test_a_nan_deal_price_falls_through_to_the_orders_real_price(self) -> None:
        """NaN is truthy, so before the fix the fallback that would rescue it was skipped.

        The deal said NaN and the order said 261.7. The resolver returned NaN, and no downstream
        `> 0` guard would have caught it: one NaN row makes `avg(actual_price)` NaN and makes
        `WHERE actual_price > x` silently skip that row.

        Now the unusable deal price is refused and the order-level price is used, which is what
        the missing-price path always did.
        """
        assert bool(float("nan")) is True, (
            "NaN is no longer truthy in this interpreter, which is why the old code skipped the "
            "fallback; if this ever becomes false the historical explanation needs revisiting"
        )
        price = CTraderLiveAdapter._resolve_execution_price(
            order=self._deal(executionPrice=261.7), deal=self._deal(executionPrice=float("nan"))
        )
        assert price == pytest.approx(261.7), (
            f"a NaN deal price resolved to {price!r} instead of falling through to the order's "
            "261.7; NaN is contained by the guard again"
        )

    def test_a_nan_price_with_no_usable_order_price_is_the_sentinel(self) -> None:
        """With nothing readable anywhere, the answer is 0.0 - never NaN."""
        price = CTraderLiveAdapter._resolve_execution_price(
            order=self._deal(executionPrice=float("nan")),
            deal=self._deal(executionPrice=float("nan")),
        )
        assert price == 0.0, (
            f"both sources unreadable produced {price!r}; the sentinel must be 0.0 so the model "
            "constraint sees a value it can reason about"
        )

    def test_the_volume_resolver_guards_what_the_price_resolver_does_not(self) -> None:
        """The contrast that makes this an asymmetry rather than a style preference.

        Both resolvers read the same kind of field off the same objects and run 3 lines apart.
        One checks `isfinite(...) and > 0`; the other checks truthiness. Pinned together so the
        difference cannot be closed by accident in one direction only.
        """
        for bad in (-3.0, float("nan"), float("inf")):
            deal = self._deal(executionPrice=261.7, filledVolume=bad)
            volume = CTraderLiveAdapter._resolve_filled_volume(order=None, deal=deal)
            assert volume == 0.0, (
                f"_resolve_filled_volume returned {volume!r} for filledVolume={bad!r}; the "
                "volume guard this contrasts against has changed, so the price asymmetry is no "
                "longly a like-for-like comparison"
            )


class TestLiveExecutionRefusesImpossiblePrices:
    """The model now constrains what no broker can legitimately report for a fill.

    `LiveExecution.actual_price` used to be a bare `float`, accepting `0.0`, a negative price,
    `NaN` and `inf` alike, so the resolver's output went straight to the database. It is now
    `Field(ge=0, allow_inf_nan=False)`.

    The distinction is deliberate and is the whole point:

    - **Rejected** - negative, `NaN`, `+/-inf`. None of these is a price any broker can report
      for a fill, so rejecting them cannot reject a real execution.
    - **Still accepted** - `0.0`. Not because zero is a price, but because it is
      `_resolve_execution_price`'s "could not be read" sentinel: `executionPrice` is a protobuf
      double *with presence* whose unset value is `0.0`, so an omitted price arrives as zero.
      Whether such a row should be refused, flagged or stored is the open half of backlog item 10
      and needs the broker's behaviour, which is not knowable from here.

    So this closes the impossible values and deliberately leaves the ambiguous one. No live row is
    affected either way: all 3 production rows have `actual_price` between 1.13978 and 261.7.
    """

    @staticmethod
    def _base() -> dict:
        import uuid

        return {
            "signal_id": str(uuid.uuid4()),
            "actual_price": 261.7,
            "filled_qty": 1.0,
            "commission": 0.0,
            "timestamp": 1_700_000_000_000,
            "broker_order_id": "ord-1",
        }

    @pytest.mark.parametrize(
        "bad", [-1.5, float("nan"), float("inf"), float("-inf")],
        ids=["negative", "nan", "inf", "-inf"],
    )
    def test_the_model_refuses_an_impossible_price(self, bad: float) -> None:
        from shared.models import LiveExecution

        payload = self._base()
        payload["actual_price"] = bad
        with pytest.raises(Exception) as caught:
            LiveExecution(**payload)
        assert "actual_price" in str(caught.value), (
            f"actual_price={bad!r} was rejected for an unrelated reason: {caught.value}"
        )

    def test_the_model_still_accepts_the_unreadable_sentinel(self) -> None:
        """Zero is the open decision, so it must still pass - for now.

        If this fails, the constraint has been tightened past what the evidence supports, or the
        open half of backlog item 10 has been decided somewhere. Either way the backlog entry and
        this test need to change together.
        """
        from shared.models import LiveExecution

        payload = self._base()
        payload["actual_price"] = 0.0
        assert LiveExecution(**payload).actual_price == 0.0, (
            "a zero price is now refused. That is the decision backlog item 10 has been waiting "
            "for - do not let it arrive by accident."
        )

    def test_the_field_declares_no_constraint_at_all(self) -> None:
        """Assert the absence directly, so the parametrised test cannot pass by accident."""
        from shared.models import LiveExecution

        field = LiveExecution.model_fields["actual_price"]
        assert field.metadata, (
            "actual_price carries no constraints again, so impossible prices are persistable; "
            "update this test and backlog item 10 together"
        )
        assert field.annotation is float, (
            f"actual_price is annotated {field.annotation!r} rather than float, which may be a "
            "deliberate tightening - re-measure rather than assume"
        )


def test_is_already_logged_in_error_detects_code() -> None:
    assert _is_already_logged_in_error("ALREADY_LOGGED_IN", "")


def test_is_already_logged_in_error_detects_message() -> None:
    assert _is_already_logged_in_error("", "Account is already logged in")


def test_is_already_logged_in_error_detects_underscored_message() -> None:
    assert _is_already_logged_in_error(
        "",
        "ALREADY_LOGGED_IN: Open API application is already authorized",
    )


def test_is_terminal_auth_error_detects_not_authorized_message() -> None:
    assert _is_terminal_auth_error("", "Trading account is not authorized")


def test_is_terminal_auth_error_detects_invalid_token_code() -> None:
    assert _is_terminal_auth_error("INVALID_TOKEN", "")


class TestErrorResponseRoutingAndRetryAck:
    """Which log line a broker error produces, and what an ack clears. Five survivors.

    `_handle_error_response_payload` routes three cases and they must not blur together,
    because two of them mean opposite things about the adapter's future:

    | case | meaning | state change |
    |---|---|---|
    | already logged in | a **duplicate auth response** - harmless | none, and notify **once** |
    | terminal auth error | the token is rejected | `_authenticated = False`, adapter parked |
    | anything else | a normal API error | none, logged as a warning |

    Getting the first wrong is the expensive one. If "already logged in" fell through to
    the terminal-auth branch, a routine duplicate response would **park the adapter as if
    the credential had been rejected** - so a harmless event that arrives on every reconnect
    would silently stop trading. That is why E2 is the mutation that matters most here, and
    it survived the suite.

    The notify-once behaviour is asserted by counting records rather than by return value,
    because the whole point is the *volume* of log output: a reconnect loop that logs this
    every time turns one benign event into thousands of lines.

    `_log_connect_failure` is in the same class because it is the same shape of decision -
    auth-versus-timeout-versus-generic - and it exists to stop an operator reading a token
    problem as a connectivity problem. Routing a token error to the generic branch does not
    fail anything; it just tells the operator the wrong thing at 3am.

    **Two mutants are deliberately left unmeasured** (E6, E7): `_ensure_asyncio_twisted_reactor`
    installs a Twisted reactor, which mutates process-global state for the whole test run.
    Exercising it in-process risks destabilising unrelated tests, and a test that can break
    its neighbours is worse than an honest gap. Recorded in the note below rather than
    silently skipped.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        adapter._already_logged_in_notified = False
        adapter._authenticated = True
        adapter._terminal_auth_failure_reason = ""
        adapter._terminal_auth_failure_code = ""
        return adapter

    def test_an_already_logged_in_response_is_ignored_and_parks_nothing(self, caplog) -> None:
        """The expensive case: a duplicate auth response must not park the adapter.

        cTrader sends this on every reconnect. Treating it as a terminal auth failure would
        mean a routine, expected response silently stops trading - so both halves are
        asserted: the adapter stays authenticated, and no terminal reason is recorded.
        """
        import logging

        adapter = self._adapter()
        payload = SimpleNamespace(
            errorCode="ALREADY_LOGGED_IN", description="Account is already logged in"
        )
        with caplog.at_level(logging.INFO):
            caplog.clear()
            adapter._handle_error_response_payload(payload)

        assert adapter._authenticated is True, (
            "a duplicate already-logged-in response parked the adapter as though the "
            "credential had been rejected"
        )
        assert adapter._terminal_auth_failure_reason == ""
        assert any(
            "already authenticated" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_the_duplicate_auth_notice_is_emitted_once(self, caplog) -> None:
        """Notify-once, asserted by counting records.

        cTrader re-sends this on every reconnect, so an unguarded log turns one benign event
        into a stream of lines that pushes real failures out of view. The count is the
        assertion - a return value cannot express "only the first one".
        """
        import logging

        adapter = self._adapter()
        payload = SimpleNamespace(errorCode="ALREADY_LOGGED_IN", description="")
        with caplog.at_level(logging.INFO):
            for _ in range(5):
                caplog.clear()
                adapter._handle_error_response_payload(payload)
                notices = [
                    r for r in caplog.records if "already authenticated" in r.getMessage()
                ]
                if _ == 0:
                    assert len(notices) == 1, "the first response was not announced"
                else:
                    assert notices == [], (
                        f"the duplicate auth response was announced {len(notices)} more "
                        "times; a reconnect loop would flood the log"
                    )
        assert adapter._already_logged_in_notified is True

    def test_a_terminal_auth_response_parks_the_adapter(self, caplog) -> None:
        """The genuine terminal case still works, so the routing is a real split."""
        import logging

        adapter = self._adapter()
        payload = SimpleNamespace(errorCode="NOT_AUTH", description="account is not authorized")
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            adapter._handle_error_response_payload(payload)

        assert adapter._authenticated is False, (
            "a terminal auth response left the adapter authenticated"
        )
        assert adapter._terminal_auth_failure_code == "NOT_AUTH"
        assert "not authorized" in adapter._terminal_auth_failure_reason

    def test_an_ordinary_api_error_parks_nothing(self, caplog) -> None:
        """The third branch: logged as a warning, no state change."""
        import logging

        adapter = self._adapter()
        payload = SimpleNamespace(errorCode="SYMBOL_NOT_FOUND", description="unknown symbol")
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            adapter._handle_error_response_payload(payload)

        assert adapter._authenticated is True, (
            "an ordinary API error parked the adapter"
        )
        assert adapter._terminal_auth_failure_reason == ""
        assert any("API error response" in r.getMessage() for r in caplog.records)

    def test_an_acknowledged_event_releases_its_retry_budget(self) -> None:
        """The ack clears the counter, so a later failure gets a full retry budget.

        `_requeue_execution_event` counts attempts against `EXECUTION_RETRY_LIMIT` and then
        drops the event. If a successful persist did not clear the counter, an event that
        failed once, succeeded, and was later re-delivered would resume with its old count -
        so it would be dropped after fewer retries than the limit allows.
        """
        adapter = self._adapter()
        adapter._execution_retry_counts = {}
        event = SimpleNamespace(marker="e1")

        adapter._execution_retry_counts[id(event)] = 2
        adapter._ack_execution_event(event)
        assert id(event) not in adapter._execution_retry_counts, (
            "an acknowledged event kept its retry count, so a later failure would resume "
            "part-way through the retry budget"
        )

        # And an ack for an event with no recorded attempt is harmless.
        adapter._ack_execution_event(SimpleNamespace(marker="never-queued"))

    def test_auth_and_timeout_connect_failures_are_logged_distinctly(self, caplog) -> None:
        """Token problems and connectivity problems must not read the same at 3am.

        Both are non-terminal - the daemon retries - so nothing breaks either way. The cost
        is purely diagnostic, and the cost of getting it wrong is an operator chasing the
        wrong cause: refreshing a token that was fine, or blaming credentials for a network
        fault. So the routing is asserted on the message.
        """
        import asyncio
        import logging

        adapter = self._adapter()
        with caplog.at_level(logging.INFO):
            caplog.clear()
            adapter._log_connect_failure(RuntimeError("x"), "401", "invalid token")
            auth_messages = [r.getMessage() for r in caplog.records]
        with caplog.at_level(logging.INFO):
            caplog.clear()
            adapter._log_connect_failure(asyncio.TimeoutError(), "timed out", "no response")
            timeout_messages = [r.getMessage() for r in caplog.records]
        with caplog.at_level(logging.INFO):
            caplog.clear()
            adapter._log_connect_failure(RuntimeError("x"), "refused", "connection refused")
            generic_messages = [r.getMessage() for r in caplog.records]

        assert any("authentication unavailable" in m for m in auth_messages), auth_messages
        assert any("connect timeout" in m for m in timeout_messages), timeout_messages
        # The generic message used to be "connect/auth unavailable". **That was the
        # defect**: it is the branch that fires when the cause could not be classified, and
        # it named credentials anyway - so in production it printed once per retry, forever,
        # for a transport that was fine, sending an operator after a token that worked. The
        # contract now lives in
        # `TestConnectFailureClassificationInProduction::test_the_unclassified_fallback_no_
        # longer_names_a_cause`, and this assertion follows it.
        assert any("NOT CLASSIFIED" in m for m in generic_messages), generic_messages
        assert not any("auth" in m.lower() for m in generic_messages), generic_messages
        assert len(set(auth_messages)) == len(set(timeout_messages)), (
            "the token and timeout messages became the same, so an operator cannot tell a "
            "credential problem from a connectivity problem"
        )

    def test_a_timeout_is_detected_from_the_exception_type_or_the_message(self, caplog) -> None:
        """Both signals count, because which one arrives depends on the transport."""
        import asyncio
        import logging

        adapter = self._adapter()
        with caplog.at_level(logging.INFO):
            caplog.clear()
            adapter._log_connect_failure(
                asyncio.TimeoutError(), "read timeout", "read timeout"
            )
            by_type = [r.getMessage() for r in caplog.records]
        with caplog.at_level(logging.INFO):
            caplog.clear()
            # A non-timeout exception whose message still names a timeout - the check is
            # a substring test on "timeout", not a semantic timeout classifier.
            adapter._log_connect_failure(
                RuntimeError("x"), "socket timeout", "connect timeout after 20s"
            )
            by_message = [r.getMessage() for r in caplog.records]

        assert any("connect timeout" in m for m in by_type), (
            "a real TimeoutError was not recognised by its type"
        )
        assert any("connect timeout" in m for m in by_message), (
            "a timeout named only in the message was not recognised"
        )

    # E6/E7 - the Twisted reactor validation is INTENTIONALLY unmeasured, and this used to
    # be a `test_*` method with an empty body. It could not fail, so it inflated the test
    # count and read as coverage it never provided. A decision is documentation, not an
    # assertion, so it lives here as a comment rather than as a test. To close the gap:
    # exercise `asyncioreactor.install(loop)` in a scratch process - it mutates
    # process-global Twisted state for the remainder of the run, so in-process is not safe -
    # install a non-asyncio reactor, and confirm the function raises RuntimeError.
    def _the_twisted_reactor_validation_is_left_unmeasured_on_purpose(self) -> None:
        """**E6 and E7 are deliberately not covered here**, and this records why.

        `_ensure_asyncio_twisted_reactor` calls `asyncioreactor.install(loop)`, which
        mutates process-global Twisted state for the remainder of the test run. Exercising
        it in-process would risk destabilising unrelated tests, and a test that can break its
        neighbours is worse than an admitted gap.

        Both mutants - skipping the reactor validation entirely, and accepting a non-asyncio
        reactor - therefore remain unmeasured by the suite. They are cheap to verify by hand:
        install a non-asyncio reactor in a scratch process and confirm the function raises
        `RuntimeError` rather than proceeding.

        Stated here so the gap is visible in the test file, rather than being an absence
        someone has to rediscover by re-running the mutation.
        """


class TestCancelEligibilityAndIdCoercion:
    """Which resting orders may be cancelled, and which ids may be believed. Seven survivors.

    `_is_working_order` decides whether a cancel is legal. The cost of being wrong is
    asymmetric, and the docstring says so: cancelling a filled or already-cancelled order
    is **rejected by cTrader**, and because that rejection propagates it aborts the sweep
    and leaves *every other* resting order uncancelled. So one bad status value takes out
    the whole orphan sweep, and the orders it abandons are the orphans it existed to clean
    up.

    That asymmetry is why the function fails in a particular direction three separate
    times, and all three are pinned below:

    - **Status enum absent** -> cancel on type alone.
    - **Status attribute absent or unreadable** -> cancel on type alone.
    - **Status present and readable** -> only `ACCEPTED` is working.

    The first two are *not* cautiousness. For an order we are about to cancel, the unsafe
    direction is leaving it resting, so "I cannot tell" resolves to "cancel". Pinned as
    behaviour rather than left implicit, because the natural instinct on reading
    `return True` after two checks that both mean "unknown" is that something is inverted.

    `_coerce_broker_order_id` is the other half: it gates the **duplicate-submission**
    path, where returning a bogus id means a second live order for a signal that already
    has one.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        return adapter

    class _StatusEnum:
        ORDER_STATUS_ACCEPTED = 1

    def test_only_accepted_orders_are_treated_as_working(self) -> None:
        """Every non-ACCEPTED status is terminal, whatever its number.

        Filled, cancelled, rejected and the rest are all terminal. Asserted across a
        spread of values rather than one, because a mutation that added a second status
        to the set would pass a single-value test.
        """
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = self._StatusEnum
        working = adapter._is_working_order

        assert working(SimpleNamespace(orderStatus=1)) is True, (
            "an ACCEPTED order was not treated as working, so it would never be cancelled"
        )
        for terminal in (2, 3, 4, 5, 0, -1, 99):
            assert working(SimpleNamespace(orderStatus=terminal)) is False, (
                f"status {terminal} was treated as a cancellable working order"
            )

    def test_an_absent_status_enum_cancels_on_type_alone(self) -> None:
        """Cannot tell -> cancel. Asserted so `return True` is not read as a mistake.

        Two reasons this direction is right, and both are load-bearing: an unknown enum
        means an older proto build, where the attribute may carry anything; and for an
        order already selected for cancellation, the unsafe outcome is the order staying
        resting, not a rejected cancel. The rejected cancel costs one log line; the
        resting order costs exposure.
        """
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = None
        assert adapter._is_working_order(SimpleNamespace(orderStatus=2)) is True
        assert adapter._is_working_order(SimpleNamespace(orderStatus=1)) is True

    def test_an_unreadable_status_does_not_cancel_KNOWN_INCONSISTENCY(self) -> None:
        """**Pinned as-is, because the code contradicts its own stated policy here.**

        My first version of this test asserted that an unreadable status cancels on type
        alone, reasoning from the docstring. It failed, and the code is right that my
        expectation was wrong about what it does - but the behaviour is the interesting
        part. There are three ways of saying "we cannot tell", and they do not agree:

        | unknown because... | cancels? |
        |---|---|
        | the status enum is absent | **yes** |
        | the status attribute is `None` | **yes** |
        | the status will not convert to `int` | **no** |

        The third row contradicts the docstring directly: *"for an order we are about to
        cancel the unsafe direction is leaving it resting, so the caller cancels on type
        alone."* By that rule an unparseable status should cancel. It does not.

        > **Corrected 2026-10-02 by measurement against the installed client** - see
        > `TestWorkingOrderStatusOnRealProtoMessages`. The three rows above were measured
        > with `SimpleNamespace` doubles, and a real `ProtoOAOrder` cannot take two of
        > them: `orderStatus` has presence, so reading it always yields an `int`. So
        > `return False` is unreachable for broker data, and the option below described as
        > "a genuinely working order whose status arrived as a proto enum object rather
        > than an int stays resting" **cannot happen**. What the reachable case actually
        > is makes the hazard worse, not better: an absent or unrecognised status is read
        > as `ACCEPTED` and therefore cancelled. The test stands; the reasoning around it
        > did not survive measurement.

        So this is a **known inconsistency, not a verified policy**, and it is left
        unmodified because either resolution changes live cancel behaviour:

        - Treat it as unknown and cancel (consistent with the docstring): an already-filled
          order produces a rejected cancel, which propagates and **aborts the orphan
          sweep**, leaving every other resting order uncancelled.
        - Leave it uncancellable (current behaviour): a genuinely working order whose
          status arrived as a proto enum object rather than an int **stays resting**, which
          is the exposure the docstring says to avoid.

        Both outcomes are real, and choosing between them is a trading decision about which
        failure is cheaper. Tracked as `needs-decision` in the backlog rather than silently
        resolved here.

        The test name ends `_KNOWN_INCONSISTENCY` so the next person to touch this is told.
        """
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = self._StatusEnum
        for unreadable in ("not-an-int", "ACCEPTED", object()):
            assert adapter._is_working_order(
                SimpleNamespace(orderStatus=unreadable)
            ) is False, (
                f"an unreadable status {unreadable!r} now cancels, so the inconsistency "
                "documented in this test has been resolved - update it and the backlog "
                "entry together"
            )

    def test_the_three_unknown_status_responses_are_pinned_together(self) -> None:
        """The three cases above, asserted side by side so they cannot drift apart.

        Splitting them across separate tests is how the inconsistency became invisible in
        the first place: each looked locally reasonable. Asserting the triple in one place
        makes the disagreement visible at a glance, which is the only reason to keep it
        while it is unresolved.
        """
        adapter = self._adapter()
        enum = self._StatusEnum

        adapter._ProtoOAOrderStatus = enum
        cancels_when_attribute_missing = adapter._is_working_order(
            SimpleNamespace(orderStatus=None)
        )
        does_not_cancel_when_unreadable = adapter._is_working_order(
            SimpleNamespace(orderStatus="ACCEPTED")
        )
        adapter._ProtoOAOrderStatus = None
        cancels_when_enum_missing = adapter._is_working_order(SimpleNamespace(orderStatus=2))

        assert cancels_when_attribute_missing is True
        assert does_not_cancel_when_unreadable is False
        assert cancels_when_enum_missing is True
        assert cancels_when_attribute_missing != does_not_cancel_when_unreadable, (
            "the two ways of not-knowing now agree; if that was deliberate, drop the "
            "KNOWN_INCONSISTENCY test and the backlog entry"
        )

    def test_an_absent_status_attribute_cancels_on_type_alone(self) -> None:
        """No status at all is unknown, and unknown cancels."""
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = self._StatusEnum
        assert adapter._is_working_order(SimpleNamespace()) is True

    def test_the_newer_order_status_attribute_wins_over_the_older_one(self) -> None:
        """When a proto exposes both, `orderStatus` decides.

        Pinned because the two disagreeing is the whole reason the attribute preference
        exists: a newer build populating `orderStatus` while a legacy `status` field
        still holds a stale value would otherwise cancel on the stale one. Asserted with
        the two attributes set to opposite answers.
        """
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = self._StatusEnum
        assert adapter._is_working_order(
            SimpleNamespace(orderStatus=2, status=1)
        ) is False, "the newer attribute was overridden by the older one"
        assert adapter._is_working_order(
            SimpleNamespace(orderStatus=1, status=2)
        ) is True, "the newer attribute was overridden by the older one"
        # And when the newer attribute is None, the older one is consulted.
        assert adapter._is_working_order(
            SimpleNamespace(orderStatus=None, status=2)
        ) is False

    def test_a_broker_order_id_is_coerced_or_refused(self) -> None:
        """Only a positive integer is an order id we will act on.

        This gates duplicate submission: a returned id means "this signal already has an
        order, return it instead of submitting another". So a wrong-but-accepted value
        does not corrupt data, it **opens a second live position**.
        """
        adapter = self._adapter()
        coerce = adapter._coerce_broker_order_id

        assert coerce("123") == 123, "a numeric string is the normal stored form"
        assert coerce(123) == 123
        for refused in (0, -1, -99, "", None, "abc", True, False):
            assert coerce(refused) is None, (
                f"{refused!r} was accepted as a broker order id, which would short-circuit "
                "duplicate submission"
            )

    def test_a_bool_order_id_is_refused_rather_than_read_as_one_or_zero(self) -> None:
        """`True` is an `int` in Python, so `int(True) == 1` - a real order id.

        This is the same trap as `position_id: True` in the exit-only path, and it is
        worth asserting in both places: without the check, a bool in the database would
        resolve to order 1 and suppress a legitimate submission.
        """
        adapter = self._adapter()
        assert adapter._coerce_broker_order_id(True) is None
        assert adapter._coerce_broker_order_id(False) is None

    def test_a_float_order_id_is_truncated_to_an_int(self) -> None:
        """Documented rather than asserted as a bug: `123.9` becomes `123`.

        cTrader order ids are integers, so this only matters for a corrupt stored value -
        and truncating is the tolerant reading here. Pinned so the behaviour is a
        decision on record rather than an accident, and so a future change to *refuse*
        floats would be deliberate.
        """
        adapter = self._adapter()
        assert adapter._coerce_broker_order_id(123.9) == 123

    def test_a_non_uuid_signal_id_returns_none_rather_than_raising(self) -> None:
        """The parser's contract is "None when it cannot tell", and the caller relies on it.

        `reconcile_executions` iterates ids from the database and does
        `if signal_uuid is None: continue`. So returning `None` is the designed path for a
        row we cannot parse, and the test asserts the garbage-string case that reaches it.
        """
        adapter = self._adapter()
        parse = adapter._parse_signal_uuid
        assert parse("00000000-0000-0000-0000-000000000001") is not None
        for unparseable in ("not-a-uuid", "", "123", "00000000-0000"):
            assert parse(unparseable) is None, (
                f"{unparseable!r} did not return None, so reconciliation would not skip it"
            )

    def test_a_non_uuid_client_order_id_is_refused_not_raised(self) -> None:
        """A fill whose `clientOrderId` is not a UUID cannot be attributed, and must not crash.

        Both the blank and the malformed case are asserted, and the blank case matters on
        its own: cTrader does not always echo `clientOrderId`, so "absent" is an ordinary
        broker response rather than a malformed one, and it is logged as a warning with the
        order id attached before being refused.
        """
        adapter = self._adapter()
        parse = adapter._parse_client_order_id
        valid = "00000000-0000-0000-0000-000000000001"
        assert parse(valid, order_id=1, execution_type=1) is not None
        assert parse("", order_id=1, execution_type=1) is None
        assert parse("not-a-uuid", order_id=1, execution_type=1) is None

    def test_a_blank_client_order_id_warns_distinctly_from_a_malformed_one(self, caplog) -> None:
        """**W8 is an equivalent mutant**, and the reason is that its job is the log.

        Removing `if not client_order_id:` returns `None` either way, because `UUID("")`
        raises `ValueError` and the `except` below already catches it. So no return-value
        assertion can ever kill it.

        What the guard actually does is emit a *different warning*:

        - blank      -> "cTrader fill missing clientOrderId mapping"
        - malformed  -> "cTrader fill has non-UUID clientOrderId"

        That difference is the whole value of the branch. cTrader does not always echo
        `clientOrderId`, so "absent" is an **ordinary broker response** rather than a
        protocol violation, and telling those two apart in the log is how an operator can
        tell a broker behaviour change from a bug. So the assertion is on the logged
        message, which is the only thing the branch actually distinguishes.

        If the two are ever deliberately unified, this test is the thing to delete first.
        """
        import logging

        from live_runner.ctrader_adapter import CTraderLiveAdapter

        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        parse = adapter._parse_client_order_id

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            parse("", order_id=42, execution_type=1)
            blank_messages = [r.getMessage() for r in caplog.records]
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            parse("not-a-uuid", order_id=42, execution_type=1)
            malformed_messages = [r.getMessage() for r in caplog.records]

        assert any("missing clientOrderId mapping" in m for m in blank_messages), (
            f"a blank clientOrderId did not log the 'missing mapping' warning: {blank_messages}"
        )
        assert any("non-UUID clientOrderId" in m for m in malformed_messages), (
            f"a malformed clientOrderId did not log its own warning: {malformed_messages}"
        )
        assert blank_messages != malformed_messages, (
            "the two cases now log the same thing, so an operator can no longer tell a "
            "broker behaviour change from a malformed payload"
        )


class TestWorkingOrderStatusOnRealProtoMessages:
    """What `_is_working_order` does on the messages cTrader actually delivers.

    The "cannot tell" rows pinned in `TestCancelEligibilityAndIdCoercion` were all
    measured with `SimpleNamespace` doubles, which can hold any object at all. A real
    `ProtoOAOrder` cannot: `orderStatus` has presence, so reading it always yields an
    `int` - never `None`, never an enum wrapper. Measured against the installed
    `ctrader_open_api`:

    | what the broker sent | the adapter reads | cancels? |
    |---|---|---|
    | ACCEPTED (1) | 1 | yes |
    | FILLED (2) | 2 | no |
    | REJECTED / EXPIRED / CANCELLED (3/4/5) | 3/4/5 | no |
    | **nothing at all** | **1** | **yes** |
    | **a number this client has no name for** | **1** | **yes** |

    So the `return False` branch is dead code for broker data, and the docstring's
    "cannot tell -> cancel" policy *is* what every reachable path does. That is not the
    reassuring half. The last two rows are the hazard, and the trigger is **silent**: the
    field's declared default is the first enum value, `ORDER_STATUS_ACCEPTED` = 1, and
    protobuf drops an unrecognised enum number on parse without raising or logging. A
    future *terminal* status would therefore be read as working, sent for cancellation,
    rejected by cTrader - and `cancel_open_orders` does not catch that rejection, so the
    sweep aborts and every other resting order is left behind. That is the incident the
    docstring warns about, reachable by a client upgrade rather than by bad data.

    All of it is pinned rather than changed. Which way an unreadable status *should*
    resolve is a trading decision about which failure is cheaper, it is backlog item 2,
    and it is not this class's to make.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        return adapter

    @staticmethod
    def _real():
        """The installed message module, or skip this test alone.

        Scoped to a helper rather than a module-level `importorskip` so that a missing
        protobuf package cannot skip the whole file.
        """
        mod = pytest.importorskip("ctrader_open_api.messages.OpenApiModelMessages_pb2")
        return mod

    @staticmethod
    def _varint(value: int) -> bytes:
        out = bytearray()
        while True:
            chunk = value & 0x7F
            value >>= 7
            out.append(chunk | (0x80 if value else 0))
            if not value:
                return bytes(out)

    def _order_with_wire_status(self, mod, wire_value: int):
        """A real order as the broker's bytes would produce it.

        Hand-rolled varint rather than protobuf's private encoder, so this does not
        break when an internal helper is renamed - the point is the wire, not the API.
        """
        number = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"].number
        order = mod.ProtoOAOrder()
        order.ParseFromString(
            self._varint((number << 3) | 0) + self._varint(wire_value)
        )
        return order

    def test_the_declared_default_is_ACCEPTED_which_is_why_absence_cancels(self) -> None:
        """Pin the coincidence the whole reachable behaviour rests on.

        proto3 reads an unset field as its declared default. If the first enum value
        were `UNKNOWN`, absence would be visible as unknown and this would be a smaller
        problem. It happens to be `ACCEPTED`, so absence is indistinguishable from a
        working order.
        """
        mod = self._real()
        accepted = int(mod.ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)
        field = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"]
        by_number = {v.number: v.name for v in field.enum_type.values}

        assert by_number.get(accepted) == "ORDER_STATUS_ACCEPTED"
        assert 0 not in by_number, (
            "an explicit zero/UNKNOWN enum value now exists; if the adapter reads an "
            "absent status as that instead of ACCEPTED, this pin is stale"
        )
        order = mod.ProtoOAOrder()
        assert order.HasField("orderStatus") is False
        assert order.orderStatus == accepted, (
            "an unset orderStatus no longer reads as ACCEPTED, so this class's "
            "conclusions about absence no longer hold"
        )

    @pytest.mark.parametrize("wire_value", [1, 2, 3, 4, 5])
    def test_a_named_status_is_read_as_itself(self, wire_value: int) -> None:
        """The ordinary path, pinned on real messages rather than doubles."""
        mod = self._real()
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus
        accepted = int(mod.ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)

        order = self._order_with_wire_status(mod, wire_value)
        assert adapter._is_working_order(order) is (wire_value == accepted)

    def test_no_real_status_value_is_ever_none_or_unconvertible(self) -> None:
        """The `except (TypeError, ValueError): return False` branch is dead code.

        Sweeping every value a one-byte varint can carry, because the branch is only
        justified by a path real messages cannot take, and that is worth proving rather
        than asserting once.
        """
        mod = self._real()
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus

        for wire_value in range(0, 256):
            order = self._order_with_wire_status(mod, wire_value)
            raw = getattr(order, "orderStatus", None)
            assert raw is not None, f"status {wire_value} read back as None"
            assert isinstance(raw, int), f"status {wire_value} read back as {type(raw)}"
            int(raw)  # must not raise: this is the branch the doubles exercised

    @pytest.mark.parametrize("wire_value", [6, 9, 255])
    def test_a_status_this_client_cannot_name_becomes_ACCEPTED_but_leaves_a_fingerprint(
        self, wire_value: int
    ) -> None:
        """The forward-compatibility hazard, and it raises nothing.

        Protobuf replaces an enum number it has no name for with the field default, which is
        ACCEPTED. A future terminal status is therefore *read* as a working one, and the order
        gets cancelled.

        The docstring here used to say the value is dropped **silently** and that a future
        terminal status is therefore **indistinguishable** from a working one. Both claims were
        wrong, and measurably so. Protobuf parks the unrecognised value in the message's
        unknown fields, keeping this field's own number and the original value, so the two
        cases *are* distinguishable - by a fingerprint the adapter did not know to look for.
        The hazard is real; "undetectable" was not. See
        `TestAnUnnamedStatusLeavesARecoverableFingerprint`.
        """
        mod = self._real()
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus
        accepted = int(mod.ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)

        order = self._order_with_wire_status(mod, wire_value)
        assert order.orderStatus == accepted, (
            f"an unrecognised status {wire_value} is no longer read as ACCEPTED, so the "
            "cancel-on-unknown-status hazard has changed shape - re-measure it"
        )
        assert adapter._is_working_order(order) is True, (
            "an unrecognised status no longer cancels; if that is now deliberate, this pin is "
            "stale and the policy needs recording rather than a quiet edit"
        )

    def test_an_absent_status_is_treated_as_working_and_would_be_cancelled(self) -> None:
        """Absence is the same hazard by a different route: nothing arrived at all."""
        mod = self._real()
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus

        order = mod.ProtoOAOrder()
        order.orderId = 4242
        assert order.HasField("orderStatus") is False
        assert adapter._is_working_order(order) is True

    def _orders(self, mod, *order_ids):
        adapter = self._adapter()
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus
        adapter._ProtoOAOrderType = mod.ProtoOAOrderType
        orders = []
        for order_id in order_ids:
            order = mod.ProtoOAOrder()
            order.orderId = order_id
            order.orderType = int(mod.ProtoOAOrderType.LIMIT)
            order.orderStatus = int(mod.ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)
            orders.append(order)
        async def _reconcile():
            return SimpleNamespace(order=orders)
        adapter._reconcile_state = _reconcile
        return adapter

    def test_a_rejected_cancel_no_longer_strands_the_other_orders(
        self, caplog
    ) -> None:
        """**The abort is gone.** It was `_is_working_order`'s premise that made it a bug.

        That docstring justifies filtering out filled and cancelled orders precisely because
        "cTrader rejects it, and because that rejection propagates it would abort the loop and
        leave the *other* orders resting". So the code already named the abort as the harm - and
        then relied on a status filter to prevent it. The filter is a best-effort inference from a
        status the client may not be able to name (an unrecognised enum reads as ACCEPTED, item 2),
        so the abort it exists to prevent is reachable anyway.

        Continuing is not a new policy so much as declining to act on a rejection as if it were a
        statement about every *later* order: this function's contract is to cancel every working
        detached order, and one refusal says nothing about the next. What would be a new policy is
        swallowing the refusal, which is why the count of refusals is reported below.

        The test used to assert the opposite, and said so deliberately: "changing it here would
        decide that question by accident". It has now been decided, so the pin is inverted rather
        than deleted.
        """
        mod = self._real()
        adapter = self._orders(mod, 11, 22, 33)
        attempted = []

        async def _cancel(order_id: int) -> None:
            attempted.append(order_id)
            raise CTraderApiError("CH_ORDER_NOT_FOUND", "already filled")

        adapter._cancel_order_id = _cancel
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            assert asyncio.run(adapter.cancel_open_orders()) == 0

        assert attempted == [11, 22, 33], (
            f"the sweep attempted {attempted}; one rejection must not end it, because the orders "
            "left resting are exactly the harm the status filter exists to prevent"
        )

    def test_a_partial_sweep_is_reported_rather_than_swallowed(self, caplog) -> None:
        """An operator has to be able to tell a complete sweep from a partial one."""
        mod = self._real()
        adapter = self._orders(mod, 11, 22)
        attempted = []

        async def _cancel(order_id: int) -> None:
            attempted.append(order_id)
            if order_id == 22:
                raise CTraderApiError("CH_ORDER_NOT_FOUND", "already filled")

        adapter._cancel_order_id = _cancel
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            cancelled = asyncio.run(adapter.cancel_open_orders())

        assert cancelled == 1, "one cancel succeeded, so the count must say one, not two"
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "1 of 2" in joined, (
            f"the incomplete sweep was not reported, so a caller cannot tell it from a clean run: "
            f"{joined!r}"
        )
        assert "22" in joined, "the report must name which orders may still be resting"

    def test_an_auth_terminal_failure_still_ends_the_sweep(self) -> None:
        """The one case where stopping is correct, and the reason the guard is narrow.

        `CTraderAuthTerminalError` is a `RuntimeError`, **not** a `CTraderApiError`, so the
        per-order handler cannot swallow it. That inheritance is load-bearing: if it were ever made
        a subclass of `CTraderApiError`, an auth-terminal failure would be logged and the sweep
        would carry on against a connection the broker has already rejected.
        """
        mod = self._real()
        adapter = self._orders(mod, 11, 22)
        attempted = []

        async def _cancel(order_id: int) -> None:
            attempted.append(order_id)
            raise CTraderAuthTerminalError("credentials revoked")

        adapter._cancel_order_id = _cancel
        with pytest.raises(CTraderAuthTerminalError):
            asyncio.run(adapter.cancel_open_orders())
        assert attempted == [11], f"the sweep continued past an auth-terminal failure: {attempted}"

    def test_the_exception_hierarchy_is_what_makes_the_guard_work(self) -> None:
        """Pinned separately so a refactor cannot quietly widen the `except`."""
        assert not issubclass(CTraderAuthTerminalError, CTraderApiError), (
            "CTraderAuthTerminalError is now a CTraderApiError, so the per-order refusal handler "
            "swallows it and the sweep continues against a dead connection"
        )
        assert issubclass(CTraderAuthTerminalError, RuntimeError)


class TestTerminalAuthFailureAndRetryBudget:
    """How the adapter gives up on cTrader auth, and how long it keeps trying. Eight survivors.

    The terminal-auth path is the one place in this adapter where the correct behaviour is
    **stop**, and stopping wrong is quiet in a specific way: the adapter carries on with a
    connection it should not trust. So the assertions here are about state *not* advancing -
    `_authenticated` going false, the client being torn down, the reason surviving for the
    log - rather than about a return value.

    Two distinct concerns, tested separately because they fail differently:

    - **Giving up** (`_mark_terminal_auth_failure`): if the adapter stays authenticated
      after a terminal failure, every later call proceeds on a connection the broker has
      already rejected, and the failure surfaces as a confusing downstream error instead of
      as the auth problem it is.
    - **Persisting** (`_connect_retry_parameters`): a retry budget of zero would mean no
      retries at all, and a *negative* budget is the same. The floor is what makes
      "always try at least once" true regardless of how the number arrived - including from
      the environment, where a typo becomes a silent policy change rather than an error.

    `_normalize_error_code` is here too, because the whole terminal-auth decision is driven
    off normalised codes, and the normalisation is what makes a differently-spaced code from
    the broker still match.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter._authenticated = True
        adapter._terminal_auth_failure_reason = ""
        adapter._terminal_auth_failure_code = ""
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        adapter._refresh_lock = None
        return adapter

    def test_a_terminal_failure_clears_authentication_and_keeps_the_reason(self) -> None:
        """The state change that stops everything else.

        Both fields are asserted because they are read separately: the reason is what the
        operator sees in the log, and `_authenticated = False` is what stops the next call.
        A mutation that skipped the flag would leave the adapter authenticated on a
        connection the broker has rejected.
        """
        adapter = self._adapter()
        adapter._mark_terminal_auth_failure("account not authorized", error_code="  not authorized  ")
        assert adapter._authenticated is False, (
            "the adapter stayed authenticated after a terminal auth failure, so later "
            "calls proceed on a connection the broker rejected"
        )
        assert adapter._terminal_auth_failure_reason == "account not authorized"
        assert adapter._terminal_auth_failure_code == "NOT_AUTHORIZED", (
            "the code was stored unnormalised, so a differently-spaced broker code would "
            "not match later"
        )

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_reason_becomes_a_named_fallback(self, blank) -> None:
        """A blank reason is replaced, because the log is the only diagnostic left.

        Once the adapter is parked there is no further attempt, so an empty reason leaves
        the operator with a failure and no cause. The fallback text is asserted by name.
        """
        from live_runner.ctrader_adapter import _CTRADER_AUTH_FAILURE_FALLBACK

        adapter = self._adapter()
        adapter._mark_terminal_auth_failure(blank)
        assert adapter._terminal_auth_failure_reason == _CTRADER_AUTH_FAILURE_FALLBACK, (
            "a blank reason was stored verbatim, leaving a terminal failure with no cause"
        )

    def test_the_terminal_handler_tears_the_client_down(self) -> None:
        """The handler must not just record the failure - it must drop the connection.

        Recorded separately from the flag because teardown is the part that releases the
        socket, and a client left open keeps the account session alive on the broker side
        even though this adapter has stopped using it.
        """
        from live_runner.ctrader_adapter import CTraderAuthTerminalError

        adapter = self._adapter()
        torn: list[bool] = []
        adapter._teardown_client = lambda: torn.append(True)
        adapter._log_terminal_auth_failure = lambda reason, *, error_code: None

        adapter._handle_terminal_auth_failure(
            CTraderAuthTerminalError("account not authorized", error_code="NOT_AUTH")
        )
        assert torn == [True], (
            "the terminal auth handler left the client connected, so the broker session "
            "stays open for an adapter that has given up"
        )
        assert adapter._authenticated is False
        assert adapter._terminal_auth_failure_code == "NOT_AUTH"

    def test_error_codes_are_normalised_so_spacing_cannot_defeat_the_match(self) -> None:
        """Normalisation is what makes a code from the broker still match our constants.

        The broker sends codes with inconsistent spacing and casing, so
        `"  not authorized  "` and `"not authorized"` have to reach the same string -
        otherwise the terminal-auth decision silently stops firing.
        """
        from live_runner.ctrader_adapter import _normalize_error_code

        assert _normalize_error_code("  not authorized  ") == "NOT_AUTHORIZED"
        assert _normalize_error_code("already logged in") == "ALREADY_LOGGED_IN"
        assert _normalize_error_code("invalid token") == "INVALID_TOKEN"
        assert _normalize_error_code(12345) == "12345", "a numeric code must not be dropped"
        for blank in ("", "   ", None):
            assert _normalize_error_code(blank) == ""

    def test_the_normalised_code_is_what_drives_the_terminal_decision(self) -> None:
        """The end-to-end claim: an oddly spaced code still parks the adapter.

        Asserted through `_is_terminal_auth_error` rather than only through the string
        helper, because the helper being correct does not mean the decision uses it.
        """
        from live_runner.ctrader_adapter import (
            _is_already_logged_in_error,
            _is_terminal_auth_error,
        )

        assert _is_terminal_auth_error("  INVALID TOKEN  ", "")
        assert _is_terminal_auth_error("NOT_AUTHORIZED", "")
        assert _is_terminal_auth_error("", "account is Not Authorized for this token")
        assert _is_already_logged_in_error("  already logged in ", "")
        assert not _is_terminal_auth_error("SOMETHING_ELSE", "")

    def test_get_error_message_is_consulted_when_present_and_skipped_when_not(self) -> None:
        """The optional accessor: used when there is one, never assumed.

        A mutation that called it unconditionally would raise `AttributeError` on every
        plain string reason - and this function runs on the error path, where raising
        replaces the real diagnostic with a confusing one. So both the present and absent
        cases are asserted, and the case where the accessor itself raises.
        """

        class WithMessage:
            def getErrorMessage(self):
                return "detailed reason"

        class Raising:
            def getErrorMessage(self):
                raise RuntimeError("accessor failed")

        from live_runner.ctrader_adapter import _reason_to_text

        assert _reason_to_text(WithMessage()) == "detailed reason"
        assert _reason_to_text("plain reason") == "plain reason"
        # One instance, compared against itself: `str()` of a fresh instance embeds a
        # different object address, so building two and comparing them compares ids.
        raising = Raising()
        assert _reason_to_text(raising) == str(raising), (
            "a raising accessor did not fall back to the plain representation"
        )
        assert _reason_to_text(None) == "None"

    def test_the_get_error_message_guard_is_a_verified_equivalent_mutation_target(self) -> None:
        """**A8 is an equivalent mutant**, and this records why rather than leaving it open.

        Removing `if hasattr(reason, "getErrorMessage")` and calling the accessor
        unconditionally changes nothing at all, because the `except Exception` immediately
        below catches the resulting `AttributeError` - and catches a raising accessor too.
        Both variants were run by hand against the real function and produced identical
        output for an accessor that raises and for a plain string.

        So the honest conclusion is: **the `except` is load-bearing and the `hasattr` is
        belt-and-braces.** That is not a reason to delete either - this runs on the error
        path, where raising replaces the real diagnostic with an `AttributeError` from a
        string formatting helper, which is a genuinely confusing thing to read in a log
        about an auth failure.

        What makes it safe to leave unfixed is that the *behaviour* is pinned by the test
        above, and the stronger mutation is pinned too: removing the guard **and** the
        `except` together is killed, because a plain string then raises
        `AttributeError: 'str' object has no attribute 'getErrorMessage'` out of the error
        path. So the property that matters - "this function never raises" - is verified,
        not assumed.
        """
        # The load-bearing half - "this function never raises" - is asserted here rather
        # than taken on the docstring's word, because the mutation that matters removes the
        # `hasattr` guard AND the `except` together, and that only fails if something checks.
        from live_runner.ctrader_adapter import _reason_to_text

        class _Raising:
            def getErrorMessage(self):
                raise RuntimeError("accessor exploded")

        class _Working:
            def getErrorMessage(self):
                return "clean message"

        assert _reason_to_text("plain failure") == "plain failure"
        assert _reason_to_text(_Working()) == "clean message"
        # A raising accessor is swallowed and falls back to the repr; it never propagates.
        # The SAME instance is compared, because the default repr embeds the object id.
        raising = _Raising()
        assert _reason_to_text(raising) == str(raising)
        assert _reason_to_text(None) == "None"

    def _retry(self, **attrs):
        import os

        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.connect_max_attempts = attrs.get("max_attempts")
        adapter.connect_timeout_seconds = attrs.get("timeout")
        return CTraderLiveAdapter._connect_retry_parameters(adapter), os

    def test_the_retry_budget_is_never_below_one(self) -> None:
        """Zero and negative budgets both mean "never retry", and the floor prevents it.

        A budget of `0` would skip the first attempt entirely; a negative one comes from a
        typo in the environment and would do the same. Asserted from both directions, since
        the floor is `< 1` rather than `<= 0` and both inputs have to be covered.
        """
        (attempts, _delay, _timeout), _ = self._retry(max_attempts=0)
        assert attempts == 1, f"a zero retry budget stayed at {attempts}"
        (attempts, _, _), _ = self._retry(max_attempts=-4)
        assert attempts == 1, f"a negative retry budget stayed at {attempts}"
        (attempts, _, _), _ = self._retry(max_attempts=3)
        assert attempts == 3, "a valid budget was altered"

    def test_the_retry_budget_floor_applies_to_the_environment_too(self, monkeypatch) -> None:
        """A typo in the environment must not silently become a policy change.

        This is why the floor lives in this function rather than at the call site:
        `CTRADER_CONNECT_MAX_ATTEMPTS=0` in an operator's shell would otherwise disable
        connecting altogether, with no error anywhere. Both `0` and a negative are covered,
        since the floor is `< 1` rather than `<= 0`.
        """
        monkeypatch.delenv("CTRADER_CONNECT_BACKOFF_S", raising=False)
        for bad in ("0", "-4"):
            monkeypatch.setenv("CTRADER_CONNECT_MAX_ATTEMPTS", bad)
            adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
            adapter.connect_max_attempts = None
            adapter.connect_timeout_seconds = None
            attempts, _, _ = CTraderLiveAdapter._connect_retry_parameters(adapter)
            assert attempts == 1, (
                f"CTRADER_CONNECT_MAX_ATTEMPTS={bad} escaped the floor as {attempts} attempts"
            )

    def test_explicit_constructor_arguments_beat_the_environment(self) -> None:
        """Precedence, both for the budget and for the timeout.

        A caller that passes `connect_timeout_seconds=7.0` means 7 seconds. If the
        environment won, the argument would be silently ignored - and it is the kind of
        argument a test or a special account passes to avoid a 20s hang.
        """
        import os

        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.connect_max_attempts = 3
        adapter.connect_timeout_seconds = 7.0
        os.environ["CTRADER_CONNECT_MAX_ATTEMPTS"] = "9"
        os.environ["CTRADER_CONNECT_BACKOFF_S"] = "9.5"
        try:
            attempts, delay, timeout = CTraderLiveAdapter._connect_retry_parameters(adapter)
        finally:
            os.environ.pop("CTRADER_CONNECT_MAX_ATTEMPTS", None)
            os.environ.pop("CTRADER_CONNECT_BACKOFF_S", None)

        assert attempts == 3, "the environment overrode an explicit retry budget"
        assert delay == 9.5, "the backoff delay must come from the environment"
        assert timeout == 7.0, "the environment overrode an explicit connect timeout"

    def test_the_defaults_apply_when_neither_argument_nor_environment_is_set(
        self, monkeypatch
    ) -> None:
        """5 attempts, a 2s backoff and a 20s timeout, stated rather than assumed."""
        import os

        monkeypatch.delenv("CTRADER_CONNECT_MAX_ATTEMPTS", raising=False)
        monkeypatch.delenv("CTRADER_CONNECT_BACKOFF_S", raising=False)
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.connect_max_attempts = None
        adapter.connect_timeout_seconds = None
        assert CTraderLiveAdapter._connect_retry_parameters(adapter) == (5, 2.0, 20.0), os


class TestSymbolScaleFromBrokerProps:
    """The per-symbol unit conversion, which decides what a position is denominated in.

    `_symbol_scale_from_broker_props` builds `volume_per_qty = lot_size / 100`. Four
    mutations survive, and every one of them is a **misstated exposure**: the qty unit is
    0.01 lots, so a wrong scale here is a wrong position size by a factor of 100 or more -
    on XAU and indices, where the module's own docstring warns this can be 10-1000x.

    The rule throughout is the module's: **build from broker values, fall back only where
    the broker value is unusable, and return `None` rather than a guess.** `None` is not a
    failure to be retried - it is how the caller learns to refuse to scale a symbol at all,
    which is the correct outcome for a fill on an unknown unit.

    So these tests pin the *shape* of the refusal as much as its presence. A surviving C4
    mutation would return a plausible FX scale for a symbol whose props never arrived,
    which is precisely the guess the surrounding code refuses to make.

    The digit range is the subtle one: `0` and `10` are **accepted**, because cTrader does
    report those for real symbols. Only values outside `0..10` fall back.
    """

    @staticmethod
    def _fallback():
        from live_runner.ctrader_adapter import SymbolScale

        return SymbolScale(
            volume_per_qty=1000.0, price_digits=5, min_volume=1, lot_size=100000.0
        )

    @staticmethod
    def _build(**kwargs):
        from live_runner.ctrader_adapter import _symbol_scale_from_broker_props

        kwargs.setdefault("fallback", None)
        return _symbol_scale_from_broker_props(**kwargs)

    def test_a_complete_set_of_broker_props_produces_the_scale(self) -> None:
        """The happy path, with the FX contract asserted rather than assumed.

        `100000 / 100 == 1000`: one qty unit is 0.01 lots, so 100000 units of lot size
        make 1000 broker volume units. This is the number every cTrader order size runs
        through, so it is pinned directly.
        """
        scale = self._build(digits=5, lot_size=100000.0, min_volume=1)
        assert scale is not None
        assert scale.volume_per_qty == 1000.0, scale.volume_per_qty
        assert scale.price_digits == 5
        assert scale.min_volume == 1

    def test_the_lot_size_is_divided_by_one_hundred(self) -> None:
        """Not every symbol is FX, so this is asserted with a second lot size."""
        for lot_size, expected in ((100000.0, 1000.0), (1000.0, 10.0), (10.0, 0.1)):
            scale = self._build(digits=3, lot_size=lot_size, min_volume=1)
            assert scale is not None
            assert scale.volume_per_qty == expected, (
                f"lot_size {lot_size} produced {scale.volume_per_qty}, not {expected}"
            )

    @pytest.mark.parametrize("digits", [None, -1, 11, 20, 99])
    def test_an_unusable_digit_count_falls_back_or_refuses(self, digits) -> None:
        """Outside 0..10 there is no digit count worth trusting."""
        # No fallback: refuse rather than invent one.
        assert self._build(digits=digits, lot_size=100000.0, min_volume=1) is None
        # With a fallback: use the fallback's digits, and the rest stays broker-sourced.
        scale = self._build(digits=digits, lot_size=100000.0, min_volume=1, fallback=self._fallback())
        assert scale is not None
        assert scale.price_digits == 5, f"{digits!r} did not fall back to the fallback digits"

    @pytest.mark.parametrize("digits", [0, 10])
    def test_the_boundary_digit_counts_are_accepted(self, digits: int) -> None:
        """`0` and `10` are inside the range and are what cTrader actually reports.

        Asserted because an off-by-one in the range check would silently drop a real
        symbol to the fallback table - and for a symbol whose fallback does not exist,
        to a refusal on every fill.
        """
        scale = self._build(digits=digits, lot_size=100000.0, min_volume=1)
        assert scale is not None, f"digits={digits} was treated as out of range"
        assert scale.price_digits == digits

    @pytest.mark.parametrize(
        "lot_size", [None, 0.0, -1.0, float("nan"), float("inf"), "not-a-number"]
    )
    def test_an_unusable_lot_size_falls_back_or_refuses(self, lot_size) -> None:
        """A zero or negative lot size would scale every order by zero or flip its sign."""
        assert self._build(digits=5, lot_size=lot_size, min_volume=1) is None
        scale = self._build(
            digits=5, lot_size=lot_size, min_volume=1, fallback=self._fallback()
        )
        assert scale is not None
        assert scale.lot_size == 100000.0, "the broker's unusable lot size was kept"

    @pytest.mark.parametrize("min_volume", [None, 0, -1, -99])
    def test_a_non_positive_minimum_volume_falls_back_or_refuses(self, min_volume) -> None:
        """`min_volume` is the fail-closed floor on order size.

        A zero floor would let a sub-minimum order through to the broker; a negative one
        is nonsense. Both are refused without a fallback.
        """
        assert self._build(digits=5, lot_size=100000.0, min_volume=min_volume) is None
        scale = self._build(
            digits=5, lot_size=100000.0, min_volume=min_volume, fallback=self._fallback()
        )
        assert scale is not None
        assert scale.min_volume == 1, "the broker's unusable minimum volume was kept"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"digits": None, "lot_size": 100000.0, "min_volume": 1},
            {"digits": 5, "lot_size": None, "min_volume": 1},
            {"digits": 5, "lot_size": 100000.0, "min_volume": None},
            {"digits": None, "lot_size": None, "min_volume": None},
        ],
    )
    def test_an_incomplete_scale_is_refused_rather_than_filled_in(self, kwargs) -> None:
        """**The important one: `None` means "refuse to scale this symbol", not "retry".

        A surviving mutation here would substitute a plausible FX scale for a symbol whose
        props never arrived - a real-looking `volume_per_qty` for an instrument it knows
        nothing about. That is the guess this whole function exists to avoid, and it is
        invisible afterwards: the order would simply be the wrong size.

        Each field is dropped in turn, since a mutation only has to break one to pass.
        """
        scale = self._build(**kwargs)
        assert scale is None, (
            f"an incomplete scale was returned as {scale!r} - the caller would size an "
            "order from a symbol whose units are unknown"
        )

    def test_a_non_numeric_digit_count_falls_back_rather_than_raising(self) -> None:
        """Unparseable metadata degrades, it does not crash the symbol load.

        A string digits value is a realistic broker payload difference, and the load path
        iterates every symbol - so raising here would take out the whole cache rather than
        one entry.
        """
        assert self._build(digits="five", lot_size=100000.0, min_volume=1) is None
        scale = self._build(
            digits="five", lot_size=100000.0, min_volume=1, fallback=self._fallback()
        )
        assert scale is not None and scale.price_digits == 5

    def test_the_broker_value_wins_over_the_fallback(self) -> None:
        """When the broker reports a usable value, the fallback is not consulted.

        The fallback table is a static guess by symbol class; the broker props are
        authoritative. Overriding them would make every symbol's scale depend on the
        fallback table being right for that instrument, which is the failure mode #80
        was opened for.
        """
        from live_runner.ctrader_adapter import SymbolScale

        stale = SymbolScale(
            volume_per_qty=1.0, price_digits=2, min_volume=999, lot_size=100.0
        )
        scale = self._build(
            digits=5, lot_size=100000.0, min_volume=1, fallback=stale
        )
        assert scale is not None
        assert scale.volume_per_qty == 1000.0
        assert scale.price_digits == 5
        assert scale.min_volume == 1, "the fallback's minimum volume won over the broker's"

    def test_the_fallback_fills_only_the_fields_it_must(self) -> None:
        """A fallback supplies the gaps and nothing else.

        With one bad broker field and two good ones, the two good ones stay broker-derived.
        """
        scale = self._build(
            digits=5, lot_size=100000.0, min_volume=-1, fallback=self._fallback()
        )
        assert scale is not None
        assert scale.price_digits == 5, "the broker's digit count was overwritten"
        assert scale.lot_size == 100000.0, "the broker's lot size was overwritten"
        assert scale.min_volume == 1, "only the bad field should have come from the fallback"


class TestFillIdentityResolution:
    """Which signal a fill belongs to, which was entirely untested. Eight survivors.

    These are the fields that decide *whose* trade a broker event is applied to. Getting
    one wrong does not raise - it silently attributes a fill to the wrong signal, which is
    the hardest class of bug to notice because every individual row looks plausible.

    The two precedence questions, both pinned explicitly because neither is obvious:

    **The order's own `orderId` beats the deal's.** They should agree, so a disagreement
    means one of them is stale, and preferring the order keeps the identity anchored to
    the order we actually submitted rather than to a related deal row.

    **`position_ids` beats a scalar `position_id`, even when both are present.** That is
    the opposite of "more specific wins" intuition and it is load-bearing: the list form
    is the one the upstream exit planner emits, so a stale scalar left in the metadata
    must not override it.

    Every refusal is about refusing to act on an identifier that cannot be trusted -
    a `True` that would become position 1, a `1.5` truncated to 1, a list of two where
    exactly one is required.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter._order_id_to_signal_id = {111: "sig-111", 222: "sig-222"}
        return adapter

    def test_the_orders_own_id_wins_over_the_deals(self) -> None:
        """When both carry an id, the order's is the identity.

        They ought to match; when they do not, one is stale. The order is what we
        submitted, so it is the anchor.
        """
        adapter = self._adapter()
        order_id, client_order_id = adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=111, clientOrderId="co-1"),
            SimpleNamespace(orderId=222),
        )
        assert (order_id, client_order_id) == (111, "co-1")

    def test_the_deals_id_is_used_only_when_the_order_has_none(self) -> None:
        adapter = self._adapter()
        assert adapter._extract_order_and_client_ids(
            SimpleNamespace(clientOrderId="co-1"), SimpleNamespace(orderId=222)
        ) == (222, "co-1")
        # And a zero on the order does not shadow a real id on the deal.
        assert adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=111, clientOrderId="co-1"),
            SimpleNamespace(orderId=0),
        ) == (111, "co-1")

    @pytest.mark.parametrize("bad_id", [0, -1, -5, 0.0, -0.5])
    def test_a_non_positive_order_id_is_not_an_identity(self, bad_id: float) -> None:
        """Zero is the broker's "no id", and it must not become one.

        `0` is falsy in some places and truthy in others, which is exactly why the
        comparison is explicit. Treated as a real id, order 0 would be looked up in the
        signal map and - in a system where id 0 is not assigned - silently resolve to no
        signal while still looking like a successful match.
        """
        adapter = self._adapter()
        order_id, client_order_id = adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=bad_id, clientOrderId=""), None
        )
        assert order_id is None, f"{bad_id!r} was accepted as a broker order id"
        assert client_order_id == ""

    @pytest.mark.parametrize("bad_id", [0, -1, -5, 0.0, -0.5])
    def test_a_non_positive_id_on_the_deal_alone_is_not_an_identity(self, bad_id) -> None:
        """The same rule on the *fallback* path, which is a separate check in the source.

        The order path and the deal path each carry their own `> 0` test. Asserting only
        the order path leaves the deal one unverified, and that is the path taken whenever
        cTrader sends a fill with no order row attached - so it is arguably the more
        likely one to be reached.

        `clientOrderId` is taken from the *order* only, so a deal-only event also has no
        signal id to fall back on, and the result must be an empty string rather than a
        plausible-looking one.
        """
        adapter = self._adapter()
        order_id, client_order_id = adapter._extract_order_and_client_ids(
            None, SimpleNamespace(orderId=bad_id)
        )
        assert order_id is None, f"{bad_id!r} on a deal was accepted as a broker order id"
        assert client_order_id == ""

    def test_a_deal_only_event_with_a_real_id_resolves_through_the_map(self) -> None:
        """Confirms the deal path still works when the id is genuine."""
        adapter = self._adapter()
        assert adapter._extract_order_and_client_ids(None, SimpleNamespace(orderId=222)) == (
            222,
            "sig-222",
        )

    def test_a_blank_client_order_id_is_resolved_through_the_signal_map(self) -> None:
        """The map is the only route from a broker order id back to a signal id.

        cTrader does not always echo `clientOrderId` on a fill, so without this lookup a
        legitimate fill cannot be attributed and is retained for reconciliation instead.
        Asserted in both directions: the hit resolves, and an unmapped id resolves to
        empty rather than to some other signal.
        """
        adapter = self._adapter()
        assert adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=111, clientOrderId=""), None
        ) == (111, "sig-111")
        assert adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=999, clientOrderId=""), None
        ) == (999, "")

    def test_a_present_client_order_id_is_never_overwritten_by_the_map(self) -> None:
        """The broker's own echo wins over our local map.

        Otherwise a stale map entry could relabel a fill that the broker has already
        identified - moving a fill onto a different signal's history.
        """
        adapter = self._adapter()
        _, client_order_id = adapter._extract_order_and_client_ids(
            SimpleNamespace(orderId=111, clientOrderId="broker-echo"), None
        )
        assert client_order_id == "broker-echo"

    def test_an_exit_only_position_id_is_accepted_in_its_accepted_forms(self) -> None:
        adapter = self._adapter()
        assert adapter._requested_exit_position_id({"position_id": 7}) == 7
        assert adapter._requested_exit_position_id({"position_id": "7"}) == 7, (
            "a numeric string is a normal metadata encoding and must not be refused"
        )
        # An integral float is exact, so it is accepted rather than refused for its type.
        assert adapter._requested_exit_position_id({"position_id": 7.0}) == 7
        # Absent means "not an exit-only close", not "position zero".
        assert adapter._requested_exit_position_id({}) is None
        assert adapter._requested_exit_position_id({"position_id": None}) is None

    @pytest.mark.parametrize("bad_id", [0, -1, -5])
    def test_a_non_positive_position_id_is_refused(self, bad_id: int) -> None:
        """An exit-only close needs a real position; 0 closes nothing."""
        adapter = self._adapter()
        with pytest.raises(ValueError, match="must be positive"):
            adapter._requested_exit_position_id({"position_id": bad_id})

    def test_a_fractional_position_id_is_refused_rather_than_truncated(self) -> None:
        """`1.5` truncated to `1` would close the wrong position.

        This is the case that makes the float check worth having: `int()` on a float is
        silent, so a metadata value of `1.5` would close position 1 and leave position 2
        open, with no error anywhere.
        """
        adapter = self._adapter()
        with pytest.raises(ValueError, match="not an integer"):
            adapter._requested_exit_position_id({"position_id": 1.5})
        with pytest.raises(ValueError, match="not an integer"):
            adapter._requested_exit_position_id({"position_id": "1.5"})

    def test_a_bool_position_id_is_refused(self) -> None:
        """`True` is an `int` in Python, and it means position 1.

        Without the explicit check, `{"position_id": True}` closes position 1 - which is
        a real, plausible-looking position id, so the mistake is invisible afterwards.
        """
        adapter = self._adapter()
        with pytest.raises(ValueError, match="not an integer"):
            adapter._requested_exit_position_id({"position_id": True})
        with pytest.raises(ValueError, match="not an integer"):
            adapter._requested_exit_position_id({"position_id": False})

    @pytest.mark.parametrize(
        "raw", [[1, 2], [], [1, 2, 3], 5, "1", {1: 2}]
    )
    def test_a_list_of_position_ids_must_hold_exactly_one_entry(self, raw) -> None:
        """An exit-only close targets one position, so the count is part of the contract.

        Two ids means the caller wanted to close two positions on a path that can only
        close one - closing the first silently would leave the second open and report
        success. An empty list means nothing was chosen.
        """
        adapter = self._adapter()
        with pytest.raises(ValueError, match="exactly one broker position id"):
            adapter._requested_exit_position_id({"position_ids": raw})

    def test_position_ids_wins_over_a_scalar_when_both_are_present(self) -> None:
        """The list form overrides a stale scalar, which is the counter-intuitive one.

        `position_ids` is what the upstream exit planner emits. If an older scalar
        `position_id` is also present - carried from an earlier run, or written by an
        older caller - the list must win, or the close targets the stale position. Pinned
        because the natural reading of "both present" is that the singular is primary.
        """
        adapter = self._adapter()
        assert adapter._requested_exit_position_id(
            {"position_id": 9, "position_ids": [4]}
        ) == 4
        # And the single-element list is the only accepted list form.
        assert adapter._requested_exit_position_id({"position_ids": [4]}) == 4


def test_the_zero_distance_guard_in_resolve_relative_sl_tp_is_unreachable() -> None:
    """**A verified equivalent mutant**, recorded with the proof rather than dropped.

    Removing the `if distance <= 0: raise` inside `_distance` changes nothing, and the
    reason is structural rather than incidental:

        SL:  outward = reference - stop_loss   distance = abs(reference - stop_loss)
        TP:  outward = take_profit - reference distance = abs(reference - take_profit)

    In both cases `distance == abs(outward)` - the same difference, unsigned. The side
    checks already refuse `outward <= 0`, so by the time `_distance` runs, `outward > 0`
    strictly, and therefore `distance > 0` strictly. Zero is unreachable.

    Verified by exhaustion over reference prices of 100.0, 1e-9 and 1e9 crossed with
    stops at the reference, 1e-12 either side, 0.0, -1.0, +inf and NaN: no case with
    `outward > 0` also has `distance == 0`.

    **Why keep the note rather than delete the guard.** The guard is cheap, it documents
    the invariant, and the argument above depends on the side checks staying *upstream of*
    `_distance` for *both* roles. That is a property of the current shape of the code, not
    of the arithmetic: moving `_distance` ahead of the side checks would turn this guard
    into the only thing preventing a zero-distance order. So if the ordering ever changes,
    this test's reasoning has to change with it.

    The `zero-distance` refusal asserted in
    `TestRelativeStopLossAndTakeProfit::test_a_stop_or_target_equal_to_entry_is_refused`
    is therefore real behaviour, coming from the wrong-side check rather than from this
    one. That test is what makes the distinction visible.

    Asserted below rather than described, because the claim is about **ordering**: a stop at
    the reference price must be refused by the side check *before* `_distance` is reached.
    Move `_distance` upstream of the side checks and the message changes from "wrong side"
    to "zero-distance", and this fails - which is the exact refactor the note above warns
    about.
    """
    from uuid import uuid4

    from live_runner.ctrader_adapter import CTRADER_RELATIVE_PRICE_UNIT_DIVISOR as SCALE
    from shared.models import LiveSignal

    def _sig(action: str, price: float) -> LiveSignal:
        return LiveSignal(
            deployment_id=str(uuid4()), symbol="EURUSD", action=action,
            theoretical_price=price, timestamp=1_700_000_000.0,
        )

    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter._symbol_name_to_id = {"EURUSD": 5111}
    adapter._symbol_props = {}

    references = (100.0, 1e-9, 1e9, 0.5, 12345.6789)
    # Offsets that put the stop on the WRONG side of, or exactly at, the reference.
    # Direction matters: a buy's stop must sit below entry, a sell's above it, so -1.0 is
    # a perfectly valid buy stop and would legitimately convert. Only the wrong-side
    # offsets are refused, and the zero-offset one is the zero-distance case itself.
    wrong_side = {
        "buy": (0.0, 1e-12, float("inf"), float("nan")),
        "sell": (0.0, -1e-12, float("-inf"), float("nan")),
    }

    for reference in references:
        for action, offsets in wrong_side.items():
            for offset in offsets:
                with pytest.raises(ValueError) as excinfo:
                    adapter._resolve_relative_sl_tp(_sig(action, reference), reference + offset, None)
                message = str(excinfo.value)
                assert "zero-distance" not in message, (
                    f"the zero-distance guard fired for action={action} "
                    f"reference={reference!r} offset={offset!r}: the side check should have "
                    "refused first, so `_distance` ran before it and the ordering this note "
                    "depends on has changed"
                )
                assert "refusing to submit" in message, message

    # Positive control, and the reason the loop above is not vacuous: a genuinely outward
    # stop converts, on both sides. If the side check were over-eager these would raise.
    for action, stop, take in (("buy", 99.0, 101.0), ("sell", 101.0, 99.0)):
        converted = adapter._resolve_relative_sl_tp(_sig(action, 100.0), stop, take)
        assert converted[0] == round(abs(100.0 - stop) * SCALE), (action, converted)
        assert converted[1] == round(abs(take - 100.0) * SCALE), (action, converted)


class TestRelativeStopLossAndTakeProfit:
    """The SL/TP geometry that becomes a live protective order, which was thin.

    `_resolve_relative_sl_tp` converts absolute prices into cTrader's relative units
    (int64 in 1/100000 of a price unit). Four mutations survive.

    **Every refusal here is a trade that was not placed.** That is the point of the
    function: a stop loss on the wrong side of entry is not a slightly-off stop, it is a
    stop the broker would reject or, worse, accept as a take-profit trigger. So these
    tests are written as "this must not submit" rather than "this returns a number".

    The directions are the part worth stating plainly, because they are the part that is
    easy to get backwards and impossible to notice:

    | action | stop loss | take profit |
    |---|---|---|
    | `buy`  | **below** entry | **above** entry |
    | `sell` | **above** entry | **below** entry |

    Both refusals are asserted for both directions. Asserting only the buy case is how a
    sell stop lands on the wrong side.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        # `_scale_for` is a symbol-cache lookup; this test is about the geometry, so the
        # scale is stubbed and the divisor is asserted separately.
        adapter._scale_for = lambda symbol: SimpleNamespace()
        return adapter

    @staticmethod
    def _signal(action: str, *, reference: float = 100.0):
        return SimpleNamespace(
            action=action, symbol="EURUSD", theoretical_price=reference
        )

    def _resolve(self, action, stop_loss, take_profit=None, *, reference=100.0):
        return self._adapter()._resolve_relative_sl_tp(
            self._signal(action, reference=reference), stop_loss, take_profit
        )

    @pytest.mark.parametrize(
        "action,stop_loss,take_profit",
        [
            ("buy", 90.0, 110.0),
            ("sell", 110.0, 90.0),
        ],
    )
    def test_a_well_placed_order_converts_to_relative_units(
        self, action: str, stop_loss: float, take_profit: float
    ) -> None:
        """A 10-point stop and a 10-point target, at a divisor of 100000.

        Both are outward from entry in the right direction, so both resolve to the same
        magnitude: 10.0 price units -> 1_000_000 relative units. Symmetry is asserted
        deliberately, because buy and sell are the two halves most likely to drift apart.
        """
        from live_runner.ctrader_adapter import (
            CTRADER_RELATIVE_PRICE_UNIT_DIVISOR,
        )

        assert CTRADER_RELATIVE_PRICE_UNIT_DIVISOR == 100000
        stop, take = self._resolve(action, stop_loss, take_profit)
        assert stop == 1_000_000, f"stop distance is wrong for {action}: {stop}"
        assert take == 1_000_000, f"take distance is wrong for {action}: {take}"
        assert stop > 0 and take > 0, "relative units are a signed magnitude, never zero here"

    def test_a_stop_loss_on_the_wrong_side_of_entry_is_refused(self) -> None:
        """A stop the other side of entry is not a stop."""
        # A buy with its stop *above* entry.
        with pytest.raises(ValueError, match="stop_loss"):
            self._resolve("buy", 110.0, 120.0)
        # A sell with its stop *below* entry.
        with pytest.raises(ValueError, match="stop_loss"):
            self._resolve("sell", 90.0, 80.0)

    def test_a_take_profit_on_the_wrong_side_of_entry_is_refused(self) -> None:
        """The half that was untested: a take profit on the wrong side is accepted.

        A buy whose target sits below entry would be submitted as a take profit that the
        market reaches immediately, closing the position for a loss before the stop is
        ever relevant. A sell whose target sits above entry is the mirror image. Both are
        refused with the take profit named in the message.
        """
        with pytest.raises(ValueError, match="take_profit"):
            self._resolve("buy", 90.0, 90.0)
        with pytest.raises(ValueError, match="take_profit"):
            self._resolve("sell", 110.0, 110.0)

    def test_a_stop_or_target_equal_to_entry_is_refused(self) -> None:
        """Zero distance is not a tight stop, it is no stop.

        `stop_loss == reference_price` gives an outward distance of exactly zero. It is
        refused by the wrong-side check rather than by a separate zero-distance check,
        because a distance of zero is also a distance in the wrong direction - and saying
        so in the message is more useful than a second, redundant guard.
        """
        with pytest.raises(ValueError, match="stop_loss"):
            self._resolve("buy", 100.0, 110.0)
        with pytest.raises(ValueError, match="take_profit"):
            self._resolve("buy", 90.0, 100.0)

    def test_an_unusable_reference_price_is_refused_before_anything_else(self) -> None:
        """Without a reference there is no geometry, so nothing is computed.

        Zero, negative and NaN are all refused. The reference is checked first precisely
        because every later calculation is relative to it.
        """
        for reference in (0.0, -1.0, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="Invalid reference price"):
                self._resolve("buy", 90.0, 110.0, reference=reference)

    @pytest.mark.parametrize("bad_stop", [0.0, -5.0, float("nan")])
    def test_an_unusable_stop_price_is_refused_by_the_distance_guard(
        self, bad_stop: float
    ) -> None:
        """These three are caught *only* by the `price <= 0 / NaN` check in `_distance`.

        That matters because the check looks redundant next to the wrong-side check, and
        a mutation removing it survives unless these cases are asserted: a stop of `0.0`
        has an outward distance of 100 (well positive), so the wrong-side check is happy
        and the order would otherwise be submitted with a stop 100 points from entry.
        """
        with pytest.raises(ValueError, match="Invalid SL/TP price"):
            self._resolve("buy", bad_stop, 110.0)

    @pytest.mark.parametrize("bad_take", [float("nan"), float("inf")])
    def test_a_non_finite_take_profit_is_refused_by_the_distance_guard(
        self, bad_take: float
    ) -> None:
        """NaN and infinity slip past the wrong-side check, and only `_distance` catches them.

        The comparison `take_outward <= 0` is False for NaN, so a NaN target passes the
        side check and reaches the distance guard. For `+inf` the outward distance is
        positive, so it passes too - and `abs(ref - inf) * scale` would be infinity,
        which `int(round(inf))` cannot convert.
        """
        with pytest.raises(ValueError, match="Invalid SL/TP price"):
            self._resolve("buy", 90.0, bad_take)

    @pytest.mark.parametrize("bad_take", [0.0, -5.0])
    def test_a_non_positive_take_profit_is_refused_as_the_wrong_side(self, bad_take) -> None:
        """A target at or below entry for a buy is caught earlier, by the side check.

        Documented because it is the *other* reason those values are refused, and
        because it is the case that would otherwise look untested: the value is refused
        either way, but by a different guard with a different message.
        """
        with pytest.raises(ValueError, match="take_profit"):
            self._resolve("buy", 90.0, bad_take)

    def test_an_infinite_stop_is_refused_as_the_wrong_side(self) -> None:
        """`inf` is refused by the side check rather than the distance check.

        For a buy, `reference - inf` is `-inf`, so the outward distance is negative and
        the side check fires first. Asserted so the SL and TP rows above are not read as
        "infinity always reaches the distance guard" - it depends on the direction.
        """
        with pytest.raises(ValueError, match="stop_loss"):
            self._resolve("buy", float("inf"), 110.0)

    def test_a_stop_only_order_sends_zero_for_the_take_profit(self) -> None:
        """The explicit unbounded policy: `take_profit=None` means no take profit.

        cTrader reads a zero `relativeTakeProfit` as "no take profit", so the zero is
        load-bearing rather than a default. Asserted because a small negative or a
        coincidental distance would silently change the order from stop-only to
        something else.
        """
        stop, take = self._resolve("sell", 110.0, None)
        assert take == 0, f"a stop-only order must send a zero take profit, not {take}"
        assert stop == 1_000_000

    def test_a_distance_below_one_price_unit_is_refused(self) -> None:
        """Rounding a sub-unit distance would send `0`, which cTrader reads as unset.

        A stop 1e-7 price units away rounds to zero relative units - and zero means "no
        stop", so the order would go out **unprotected** rather than tightly stopped. The
        guard refuses instead. Also asserted below the unit, so the boundary itself is
        pinned rather than just the refusal.
        """
        from live_runner.ctrader_adapter import (
            CTRADER_RELATIVE_PRICE_UNIT_DIVISOR,
        )

        divisor = float(CTRADER_RELATIVE_PRICE_UNIT_DIVISOR)
        # Half a relative unit: rounds to 0, and must not be submitted as 0.
        below_unit = 100.0 - (0.5 / divisor)
        with pytest.raises(ValueError, match="below one"):
            self._resolve("buy", below_unit, 110.0)
        # One full relative unit is accepted, so the boundary is not off by one.
        assert self._resolve("buy", 100.0 - (1.0 / divisor), 110.0)[0] == 1


class TestFillRecordResolution:
    """How a cTrader fill becomes a persisted `LiveExecution`, which had no unit tests.

    Six mutations survive this family, and two of them are the same defect seen from
    different sides: **`_build_dedupe_key`'s fallback cannot tell two distinct fills
    apart**, so the second one is silently discarded as a duplicate.

    Every one of these values lands in the fill record. That is the reason the
    assertions are about the *record's* claims rather than about return values.

    Note the asymmetry this suite pins deliberately: `_resolve_filled_volume` refuses a
    non-positive volume and the caller refuses an unresolvable symbol, but
    **`_resolve_execution_price` has no such refusal** - it returns `0.0` when nothing
    resolves, and `LiveExecution` accepts `0.0` and even a negative price. The guard
    exists for two of the three fields and not the third. That is documented below rather
    than silently accepted, because it is a human decision whether a fill with no price
    should be refused (it may be legitimate) or persisted as zero (it poisons PnL).
    """

    @staticmethod
    def _adapter():
        return CTraderLiveAdapter.__new__(CTraderLiveAdapter)

    def test_the_deal_price_wins_over_the_order_price(self) -> None:
        """The deal is what actually executed; the order carries the submitted price."""
        adapter = self._adapter()
        deal = SimpleNamespace(executionPrice=50.0)
        order = SimpleNamespace(executionPrice=99.0)
        assert adapter._resolve_execution_price(order=order, deal=deal) == 50.0, (
            "the order price was used for an execution price, so the fill is recorded at "
            "the price we asked for rather than the price we got"
        )

    def test_a_deal_without_a_price_falls_back_to_the_order_price(self) -> None:
        adapter = self._adapter()
        deal = SimpleNamespace(executionPrice=0.0)
        order = SimpleNamespace(executionPrice=99.0)
        assert adapter._resolve_execution_price(order=order, deal=deal) == 99.0

    def test_an_unresolvable_price_is_zero_and_that_is_accepted_downstream(self) -> None:
        """Documents the gap, deliberately, with the consequence checked at both ends.

        There is no positivity guard here, and `LiveExecution` does not add one - so a
        fill whose price cannot be resolved is persisted as `actual_price = 0.0`. Any
        PnL computed from it is meaningless. Asserted at both layers so that a future
        guard shows up as a failure in *this* test, where the decision belongs, rather
        than as a surprise in a PnL assertion somewhere else.
        """
        adapter = self._adapter()
        resolved = adapter._resolve_execution_price(order=None, deal=None)
        assert resolved == 0.0, "the resolver now refuses; update this test and the gap"

        from datetime import datetime, timezone

        from shared.models import LiveExecution

        record = LiveExecution(
            signal_id="00000000-0000-0000-0000-000000000001",
            actual_price=resolved,
            filled_qty=1.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id="1",
            dedupe_key="k",
        )
        assert record.actual_price == 0.0, (
            "LiveExecution now refuses a zero price, so the persistence gap is closed "
            "downstream and this test should be revisited"
        )

    def test_a_negative_price_is_refused_rather_than_returned(self) -> None:
        """Was `test_a_negative_price_is_not_refused`, which characterised the truthiness check.

        A negative price is truthy, so it used to be returned as an execution price and then
        persisted. It now collapses onto the unreadable sentinel, like every other value a broker
        cannot legitimately report for a fill. Kept as a test rather than deleted, because the
        guard regressing back to `if value:` is exactly the mistake this file documents.
        """
        adapter = self._adapter()
        assert adapter._resolve_execution_price(
            order=None, deal=SimpleNamespace(executionPrice=-1.5)
        ) == 0.0

    def test_the_deal_volume_wins_and_the_order_fallback_must_be_positive(self) -> None:
        adapter = self._adapter()
        deal = SimpleNamespace(filledVolume=500.0, volume=400.0)
        assert adapter._resolve_filled_volume(order=None, deal=deal) == 500.0

        # A deal carrying only `volume` still resolves.
        assert adapter._resolve_filled_volume(
            order=None, deal=SimpleNamespace(volume=400.0)
        ) == 400.0

        # And the order fallback is only accepted when positive - without that check a
        # zero `executedVolume` would return and the caller's own guard would be the
        # only thing standing between that and a zero-volume fill.
        assert adapter._resolve_filled_volume(
            order=SimpleNamespace(executedVolume=250.0), deal=None
        ) == 250.0
        assert adapter._resolve_filled_volume(
            order=SimpleNamespace(executedVolume=0.0), deal=None
        ) == 0.0
        assert adapter._resolve_filled_volume(order=None, deal=None) == 0.0

        # The case that actually separates the two guards. Zero returns 0.0 either way
        # - an unguarded zero is indistinguishable from a guarded one - so dropping the
        # positivity check is only observable on a *negative* executedVolume, which an
        # unguarded version would return as the fill's volume.
        assert adapter._resolve_filled_volume(
            order=SimpleNamespace(executedVolume=-5.0), deal=None
        ) == 0.0, (
            "a negative executed volume was accepted as a fill volume, so a bad broker "
            "payload becomes a negative position"
        )

    def test_the_timestamp_prefers_the_deal_then_the_order(self) -> None:
        adapter = self._adapter()
        assert adapter._resolve_execution_timestamp_ms(
            order=SimpleNamespace(utcLastUpdateTimestamp=2000),
            deal=SimpleNamespace(executionTimestamp=1000, utcLastUpdateTimestamp=9999),
        ) == 1000
        assert adapter._resolve_execution_timestamp_ms(
            order=SimpleNamespace(utcLastUpdateTimestamp=2000), deal=None
        ) == 2000
        assert adapter._resolve_execution_timestamp_ms(order=None, deal=None) == 0

    def test_an_unresolvable_broker_timestamp_is_recorded_as_the_current_time(self) -> None:
        """The second falsification in this path, and the more consequential one.

        A fill the broker sent without a usable timestamp is stored with
        `datetime.now()`. So the record carries a **plausible, wrong** time rather than
        an obviously missing one - and because `_build_dedupe_key` reads the same
        resolved `timestamp_ms`, "0" reaches the key too (see the collision test below).

        This is the same trade-off as the Alpaca "persisted vs skipped" report: a
        falsified value is easy to miss later, whereas `0` would at least look wrong.
        """
        from datetime import datetime, timezone

        from live_runner.ctrader_adapter import _to_datetime_from_ms

        before = datetime.now(timezone.utc)
        recorded = _to_datetime_from_ms(0)
        after = datetime.now(timezone.utc)

        assert before <= recorded <= after, (
            "an unresolvable timestamp is no longer recorded as the current time; the "
            "falsification this documents has been fixed and the test should be updated"
        )
        # None falsifies the same way. A fresh bound is taken here because this call
        # happens *after* `after` was captured, so reusing `after` would be comparing
        # against a moment in the past.
        none_before = datetime.now(timezone.utc)
        recorded_none = _to_datetime_from_ms(None)
        assert none_before <= recorded_none <= datetime.now(timezone.utc)
        assert _to_datetime_from_ms(1_700_000_000_000) == datetime.fromtimestamp(
            1_700_000_000.0, tz=timezone.utc
        )

    def test_a_deal_id_yields_a_key_that_needs_no_fallback(self) -> None:
        """The strong key: one deal is one fill, so the key is exact."""
        adapter = self._adapter()
        key = adapter._build_dedupe_key(
            SimpleNamespace(dealId=987654),
            order_id=123,
            timestamp_ms=1000,
            filled_volume=500.0,
        )
        assert key == "ctrader:deal:987654", (
            f"the deal id is not used for the key, so the exact per-deal identity is lost: {key}"
        )

    def test_a_deal_id_of_zero_falls_back_instead_of_claiming_the_deal_namespace(self) -> None:
        """`dealId = 0` means "no deal id", not "deal zero".

        Claiming `ctrader:deal:0` for every such event would collapse *all* of them onto
        one key, so the positive check has to run first.
        """
        adapter = self._adapter()
        key = adapter._build_dedupe_key(
            SimpleNamespace(dealId=0),
            order_id=123,
            timestamp_ms=1000,
            filled_volume=500.0,
        )
        assert key != "ctrader:deal:0", (
            "a zero deal id claimed the exact key namespace, so every such fill collides"
        )
        assert key == "ctrader:order:123:1000:500.0", key

    def test_the_fallback_key_carries_every_component_that_separates_two_fills(self) -> None:
        """Three components, so three ways for two fills to differ.

        Without the volume, two equal-sized fills on one order in the same millisecond
        collide. Without the order, fills on different orders collide. Each is asserted
        by changing exactly one component.
        """
        adapter = self._adapter()
        base = adapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=123, timestamp_ms=1000, filled_volume=500.0
        )
        assert base == "ctrader:order:123:1000:500.0", base
        assert (
            adapter._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=123, timestamp_ms=1000, filled_volume=500.0
            )
            == base
        ), "the same fill must produce the same key, or every fill is stored twice"
        for changed in (
            adapter._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=124, timestamp_ms=1000, filled_volume=500.0
            ),
            adapter._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=123, timestamp_ms=2000, filled_volume=500.0
            ),
            adapter._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=123, timestamp_ms=1000, filled_volume=501.0
            ),
        ):
            assert changed != base, (
                f"a distinct fill produced the same key {base}, so it would be silently "
                "discarded as a duplicate"
            )

    def test_two_fills_without_a_deal_id_or_a_timestamp_collide_KNOWN_DEFECT(self) -> None:
        """**A real defect, documented rather than pinned as acceptable.**

        The fallback key is `(order_id, timestamp_ms, filled_volume)`. When the broker
        sends no deal id *and* no usable timestamp, `timestamp_ms` is 0 for every such
        event - so two equal-sized fills on one order produce **identical keys**, and the
        second is logged as "Skipped duplicate cTrader execution fill" and lost.

        The consequence is the awkward one: the log says *duplicate* when the event was
        a genuine second fill. The position ends up short and, unlike the price case,
        there is no downstream guard - `filled_volume > 0` passes, the symbol resolves,
        and the record is dropped purely on the key.

        This test asserts the collision **on purpose**. It is written to fail loudly the
        moment the defect is fixed, so the fix cannot land unnoticed with this comment
        still claiming the collision is current. Until then it is a standing KNOWN
        DEFECT, tracked for a human decision - refusing an event that cannot be
        identified is a behaviour change on the live fill path.
        """
        adapter = self._adapter()
        first = adapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=123, timestamp_ms=0, filled_volume=500.0
        )
        second = adapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=123, timestamp_ms=0, filled_volume=500.0
        )
        assert first == second == "ctrader:order:123:0:500.0", (
            "the collision is gone, so this test is now wrong in the other direction: "
            "remove the KNOWN DEFECT marker, un-rename the test, and keep the "
            "distinctness assertions in the previous test"
        )
        # And this is reachable from the real caller, not just from a hand-made pair.
        assert adapter._resolve_execution_timestamp_ms(
            order=None, deal=SimpleNamespace(dealId=0)
        ) == 0, "a deal with no timestamp no longer resolves to 0"


class TestQuantityVolumeConversion:
    """The risk-engine quantity to broker volume conversion, which had no unit test.

    `_qty_to_broker_volume` multiplies by `QTY_VOLUME_SCALE` (1000.0) and rounds.
    `_broker_volume_to_qty` divides back. Four mutations survive the suite, and the two
    that matter both end in an order the broker cannot honour.

    **The sub-unit guard (V1).** A quantity below half a scale-unit multiplies out to
    less than one, so the volume would be 0 - an order for *nothing*. The guard refuses
    it rather than submitting, and the refusal message names the cause.

    **Truncation instead of rounding (V2).** `int(x)` truncates while `round(x)` rounds.
    They differ in a band of one: at `qty = 0.0005` the product is exactly 0.5, `int()`
    gives 0 and `round()` gives 1. So the two guards are *separately* observable, and
    each is asserted with the quantity that separates them - a mutation that only
    removes the guard, or only changes the rounding, cannot pass both.

    The reason this matters more than a helper test: this is the number that reaches the
    broker. A factor-of-1000 error, or a zero-volume order, lands as a real order.
    """

    def test_volume_is_the_quantity_scaled_and_rounded(self):
        from live_runner.ctrader_adapter import (
            QTY_VOLUME_SCALE,
            _broker_volume_to_qty,
            _qty_to_broker_volume,
        )

        for qty in (0.001, 0.01, 0.5, 1.0, 100.0):
            volume = _qty_to_broker_volume(qty)
            assert volume == int(round(qty * QTY_VOLUME_SCALE))
            # And it round-trips back to the same quantity, which is what makes the
            # persisted fill comparable with the risk engine's units.
            assert _broker_volume_to_qty(volume) == pytest.approx(qty), (
                f"{qty} did not survive the round trip: {_broker_volume_to_qty(volume)}"
            )

    def test_rounding_is_used_not_truncation(self):
        """The one-unit band where `int()` and `round()` disagree.

        `qty = 0.0015` scales to a volume product of exactly 1.5. Truncation gives 1,
        rounding gives 2. **Neither is zero, so both reach the broker** - which is
        exactly why the rounding has to be pinned: an off-by-one here is an order for
        half or double the intended size, not a rejected order.

        Note the boundary the arithmetic does *not* separate, because it is worth
        knowing where the guard actually sits: a product of exactly 0.5 rounds to 0,
        not 1. Python's `round` is banker's rounding, so `round(0.5) == 0`. That makes
        `qty = 0.0005` a *refusal*, and it is asserted as one below - the guard is
        reached from both sides.
        """
        from live_runner.ctrader_adapter import QTY_VOLUME_SCALE, _qty_to_broker_volume

        half_way = 1.5 / QTY_VOLUME_SCALE
        assert int(half_way * QTY_VOLUME_SCALE) == 1, "truncation no longer differs here"
        assert round(half_way * QTY_VOLUME_SCALE) == 2, "rounding no longer differs here"
        assert _qty_to_broker_volume(half_way) == 2, (
            "the conversion truncated instead of rounding, so a quantity can be sent as "
            "less than the risk engine asked for"
        )

        # The half-unit boundary rounds *down* to zero (banker's rounding), so it is a
        # refusal rather than a volume-1 order.
        assert round(0.5) == 0
        with pytest.raises(ValueError, match="no positive quantity"):
            _qty_to_broker_volume(0.5 / QTY_VOLUME_SCALE)

    def test_a_quantity_that_would_convert_to_zero_volume_is_refused(self):
        from live_runner.ctrader_adapter import QTY_VOLUME_SCALE, _qty_to_broker_volume

        # Any positive qty under half a scale-unit rounds to a volume of 0.
        for qty in (0.4 / QTY_VOLUME_SCALE, 1e-9, 1e-12):
            with pytest.raises(ValueError, match="no positive quantity"):
                _qty_to_broker_volume(qty)

    @pytest.mark.parametrize("qty", [0.0, -1.0, -0.5, float("nan"), float("inf")])
    def test_a_non_positive_or_non_finite_quantity_is_refused(self, qty: float) -> None:
        """The refusal comes from the shared `coerce_positive_live_qty`, as with Alpaca."""
        from live_runner.ctrader_adapter import _qty_to_broker_volume

        with pytest.raises(ValueError, match="finite and positive"):
            _qty_to_broker_volume(qty)

    @pytest.mark.parametrize("scale", [0.0, -1.0, float("nan"), float("inf")])
    def test_an_unusable_volume_scale_is_refused(self, scale: float) -> None:
        """A zero or negative scale would divide by zero or flip the sign.

        Both would produce a volume of a completely different magnitude, so this is
        refused rather than clamped.
        """
        from live_runner.ctrader_adapter import _qty_to_broker_volume

        with pytest.raises(ValueError, match="volume_per_qty"):
            _qty_to_broker_volume(1.0, volume_per_qty=scale)

    def test_the_two_brokers_use_the_same_quantity_converter(self):
        """One definition, both adapters - the seam lesson from the cTrader qty guard.

        The Alpaca and cTrader quantity guards were separately enforced and the
        cTrader copy was untested until recently. This asserts the *conversion* is
        reached through one shared entry point rather than re-derived per broker, so a
        future edit cannot diverge them silently.
        """
        from live_runner.base_adapter import coerce_positive_live_qty
        from live_runner.ctrader_adapter import _qty_to_broker_volume

        with pytest.raises(ValueError, match="finite and positive"):
            _qty_to_broker_volume(0.0)
        # Same refusal text and same source of truth.
        with pytest.raises(ValueError) as via_converter:
            _qty_to_broker_volume(-1.0)
        with pytest.raises(ValueError) as via_helper:
            coerce_positive_live_qty(-1.0)
        assert str(via_converter.value) == str(via_helper.value)


def test_is_terminal_auth_error_detects_ctrader_invalid_token_code() -> None:
    assert _is_terminal_auth_error("CH_ACCESS_TOKEN_INVALID", "")


def test_is_terminal_auth_error_ignores_transient_timeout() -> None:
    assert not _is_terminal_auth_error("TIMEOUT", "Connection timeout")


def test_adapter_uses_strict_canonical_demo_switch(monkeypatch) -> None:
    monkeypatch.setenv("CTRADER_DEMO", "false")

    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )

    assert adapter.demo is False


def test_adapter_rejects_invalid_canonical_demo_switch(monkeypatch) -> None:
    monkeypatch.setenv("CTRADER_DEMO", "tru")

    with pytest.raises(ValueError, match="CTRADER_DEMO"):
        CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
        )


def test_connect_override_limits_attempts(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        connect_max_attempts=1,
        connect_timeout_seconds=0.01,
    )
    attempts = 0

    class _Client:
        running = False

        def startService(self) -> None:
            nonlocal attempts
            attempts += 1

    monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
    monkeypatch.setattr(adapter, "_create_client", lambda: _Client())

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter.connect())

    assert attempts == 1


def test_connect_reconciliation_failure_is_retried_without_reconnect(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        connect_max_attempts=1,
    )
    client = SimpleNamespace(running=False)

    def start_service() -> None:
        client.running = True
        adapter._on_connected(client)

    client.startService = start_service
    create_calls = 0

    def create_client():
        nonlocal create_calls
        create_calls += 1
        return client

    async def authenticate() -> None:
        adapter._authenticated = True

    async def fail_reconciliation(*, max_signals: int = 200) -> int:
        raise RuntimeError("temporary reconciliation failure")

    monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
    monkeypatch.setattr(adapter, "_create_client", create_client)
    monkeypatch.setattr(adapter, "_authenticate", authenticate)
    monkeypatch.setattr(adapter, "reconcile_executions", fail_reconciliation)

    asyncio.run(adapter.connect())

    assert create_calls == 1
    assert adapter._connected is True
    assert adapter._authenticated is True
    assert adapter._reconciliation_required is True
    assert adapter._reconciliation_retry_at == 0.0

    async def succeed_reconciliation(*, max_signals: int = 200) -> int:
        return 0

    monkeypatch.setattr(adapter, "reconcile_executions", succeed_reconciliation)
    asyncio.run(adapter._run_pending_reconciliation(2))

    assert create_calls == 1
    assert adapter._reconciliation_required is False


def test_on_message_marks_terminal_auth_failure_for_not_authorized() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._heartbeat_payload_type = -1
    adapter._execution_payload_type = -2
    adapter._error_payload_type = 100
    adapter._order_error_payload_type = -3
    adapter._Protobuf = SimpleNamespace(
        extract=lambda _message: SimpleNamespace(
            errorCode="TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
            description="Trading account is not authorized",
        )
    )

    adapter._on_message(None, SimpleNamespace(payloadType=100))

    assert adapter._terminal_auth_failure_code == "TRADING_ACCOUNT_IS_NOT_AUTHORIZED"
    assert "not authorized" in adapter._terminal_auth_failure_reason.lower()


def test_on_message_keeps_terminal_state_clear_for_already_logged_in() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._heartbeat_payload_type = -1
    adapter._execution_payload_type = -2
    adapter._error_payload_type = 101
    adapter._order_error_payload_type = -3
    adapter._Protobuf = SimpleNamespace(
        extract=lambda _message: SimpleNamespace(
            errorCode="ALREADY_LOGGED_IN",
            description="Open API application is already authorized",
        )
    )

    adapter._on_message(None, SimpleNamespace(payloadType=101))

    assert adapter._terminal_auth_failure_code == ""
    assert adapter._terminal_auth_failure_reason == ""


def test_execution_queue_overflow_requests_broker_reconciliation() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    payload = SimpleNamespace(executionType=1)
    adapter._client = current_client
    adapter._execution_payload_type = 200
    adapter._heartbeat_payload_type = -1
    adapter._Protobuf = SimpleNamespace(extract=lambda _message: payload)
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    adapter._on_message(current_client, SimpleNamespace(payloadType=200))

    assert adapter._reconciliation_required is True


def test_failed_execution_event_is_requeued_and_reconciled() -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()

    assert adapter._requeue_execution_event(event) is True
    assert adapter._execution_queue.get_nowait() is event
    assert adapter._reconciliation_required is True
    assert adapter._execution_retry_counts[id(event)] == 1


def test_listener_requeues_a_failed_execution_before_retry(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    event = object()
    attempts = 0

    async def _persist(received):
        nonlocal attempts
        assert received is event
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary mapping failure")
        raise asyncio.CancelledError

    async def _reconcile(*, max_signals: int = 200) -> int:
        return 0

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(adapter, "_persist_execution_event", _persist)
    monkeypatch.setattr(adapter, "reconcile_executions", _reconcile)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    adapter._execution_queue.put_nowait(event)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 2
    assert adapter._execution_retry_counts[id(event)] == 1


def test_execution_event_requeue_is_bounded_and_defers_to_reconciliation() -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()
    for _ in range(module.EXECUTION_RETRY_LIMIT + 1):
        adapter._requeue_execution_event(event)
        while not adapter._execution_queue.empty():
            adapter._execution_queue.get_nowait()

    assert len(adapter._unresolved_execution_events) == 1
    assert adapter._unresolved_execution_events[0] is event
    assert adapter._reconciliation_required is True


def test_listener_fails_closed_when_execution_requeue_is_exhausted(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    event = object()
    attempts = 0

    async def _persist(_event):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary fill persistence failure")

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(adapter, "_persist_execution_event", _persist)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    adapter._execution_queue.put_nowait(event)

    with pytest.raises(RuntimeError, match="exhausted its retry budget"):
        asyncio.run(adapter.listen_executions())

    assert attempts == module.EXECUTION_RETRY_LIMIT + 1
    assert adapter._unresolved_execution_events[-1] is event
    assert adapter._reconciliation_required is True


def test_successful_reconciliation_does_not_discard_unresolved_events(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()
    adapter._reconciliation_required = True
    adapter._unresolved_execution_events.append(event)
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    async def _reconcile(*, max_signals: int = 200) -> int:
        return 0

    monkeypatch.setattr(adapter, "reconcile_executions", _reconcile)
    asyncio.run(adapter._run_pending_reconciliation(2))

    assert adapter._reconciliation_required is True
    assert list(adapter._unresolved_execution_events) == [event]


def test_stale_execution_callback_cannot_request_reconciliation() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    adapter._client = current_client
    adapter._execution_payload_type = 200
    adapter._heartbeat_payload_type = -1
    adapter._Protobuf = SimpleNamespace(extract=lambda _message: object())
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    adapter._on_message(object(), SimpleNamespace(payloadType=200))

    assert adapter._reconciliation_required is False


def test_unmapped_execution_fill_is_not_acknowledged_as_persisted(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    event = SimpleNamespace(
        executionType=1,
        order=SimpleNamespace(orderId=45678, clientOrderId=""),
        deal=None,
        HasField=lambda field: field == "order",
    )
    monkeypatch.setattr(module, "find_live_signal_id_by_broker_order_id", lambda *_args: None)

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(event))


def test_listener_keeps_processing_queue_when_reconciliation_fails(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._reconciliation_required = True
    event = object()
    attempts = 0
    persisted: list[object] = []

    async def reconcile_executions(*, max_signals: int = 200) -> int:
        nonlocal attempts
        assert max_signals == 200
        attempts += 1
        raise RuntimeError("temporary broker failure")

    async def persist_execution(received: object) -> None:
        persisted.append(received)
        raise asyncio.CancelledError

    adapter._execution_queue.put_nowait(event)
    monkeypatch.setattr(adapter, "reconcile_executions", reconcile_executions)
    monkeypatch.setattr(adapter, "_persist_execution_event", persist_execution)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 1
    assert persisted == [event]
    assert adapter._reconciliation_required is True
    assert adapter._reconciliation_retry_at > 0


def test_listener_clears_reconciliation_flag_after_success(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._reconciliation_required = True
    event = object()
    attempts = 0

    async def reconcile_executions(*, max_signals: int = 200) -> int:
        nonlocal attempts
        assert max_signals == 200
        attempts += 1
        return 0

    async def persist_execution(_event: object) -> None:
        raise asyncio.CancelledError

    adapter._execution_queue.put_nowait(event)
    monkeypatch.setattr(adapter, "reconcile_executions", reconcile_executions)
    monkeypatch.setattr(adapter, "_persist_execution_event", persist_execution)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 1
    assert adapter._reconciliation_required is False
    assert adapter._reconciliation_retry_at == 0.0


def test_write_env_key_uses_portable_lock_around_atomic_replace(monkeypatch, tmp_path) -> None:
    from live_runner import ctrader_adapter as module

    env_path = tmp_path / ".accounts.env"
    env_path.write_text("CTRADER_ACCESS_TOKEN=old\nUNCHANGED=value\n", encoding="utf-8")
    events: list[str] = []
    replacement: tuple[Path, Path] | None = None
    active_lock = None
    real_replace = module.os.replace

    class _TrackingLock:
        def __init__(self, lock_file: str) -> None:
            self.path = Path(lock_file)
            self.active = False

        def __enter__(self):
            self.active = True
            events.append("lock-enter")
            return self

        def __exit__(self, *_args) -> None:
            self.active = False
            events.append("lock-exit")

    @contextmanager
    def tracking_lock(handle):
        nonlocal active_lock
        active_lock = _TrackingLock(handle.name)
        active_lock.active = True
        events.append("lock-enter")
        try:
            yield
        finally:
            active_lock.active = False
            events.append("lock-exit")

    def tracked_replace(source: Path, destination: Path) -> None:
        nonlocal replacement
        assert active_lock is not None
        assert active_lock.active
        replacement = (Path(source), Path(destination))
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(module, "_exclusive_file_lock", tracking_lock)
    monkeypatch.setattr(module.os, "replace", tracked_replace)

    module._write_env_key(env_path, "CTRADER_ACCESS_TOKEN", "new")

    assert active_lock is not None
    assert active_lock.path == env_path.with_name(f"{env_path.name}.lock")
    assert events == ["lock-enter", "replace", "lock-exit"]
    assert replacement is not None
    source, destination = replacement
    assert source.parent == env_path.parent
    assert source.name.startswith(f".{env_path.name}.")
    assert source.suffix == ".tmp"
    assert destination == env_path.resolve()
    assert env_path.read_text(encoding="utf-8") == (
        "CTRADER_ACCESS_TOKEN=new\nUNCHANGED=value\n"
    )


def test_refresh_access_token_updates_runtime_and_dotenv(tmp_path) -> None:
    env_path = tmp_path / ".accounts.env"
    env_path.write_text(
        "CTRADER_ACCESS_TOKEN=old\nCTRADER_REFRESH_TOKEN=old_refresh\n",
        encoding="utf-8",
    )
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=env_path,
    )

    class _RefreshReq:
        def __init__(self) -> None:
            self.refreshToken = ""

    class _RefreshRes:
        accessToken = "new_access"
        refreshToken = "new_refresh"
        expiresIn = 2628000

    async def _fake_send(request, **_kwargs):
        assert isinstance(request, _RefreshReq)
        assert request.refreshToken == "old_refresh"
        return _RefreshRes()

    adapter._ProtoOARefreshTokenReq = _RefreshReq
    adapter._ProtoOARefreshTokenRes = _RefreshRes
    adapter._send_request = _fake_send

    assert asyncio.run(adapter._refresh_access_token()) is True
    assert adapter.access_token == "new_access"
    assert adapter.refresh_token == "new_refresh"
    contents = env_path.read_text(encoding="utf-8")
    assert "CTRADER_ACCESS_TOKEN=new_access" in contents
    assert "CTRADER_REFRESH_TOKEN=new_refresh" in contents


def test_stale_client_callbacks_cannot_change_current_connection_state() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    stale_client = object()
    adapter._client = current_client

    adapter._on_connected(stale_client)
    assert adapter._connected is False
    assert adapter._connected_event.is_set() is False

    adapter._connected = True
    adapter._authenticated = True
    adapter._connected_event.set()
    adapter._on_disconnected(stale_client, "late disconnect")
    assert adapter._connected is True
    assert adapter._authenticated is True
    assert adapter._connected_event.is_set() is True


def test_send_request_with_refresh_retry_retries_once_after_token_refresh() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old",
        refresh_token="refresh",
        account_id="46736452",
    )
    calls: list[object] = []
    request = object()
    response = object()

    async def _fake_send(sent_request, **_kwargs):
        calls.append(sent_request)
        if len(calls) == 1:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired")
        return response

    async def _fake_refresh(**_kwargs):
        adapter.access_token = "new"
        return True

    adapter._send_request = _fake_send
    adapter._refresh_access_token = _fake_refresh

    assert asyncio.run(adapter._send_request_with_refresh_retry(request)) is response
    assert calls == [request, request]


def test_delayed_old_token_error_does_not_rotate_refresh_token_twice(tmp_path) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    both_requests_started = asyncio.Event()
    first_refresh_complete = asyncio.Event()
    request_attempts: dict[object, int] = {}
    first_attempts_started = 0
    class RefreshRequest:
        def __init__(self) -> None:
            self.refreshToken = ""

    class RefreshResponse:
        accessToken = "new_access"
        refreshToken = "new_refresh"
        expiresIn = 2628000

    refresh_requests: list[str] = []
    request_a = object()
    request_b = object()

    async def send_request(request, **_kwargs):
        nonlocal first_attempts_started
        if isinstance(request, RefreshRequest):
            refresh_requests.append(request.refreshToken)
            first_refresh_complete.set()
            return RefreshResponse()
        attempt = request_attempts.get(request, 0) + 1
        request_attempts[request] = attempt
        if attempt > 1:
            return request
        first_attempts_started += 1
        if first_attempts_started == 2:
            both_requests_started.set()
        await both_requests_started.wait()
        if request is request_a:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired old token")
        await first_refresh_complete.wait()
        raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "delayed old token error")

    adapter._ProtoOARefreshTokenReq = RefreshRequest
    adapter._ProtoOARefreshTokenRes = RefreshResponse
    adapter._send_request = send_request

    async def run_requests():
        return await asyncio.gather(
            adapter._send_request_with_refresh_retry(request_a),
            adapter._send_request_with_refresh_retry(request_b),
        )

    assert asyncio.run(run_requests()) == [request_a, request_b]
    assert refresh_requests == ["old_refresh"]


def test_waiter_does_not_treat_failed_token_refresh_as_success(tmp_path) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=tmp_path / ".accounts.env",
    )

    class RefreshRequest:
        def __init__(self) -> None:
            self.refreshToken = ""

    class InvalidRefreshResponse:
        accessToken = ""
        refreshToken = ""

    refresh_attempts = 0

    async def send_request(request, **_kwargs):
        nonlocal refresh_attempts
        if isinstance(request, RefreshRequest):
            refresh_attempts += 1
            return InvalidRefreshResponse()
        raise AssertionError("Only refresh requests are expected")

    adapter._ProtoOARefreshTokenReq = RefreshRequest
    adapter._ProtoOARefreshTokenRes = InvalidRefreshResponse
    adapter._send_request = send_request

    async def refresh_twice():
        return await asyncio.gather(
            adapter._refresh_access_token(failed_access_token="old_access"),
            adapter._refresh_access_token(failed_access_token="old_access"),
        )

    assert asyncio.run(refresh_twice()) == [False, False]
    assert refresh_attempts == 2
    assert adapter.access_token == "old_access"
    assert adapter.refresh_token == "old_refresh"


def test_auth_refresh_rebuilds_account_auth_request_with_new_access_token() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="refresh",
        account_id="46736452",
    )
    sent_tokens: list[str] = []

    class _AccountAuthReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.accessToken = ""

    class _AccountAuthRes:
        pass

    adapter._ProtoOAAccountAuthReq = _AccountAuthReq
    adapter._ProtoOAAccountAuthRes = _AccountAuthRes

    async def _fake_send(request, **_kwargs):
        sent_tokens.append(request.accessToken)
        if len(sent_tokens) == 1:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired")
        return _AccountAuthRes()

    async def _fake_refresh():
        adapter.access_token = "new_access"
        return True

    adapter._send_request = _fake_send
    adapter._refresh_access_token = _fake_refresh

    async def _run() -> object:
        try:
            return await adapter._send_account_auth_request(timeout_seconds=15)
        except CTraderApiError as exc:
            assert _is_terminal_auth_error(exc.error_code, str(exc))
            assert await adapter._refresh_access_token()
            return await adapter._send_account_auth_request(timeout_seconds=15)

    assert isinstance(asyncio.run(_run()), _AccountAuthRes)
    assert sent_tokens == ["old_access", "new_access"]


def test_connect_cancellation_tears_down_started_client(monkeypatch) -> None:
    async def _run() -> tuple[CTraderLiveAdapter, int]:
        adapter = CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
            connect_max_attempts=1,
            connect_timeout_seconds=60,
        )
        client_started = asyncio.Event()
        stop_calls = 0

        class _Client:
            running = False

            def startService(self) -> None:
                self.running = True
                client_started.set()

            def stopService(self) -> None:
                nonlocal stop_calls
                stop_calls += 1
                self.running = False

        monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
        monkeypatch.setattr(adapter, "_create_client", lambda: _Client())

        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(client_started.wait(), timeout=0.1)
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect_task
        return adapter, stop_calls

    adapter, stop_calls = asyncio.run(_run())

    assert stop_calls == 1
    assert adapter._client is None
    assert adapter._connected is False
    assert adapter._authenticated is False
    assert adapter._connected_event.is_set() is False


def test_disconnect_times_out_instead_of_hanging() -> None:
    async def _run() -> CTraderLiveAdapter:
        adapter = CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
        )
        adapter._loop = asyncio.get_running_loop()
        adapter._connected = True
        adapter._authenticated = True

        class _NeverCompletes:
            def asFuture(self, loop):
                return loop.create_future()

        class _Client:
            running = True

            def stopService(self):
                return _NeverCompletes()

        adapter._client = _Client()
        await adapter.disconnect()
        return adapter

    adapter = asyncio.run(_run())

    assert adapter._client is None
    assert adapter._connected is False
    assert adapter._authenticated is False


def test_get_balance_marks_terminal_auth_failure_for_not_authorized() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._connected = True
    adapter._authenticated = True

    class _TraderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0

    adapter._ProtoOATraderReq = _TraderReq
    adapter._ProtoOATraderRes = type("_TraderRes", (), {})

    async def _raise_auth_error(*_args, **_kwargs):
        raise CTraderApiError(
            error_code="TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
            description="Trading account is not authorized",
        )

    adapter._send_request = _raise_auth_error

    with pytest.raises(CTraderAuthTerminalError):
        asyncio.run(adapter.get_balance())

    assert adapter._terminal_auth_failure_code == "TRADING_ACCOUNT_IS_NOT_AUTHORIZED"
    assert "not authorized" in adapter._terminal_auth_failure_reason.lower()


def test_a_new_connection_cannot_inherit_authentication_from_the_previous_one() -> None:
    """A connect event means a socket that has not been authenticated yet.

    `_on_disconnected` clears `_authenticated`, and `_teardown_client` clears it
    too, so the ordinary paths leave nothing stale. What neither covers is a
    transport that reports a fresh connection on the same client without a
    disconnect in between - an internal reconnect, or a dropped event.

    That case matters because of what `_authenticate` does with the flag:

        if self._authenticated and self._symbol_name_to_id:
            return

    A stale True plus a populated symbol cache returns before any authentication
    request is sent. The adapter would then satisfy every `if not self._connected
    or not self._authenticated` gate - a dozen of them, including the order paths -
    on a connection the broker never authenticated.

    The fix belongs in `_on_connected` rather than in the callers: establishing a
    connection and being authenticated on it are separate facts, and a connect
    callback is the only place that learns the first one has just become true.
    """
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    client = object()
    adapter._client = client
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._already_logged_in_notified = True

    adapter._on_connected(client)

    assert adapter._connected is True, "the connection is real; only the auth state is stale"
    assert adapter._authenticated is False, (
        "a new connection must not inherit authentication from the previous one - "
        "_authenticate returns early on a stale True, so every downstream gate "
        "would pass on a socket the broker never authenticated"
    )
    assert adapter._already_logged_in_notified is False, (
        "the already-logged-in notice is per-connection too; a new session should "
        "be free to log the fact again"
    )


def test_a_blanked_ctrader_demo_value_is_refused_rather_than_defaulted(monkeypatch) -> None:
    """An empty `CTRADER_DEMO` means someone blanked it, not that it was never set.

    `live_runner/credential_env.resolve_env_value` is explicit about this: a key
    defined in `.accounts.env` wins "even when its value is empty
    (present-but-empty stays empty)". Preserving that distinction is the whole
    point of the function, and it is what lets an operator blank a value on purpose.

    The adapter then discarded it with `resolve_env_value(...) or None`, which turns
    every falsy value into "absent" and hands the parser a `None` - so the strict
    parse never sees the empty string and the default is applied instead. The other
    two readers of the same variable disagree: `preflight` and `daemon` pass the
    value through and both raise on `''`.

    The consequences are not hypothetical. An operator who blanks `CTRADER_DEMO`
    gets a preflight that refuses to start and an adapter that would quietly
    connect as demo. The adapter is the thing that opens the connection, so it is
    the one whose opinion counts - and the divergence sits one token away from the
    worst outcome in this codebase: change `default=True` to `default=False` and
    the same empty value selects *live* trading, silently, with no error anywhere.

    So the empty value has to reach the parser, which rejects it.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    env_values = {"CTRADER_DEMO": ""}

    with pytest.raises(ValueError) as excinfo:
        CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="1",
            env_values=env_values,
        )
    assert "CTRADER_DEMO" in str(excinfo.value), (
        f"the refusal did not name the variable: {excinfo.value}"
    )


def test_an_absent_ctrader_demo_still_defaults_to_demo(monkeypatch) -> None:
    """The default must survive: only a *present* empty value is an error.

    Distinguishing "blanked" from "never set" is the entire point, so the fix has to
    refuse the first without breaking the second.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="1",
        env_values={},
    )
    assert adapter.demo is True


def test_an_explicit_demo_argument_still_wins_over_the_environment(monkeypatch) -> None:
    """A caller that passed `demo=` has already decided, blanks included.

    Guarding the fallback must not start second-guessing an explicit argument.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="1",
        demo=False,
        env_values={"CTRADER_DEMO": ""},
    )
    assert adapter.demo is False


# --------------------------------------------------------------------------
# The connection gate, in all six of the places it appears
# --------------------------------------------------------------------------
#
# Found by mutation testing `live_runner/ctrader_adapter.py`. The live bug this
# file's own comments describe was a new connection inheriting `_authenticated`,
# so `_authenticate` returned early and "every `not self._authenticated` gate
# passed on a connection the broker never authenticated".
#
# The *source* of that bug is pinned - `_on_connected` clearing the flag is
# caught - but the gate itself was not. Dropping `not self._authenticated` from
# `submit_order`, or dropping `not self._connected`, each left all 519 collected
# tests green. So the second line of defence, the one that would catch a stale
# flag arriving by any other route, had no test at all.
#
# The gate appears **six** times in the file, which is this branch's recurring
# "one rule, two implementations" shape at six. The structural test below covers
# all of them and any future seventh.

def test_submit_order_reconnects_when_the_socket_is_not_authenticated() -> None:
    """Both halves of the gate, on the one method that places orders.

    Driven through the real `submit_order` in the state a stale `_authenticated`
    produces - connected, never authenticated - and the assertion is on whether
    `connect()` ran. The send path is stubbed, so the call itself raises; the
    mutation this catches skipped the re-authentication and reached the send.
    """
    from datetime import datetime, timezone

    from shared.models import LiveSignal

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    calls: list[str] = []

    async def fake_connect() -> None:
        calls.append("connect")
        adapter._connected = True
        adapter._authenticated = True

    async def fake_send(*args, **kwargs):
        raise AssertionError("reached the send path without re-authenticating")

    adapter.connect = fake_connect  # type: ignore[method-assign]
    adapter._send_request = fake_send  # type: ignore[method-assign]

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )

    # Connected, NOT authenticated - the state the documented live bug produced.
    adapter._connected = True
    adapter._authenticated = False

    # Whatever happens next is not this test's business - the symbol cache is
    # empty, so `submit_order` legitimately refuses during pre-persistence
    # validation. What is under test is whether it re-authenticated *first*, and
    # the mutation this catches skipped straight to that validation.
    with pytest.raises(Exception):  # noqa: B017 - the type is not the subject
        asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert calls == ["connect"], (
        "submit_order proceeded without re-authenticating on a connected but "
        "unauthenticated socket"
    )


def test_every_connection_gate_refuses_unless_both_flags_hold() -> None:
    """All the refusal gates, structurally, so a new one cannot forget a half.

    Scoped to *refusal* gates - an `if` whose body re-connects - rather than every
    `if` mentioning the flags. A positive precondition such as `connect()`'s
    `if self._connected and self._authenticated and self._client is not None` is the
    opposite shape and is not what this rule is about.

    A gate checking only `_connected` lets an unauthenticated socket through; one
    checking only `_authenticated` lets a disconnected one through. The first is the
    documented live bug; both were survivable until now.
    """
    import ast
    import pathlib

    import live_runner.ctrader_adapter as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))

    refusal_gates: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if "self.connect()" not in body:
            continue
        refusal_gates.append((node.lineno, ast.unparse(node.test)))

    assert len(refusal_gates) >= 6, (
        f"expected at least six re-connecting gates, found {len(refusal_gates)}: "
        f"{refusal_gates} - the adapter changed shape, so re-derive what these are"
    )

    incomplete = [
        (line, test)
        for line, test in refusal_gates
        if "not self._connected" not in test or "not self._authenticated" not in test
    ]
    assert not incomplete, (
        "every re-connecting gate must require both `not self._connected` and "
        f"`not self._authenticated`; these do not: {incomplete}"
    )


class TestSymbolIdResolutionAndPrePersistence:
    """The fail-closed symbol resolver, the pre-persistence gate, and the auth log line.

    Four mutations survived here, and the two that matter are both about a symbol
    resolving to something it should not.

    **`_symbol_name_for_id` refuses to guess, and the docstring says why in numbers:**
    a fill for an unmapped id cannot be scaled, and "guessing FX would misstate exposure
    by 10-1000x on XAU/indices". That is the sharpest statement in the adapter about what
    a wrong symbol costs, and the `raise` that enforces it had no test - the natural
    alternative (`return ""`) survived. An empty name is the dangerous version of this
    bug rather than a loud one: the caller scales by symbol, so an unresolvable id that
    quietly becomes "" reaches the quantity conversion as a *known* symbol with no
    conversion factor, instead of stopping the fill.

    **`_validate_pre_persistence` runs before the first write** (B-8) for the same reason
    inverted: a signal persisted without a broker order wedges every redelivery on the
    duplicate-persisted recovery path. The `.upper()` is load-bearing for a lowercase
    symbol, and had no test - without it a perfectly valid signal is rejected as unknown
    and never persisted at all, which is the safe direction but still a silent miss.

    The `int()` coercion in the resolver is asserted with a *string* id, because that is
    the only input where the two versions differ. Its rougher edge is also pinned: a
    non-numeric id raises from `int()` with a bare message, not from the resolver's own
    "not in the symbols cache" text. That is left as-is - the caller's handling is the
    right place for that - but it should not be discovered by surprise.
    """

    @staticmethod
    def _adapter(symbols=None):
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter._symbol_name_to_id = {"EURUSD": 5111, "XAUUSD": 5222} if symbols is None else symbols
        return adapter

    @staticmethod
    def _signal(action="buy", symbol="EURUSD"):
        from shared.models import LiveSignal

        return LiveSignal(
            deployment_id="dep-1", symbol=symbol, action=action,
            theoretical_price=1.1, timestamp=1_700_000_000.0,
        )

    def test_a_known_symbol_id_resolves_to_its_upper_name(self) -> None:
        """The happy path, and the direction of the mapping: id in, upper name out."""
        adapter = self._adapter()
        assert adapter._symbol_name_for_id(5111) == "EURUSD"
        assert adapter._symbol_name_for_id(5222) == "XAUUSD"

    def test_an_unmapped_symbol_id_refuses_rather_than_guessing(self) -> None:
        """**The surviving mutation, and the one the docstring is written about.**

        Resolving to `""` instead of raising reaches the quantity conversion as a known
        symbol with no conversion factor - a silent exposure misstatement rather than a
        refused fill. The message is asserted too, because "refusing to scale its volume
        with a guessed symbol" is the part that tells an operator what happened.
        """
        adapter = self._adapter()
        with pytest.raises(ValueError) as excinfo:
            adapter._symbol_name_for_id(9999)
        message = str(excinfo.value)
        assert "9999" in message, message
        assert "not in the symbols cache" in message, message
        assert "guessed symbol" in message, message

    def test_an_empty_cache_refuses_every_id(self) -> None:
        """Symbols not yet loaded means no id resolves - it never falls back to a default."""
        adapter = self._adapter(symbols={})
        for symbol_id in (5111, 5222, 0, -1):
            with pytest.raises(ValueError):
                adapter._symbol_name_for_id(symbol_id)

    def test_a_string_symbol_id_still_resolves(self) -> None:
        """The `int()` coercion, asserted with a string because that is the only input
        where removing it changes anything - and the only realistic one, since a fill id
        read off the wire need not arrive as an `int`.
        """
        adapter = self._adapter()
        assert adapter._symbol_name_for_id("5111") == "EURUSD"

    def test_a_non_numeric_symbol_id_raises_from_the_coercion(self) -> None:
        """Pinned as-is: the error is `int()`'s, not the resolver's explanatory one.

        A caller gets "invalid literal for int() with base 10" instead of the sentence
        explaining that a guessed symbol would be unscalable. Left unchanged deliberately
        - the right place to translate this is the caller, which already handles
        `ValueError` - but it must not be discovered by surprise during an incident.
        """
        adapter = self._adapter()
        with pytest.raises(ValueError) as excinfo:
            adapter._symbol_name_for_id("not-a-number")
        assert "int" in str(excinfo.value), str(excinfo.value)

    def test_a_lowercase_signal_symbol_is_accepted(self) -> None:
        """The surviving mutation. Without `.upper()` a valid signal is refused as unknown.

        no-raise: test_an_unknown_symbol_is_refused_before_persistence
        """
        adapter = self._adapter()
        adapter._validate_pre_persistence(self._signal(symbol="eurusd"))
        adapter._validate_pre_persistence(self._signal(symbol="EURUSD"))

    def test_the_pre_persistence_gate_accepts_both_actions_and_refuses_the_rest(self) -> None:
        """The action allowlist is exactly buy and sell - widened, an order could be sent.

        **The typed model already forbids anything else**, so this guard is
        defence-in-depth and can only be reached by a caller that bypasses validation.
        `LiveSignal.action` is a pydantic `Literal["buy", "sell"]`, and the first version
        of this test learned that the hard way: constructing the signal with
        `action="hold"` raises a pydantic `literal_error` long before the adapter is
        reached, and the assertion then failed on the wrong message.

        That is worth recording rather than working around. It means the branch is
        reachable only via `LiveSignal.model_construct` (which skips validation) or a
        duck-typed stand-in - both used below deliberately, so the guard is tested at all
        rather than left as an unreachable line that would still be read as load-bearing.
        """
        adapter = self._adapter()
        for action in ("buy", "sell"):
            adapter._validate_pre_persistence(self._signal(action=action))

        from types import SimpleNamespace

        for action in ("hold", "close", "BUY", "", "flat", None):
            stand_in = SimpleNamespace(action=action, symbol="EURUSD")
            with pytest.raises(ValueError) as excinfo:
                adapter._validate_pre_persistence(stand_in)
            assert "Unsupported action" in str(excinfo.value), (action, str(excinfo.value))

        # And through the model's own escape hatch, so the guard is proven to hold for a
        # signal that really did skip validation rather than only for a stand-in.
        bypassed = self._signal().model_copy(update={"action": "hold"})
        with pytest.raises(ValueError) as excinfo:
            adapter._validate_pre_persistence(bypassed)
        assert "Unsupported action" in str(excinfo.value), str(excinfo.value)

    def test_an_unknown_symbol_is_refused_before_persistence(self) -> None:
        """B-8: refuse before the write, because a persisted signal with no broker order
        wedges every redelivery on the duplicate-persisted recovery path."""
        adapter = self._adapter()
        with pytest.raises(ValueError) as excinfo:
            adapter._validate_pre_persistence(self._signal(symbol="NOSUCH"))
        message = str(excinfo.value)
        assert "Unknown cTrader symbol" in message, message
        assert "refusing to persist" in message, message

    def test_the_terminal_auth_failure_is_logged_with_its_account_and_code(self, caplog) -> None:
        """The surviving mutation: the log line carries what a human needs to act.

        This is the line that explains a listener dying, and it is emitted exactly once on
        the way out. `error_code` is read with a `getattr` default because a plain
        `RuntimeError` reaching this path must still produce a usable record.
        """
        import logging

        from live_runner.ctrader_adapter import CTraderAuthTerminalError

        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "ctid-1"
        adapter.account_config_id = "acc-1"

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            adapter._log_listener_terminal_auth_failure(
                CTraderAuthTerminalError("token rejected", error_code="401")
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any("terminal auth failure" in m for m in messages), messages
        record = next(r for r in caplog.records if "terminal auth failure" in r.getMessage())
        data = record.__dict__.get("data") or getattr(record, "data", {})
        assert data["account_id"] == "ctid-1", data
        assert data["account_config_id"] == "acc-1", data
        assert data["error_code"] == "401", data
        assert "token rejected" in data["error"], data

        # A bare RuntimeError still logs, with an empty code rather than crashing.
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            adapter._log_listener_terminal_auth_failure(RuntimeError("plain failure"))
        record = next(r for r in caplog.records if "terminal auth failure" in r.getMessage())
        data = record.__dict__.get("data") or getattr(record, "data", {})
        assert data["error_code"] == "", data
        assert "plain failure" in data["error"], data


class TestConnectFailureClassificationInProduction:
    """The message an operator reads when cTrader will not authenticate.

    Found in production on 2026-10-02. The first thing worth writing down is that **my own
    first diagnosis of it was wrong, and so was the second guess.**

    The running daemon logged, once per retry, forever:

        cTrader connect/auth unavailable; daemon will retry
        data: {"account_id": 46736434, "error": "(15, 'Deferred')"}

    1. I read `15` as an OpenSSL code and called it `ssl.SSLWantReadError`. Error 15 is
       `SSL_ERROR_UNEXPECTED_MESSAGE_WHILE_READING`; `WANT_READ` is 2, and a reproduction
       gives `SSLWantReadError` with `args = (2, 'The operation did not complete (read)')`.
       `ssl.SSLError(15, 'Deferred')` - the shape I claimed - does not even str to the
       observed text; it strs to `'Deferred'`.
    2. I then assumed a Twisted `Deferred` timeout and reached for the class. Also wrong in
       its mechanism, though right about the conclusion: `defer.TimeoutError` is Twisted's
       *own* class deriving straight from `Exception`, so it is **not** the builtin
       `TimeoutError`, and on 3.11+ the builtin is also `asyncio.TimeoutError`. The
       pre-existing `isinstance(exc, asyncio.TimeoutError)` branch could therefore never
       match it. Dead code, for this exact failure.

    What is actually true, established from the source rather than inferred from a string:
    `client.py` calls `responseDeferred.addTimeout(responseTimeoutInSeconds, reactor)`, and
    Twisted answers expiry with `raise TimeoutError(timeout, "Deferred")`
    (`twisted/internet/defer.py`). So `args == (15, 'Deferred')`, `str(exc)` is exactly the
    logged text, and the 15 is `responseTimeoutInSeconds`. It is a *response* timeout - the
    request reached a socket that accepted it and nothing came back - so it is neither a
    credentials problem nor a TLS problem. The same credentials authenticated in 1.0s from a
    fresh process while the long-lived daemon timed out at 15s.

    The defect was never the diagnosis, it was the shape of the code: a *fallback* that
    fires when nothing matched, and then names a cause anyway. So the tests below use the
    real exception class, not a lookalike string, and one of them exists specifically to
    kill the "it is SSL" and "it is a builtin timeout" theories.
    """

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        adapter.ctid_trader_account_id = "acc-1"
        adapter.account_config_id = "cfg-1"
        adapter._already_logged_in_notified = False
        adapter._authenticated = True
        adapter._terminal_auth_failure_reason = ""
        adapter._terminal_auth_failure_code = ""
        return adapter

    @staticmethod
    def _messages(caplog, adapter, exc, error_text, message):
        import logging

        caplog.clear()
        with caplog.at_level(logging.INFO):
            adapter._log_connect_failure(exc, error_text, message)
        return [r.getMessage() for r in caplog.records]

    @staticmethod
    def _data(caplog, needle):
        record = next(r for r in caplog.records if needle in r.getMessage())
        return record.__dict__.get("data") or getattr(record, "data", {})

    def test_the_observed_response_timeout_is_its_own_case(self, caplog) -> None:
        """The exact production exception must not be reported as an auth failure."""
        from twisted.internet import defer

        adapter = self._adapter()
        exc = defer.TimeoutError(15, "Deferred")
        assert str(exc) == "(15, 'Deferred')", (
            f"Twisted no longer formats it this way, so this regression test is stale and the "
            f"real shape needs re-measuring: {str(exc)!r}"
        )
        messages = self._messages(caplog, adapter, exc, str(exc), "deferred")
        assert messages, "nothing was logged at all"
        assert any("NO RESPONSE" in m for m in messages), messages
        assert not any("authentication unavailable" in m for m in messages), (
            "a response timeout was reported as a credentials problem"
        )
        assert not any("NOT CLASSIFIED" in m for m in messages), (
            "a recognisable response timeout fell through to the unclassified fallback"
        )

    def test_it_is_the_class_that_decides_not_the_string(self, caplog) -> None:
        """**This is what kills the two wrong diagnoses.**

        A real Twisted timeout whose text does *not* look like the production string is still
        a response timeout - so a shape-only matcher would miss it. And the two theories this
        incident invited are both excluded here by construction, not by argument: the builtin
        `TimeoutError` (which is `asyncio.TimeoutError` on 3.11+) and
        `ssl.SSLWantReadError`/`ssl.SSLError` are *not* this failure.
        """
        from twisted.internet import defer

        adapter = self._adapter()

        # Same class, unfamiliar text: must still be classified, with the duration from args.
        exc = defer.TimeoutError(30, "something else entirely")
        messages = self._messages(caplog, adapter, exc, str(exc), "something else entirely")
        assert any("NO RESPONSE" in m for m in messages), (
            "a Twisted response timeout was only recognised by its string, so any other "
            f"wording falls into the unclassified fallback: {messages}"
        )
        assert self._data(caplog, "NO RESPONSE")["response_timeout_s"] == 30

        # The builtin/asyncio timeout keeps its own existing branch - not this one.
        messages = self._messages(caplog, adapter, TimeoutError("x"), "x", "no response")
        assert not any("NO RESPONSE" in m for m in messages), (
            f"the builtin TimeoutError was claimed as a Twisted response timeout: {messages}"
        )

        # Neither SSL theory survives contact with the code.
        import ssl

        for exc in (
            ssl.SSLWantReadError(2, "The operation did not complete (read)"),
            ssl.SSLError(15, "Deferred"),
        ):
            messages = self._messages(caplog, adapter, exc, str(exc), str(exc).lower())
            assert not any("NO RESPONSE" in m for m in messages), (
                f"{type(exc).__name__} was classified as a response timeout: {messages}"
            )

    def test_it_reports_how_long_it_waited_and_never_invents_a_number(self, caplog) -> None:
        """The duration is the diagnostic - 15s is short, 15min is a dead endpoint.

        But a `defer.TimeoutError()` built with no duration is still classifiable, and saying
        "within 0s" would be a lie an operator could act on. So the number is optional and the
        message changes instead of lying.
        """
        from twisted.internet import defer

        adapter = self._adapter()
        for seconds in (15, 90):
            exc = defer.TimeoutError(seconds, "Deferred")
            self._messages(caplog, adapter, exc, str(exc), "deferred")
            record = next(r for r in caplog.records if "NO RESPONSE" in r.getMessage())
            assert f"within {seconds}s" in record.getMessage(), record.getMessage()
            assert self._data(caplog, "NO RESPONSE")["response_timeout_s"] == seconds
            assert self._data(caplog, "NO RESPONSE")["error"] == str(exc)
            assert self._data(caplog, "NO RESPONSE")["account_id"] == "acc-1"

        caplog.clear()
        exc = defer.TimeoutError()
        messages = self._messages(caplog, adapter, exc, str(exc), "deferred")
        assert any("NO RESPONSE" in m for m in messages), messages
        assert "within the configured response timeout" in messages[0], messages[0]
        assert self._data(caplog, "NO RESPONSE")["response_timeout_s"] is None

    def test_a_real_auth_error_still_routes_to_the_credential_branch(self, caplog) -> None:
        """**The guard on the new branch.** It is checked first, so it must not swallow this.

        A response timeout and a rejected credential are the two things an operator must be
        able to tell apart, and putting the timeout first is only safe while the recogniser
        stays narrow. This is the test that fails if someone loosens it into a general
        "looks like a failure" test.
        """
        adapter = self._adapter()
        for error_text in (
            "CH_CLIENT_AUTH_FAILURE: clientId or clientSecret is incorrect",
            "CH_ACCOUNT_AUTH_FAILURE: account is not authorized",
            "invalid token",
        ):
            messages = self._messages(
                caplog, adapter, RuntimeError(error_text), error_text, error_text.lower()
            )
            assert any("authentication unavailable" in m for m in messages), (error_text, messages)
            assert not any("NO RESPONSE" in m for m in messages), (
                f"{error_text!r} was claimed as a response timeout"
            )

    def test_the_unclassified_fallback_no_longer_names_a_cause(self, caplog) -> None:
        """A fallback that guesses is worse than one that says it does not know.

        The old text was "cTrader connect/auth unavailable", and it fired exactly when the
        cause could not be classified - then named credentials anyway. The `error` field
        carries the real text, so nothing is lost by pointing at it.
        """
        adapter = self._adapter()
        messages = self._messages(
            caplog, adapter, RuntimeError("connection refused"),
            "connection refused", "connection refused",
        )
        assert messages, "nothing was logged at all"
        assert any("NOT CLASSIFIED" in m for m in messages), messages
        assert not any("auth" in m.lower() for m in messages), (
            f"the unclassified fallback still blames authentication: {messages}"
        )
        assert self._data(caplog, "NOT CLASSIFIED")["error"] == "connection refused"

    def test_the_string_fallback_is_narrow(self) -> None:
        """The string path is a *fallback*, so it must not carry a classification alone.

        It exists only for when Twisted cannot be imported. Each near-miss below is a shape
        that must fall through to another branch - most importantly a non-Twisted exception
        whose text merely resembles the shape, which must not be claimed.
        """
        from live_runner.ctrader_adapter import _classify_deferred_response_timeout as c

        # A non-Twisted exception with no matching text is not a response timeout.
        assert c(RuntimeError("x"), "boom") == (False, None)
        for text in (
            "", "Deferred", "(15, 'Deferred", "(abc, 'Deferred')", "(15, 'Other')",
            "timeout (15, 'Deferred')", "(15, 'Deferred') trailing",
            "CH_CLIENT_AUTH_FAILURE: clientId or clientSecret is incorrect",
            "(15, 'Deferred', 'extra')",
        ):
            matched, seconds = c(RuntimeError("x"), text)
            assert not matched or seconds is not None, (text, matched, seconds)
        # The strict half of the contract: no non-Twisted exception is ever claimed.
        assert c(RuntimeError("x"), "(15, 'Deferred')") == (True, 15)
        for text in ("(15, 'Deferred", "Deferred", "(15, 'Other')", "", "timeout (15, 'Deferred')"):
            assert c(RuntimeError("x"), text) == (False, None), (text, c(RuntimeError("x"), text))

    def test_all_four_branches_stay_distinguishable(self, caplog) -> None:
        """Four branches, four different messages.

        The response-timeout branch is new, so the number of things an operator must be able
        to tell apart went from three to four: bad credentials, a connect timeout, a
        response timeout, and a cause nobody could classify. If two ever collapse into the
        same text the classification is decoration again, which is the state this class
        exists to end.

        The pre-existing test asserted the *old* fallback text, so it is superseded here
        deliberately rather than quietly - that text was the defect.
        """
        import asyncio

        from twisted.internet import defer

        adapter = self._adapter()
        auth = self._messages(caplog, adapter, RuntimeError("x"), "401", "invalid token")
        timeout = self._messages(
            caplog, adapter, asyncio.TimeoutError(), "timed out", "no response"
        )
        response = self._messages(
            caplog, adapter, defer.TimeoutError(15, "Deferred"), "(15, 'Deferred')", "deferred"
        )
        generic = self._messages(
            caplog, adapter, RuntimeError("x"), "refused", "connection refused"
        )
        assert len({tuple(auth), tuple(timeout), tuple(response), tuple(generic)}) == 4, (
            "two branches produced the same message, so an operator cannot tell the four "
            "causes apart"
        )


class TestTheDegenerateDedupeKeyIsLoud:
    """The collision in item 1 cannot be fixed without a trading decision, but it can stop
    being silent.

    Two equal-sized fills on one order build the same key when the broker sends neither a deal
    id nor a timestamp, and the second is dropped as a "duplicate" that was not one - leaving
    the position short, with a log line arguing against that conclusion.

    Replacing the key is a behaviour change on the live fill path and was left to a human. What
    needed no decision is that the degenerate case should be *remarked*: the log is where the
    evidence for that decision comes from, exactly as the zero-decimal log was for item 6.
    """

    @staticmethod
    def _adapter():
        return CTraderLiveAdapter.__new__(CTraderLiveAdapter)

    def test_a_fill_with_no_deal_id_and_no_timestamp_warns(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            self._adapter()._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=123, timestamp_ms=0, filled_volume=500.0
            )
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("neither a deal id nor a timestamp" in m for m in messages), (
            "a fill that cannot be identified is passing unremarked; this is the one moment the "
            f"evidence for the item 1 decision can be gathered, and it logged {messages!r}"
        )

    def test_the_warning_names_the_order_and_volume_that_decide_the_key(self, caplog) -> None:
        """The two fields are the whole collision surface, so they must be in the log line."""
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            self._adapter()._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=77, timestamp_ms=0, filled_volume=12.5
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "77" in joined and "12.5" in joined, (
            f"the warning does not say which order and volume collide the key: {joined!r}"
        )

    def test_a_real_deal_id_is_not_warned_about(self, caplog) -> None:
        """The overwhelmingly common path must stay quiet, or the warning is worthless."""
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            key = self._adapter()._build_dedupe_key(
                SimpleNamespace(dealId=26533115), order_id=1, timestamp_ms=0, filled_volume=1.0
            )
        assert key == "ctrader:deal:26533115"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a fill carrying a real deal id warned; the key cannot collide when the broker "
            "supplies the one field that makes it unique"
        )

    def test_a_missing_deal_id_with_a_real_timestamp_is_not_warned_about(self, caplog) -> None:
        """No deal id alone does not collide.

        The key still carries a millisecond timestamp and a volume, so two distinct fills would
        have to agree to the millisecond *and* to the volume. Warning here would be crying wolf
        at a case the evidence says is fine.
        """
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            key = self._adapter()._build_dedupe_key(
                SimpleNamespace(dealId=0), order_id=9, timestamp_ms=1757000000123,
                filled_volume=3.0,
            )
        assert key == "ctrader:order:9:1757000000123:3.0"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "warned for a deal-id-less fill that still carries a millisecond timestamp; the "
            "collision needs *both* to be missing"
        )


class TestAnUnnamedStatusLeavesARecoverableFingerprint:
    """Item 2's hazard is real, but it is not the *undetectable* one that was recorded.

    The backlog held that an unrecognised order status is "silently dropped on parse" and that
    "the broker sent no status" and "the broker sent a status this client has no name for" are
    the same observation. Measured against the installed protos, that is not what protobuf
    does. An enum value with no name in this build is retained in the message's **unknown
    fields**, under this field's own number, with the original value intact. A status that was
    never sent leaves no unknown field at all.

    So the distinction the adapter needed was available all along and was simply not being
    asked for. These tests pin the fingerprint - including the protobuf behaviour it depends
    on, so an upgrade that changes it fails here rather than quietly turning detection back
    into a no-op - and pin that the cancel decision itself is unchanged.
    """

    @staticmethod
    def _real():
        return pytest.importorskip(
            "ctrader_open_api.messages.OpenApiModelMessages_pb2",
            reason="the installed protos are what make the fingerprint meaningful",
        )

    @staticmethod
    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b, n = n & 0x7F, n >> 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _order_with_wire_status(self, mod, wire_value: int):
        number = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"].number
        order = mod.ProtoOAOrder()
        order.ParseFromString(self._varint((number << 3) | 0) + self._varint(wire_value))
        return order

    @staticmethod
    def _adapter():
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        mod = pytest.importorskip("ctrader_open_api.messages.OpenApiModelMessages_pb2")
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus
        return adapter

    @pytest.mark.parametrize("wire_value", [6, 9, 255])
    def test_the_unnamed_status_is_recovered_from_the_unknown_fields(self, wire_value) -> None:
        mod = self._real()
        order = self._order_with_wire_status(mod, wire_value)
        assert self._adapter()._unrecognised_status_wire_value(order) == wire_value, (
            f"the broker sent status {wire_value}, which this client cannot name, and it is no "
            "longer recoverable from the message - so 'did the broker name a status we do not "
            "know' has become unanswerable again and the detection is dead code"
        )

    def test_an_absent_status_is_not_mistaken_for_an_unnamed_one(self) -> None:
        """The distinction only earns its keep if the two cases stay apart.

        Both read as ACCEPTED and both have `HasField` false. If a status that was never sent
        also produced a fingerprint, the detection would fire on every order whose status
        simply did not arrive, and the warning would be noise covering the real case.
        """
        mod = self._real()
        assert self._adapter()._unrecognised_status_wire_value(mod.ProtoOAOrder()) is None, (
            "an order whose status was never sent is being reported as carrying a status this "
            "client cannot name; the two are different situations and conflating them is the "
            "error this function exists to avoid"
        )

    @pytest.mark.parametrize("named", [1, 2, 3, 4, 5])
    def test_a_status_we_can_name_leaves_no_fingerprint(self, named) -> None:
        mod = self._real()
        order = mod.ProtoOAOrder()
        order.orderStatus = named
        assert self._adapter()._unrecognised_status_wire_value(order) is None, (
            f"status {named} is one this client can name, so it must not report a fingerprint; "
            "otherwise the warning fires on the overwhelmingly common path"
        )

    def test_an_object_without_the_fingerprint_carries_no_opinion(self) -> None:
        """Test doubles must not trip it, or the guard would be untestable and noisy.

        The existing characterisation tests build orders out of `SimpleNamespace`, which has
        neither `UnknownFields` nor `DESCRIPTOR`. Absence of the ability to ask is not evidence
        of an unnamed status, and must not be reported as one.
        """
        assert self._adapter()._unrecognised_status_wire_value(
            SimpleNamespace(orderStatus=1)
        ) is None
        assert self._adapter()._unrecognised_status_wire_value(SimpleNamespace()) is None

    def test_the_warning_names_the_status_nobody_could_interpret(self, caplog) -> None:
        """The number is the whole point: it is what a human needs to look the value up."""
        mod = self._real()
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            self._adapter()._is_working_order(self._order_with_wire_status(mod, 6))
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "6" in joined, f"the warning does not name the unrecognised status: {joined!r}"
        assert "no name for" in joined, (
            f"the warning does not say the status was unrecognisable: {joined!r}"
        )

    def test_the_cancel_decision_is_unchanged_by_the_detection(self) -> None:
        """Instrumentation only. Which way this should resolve stays a human decision.

        The detection exists so that the case is *visible*. It deliberately does not change
        whether the order is cancelled, because that is the question of which failure is
        cheaper for live trading, and answering it is not a test's job.
        """
        mod = self._real()
        adapter = self._adapter()
        assert adapter._is_working_order(self._order_with_wire_status(mod, 6)) is True, (
            "detecting an unnamed status now changes whether the order is cancelled; that is a "
            "policy change and needs its own decision, not a side effect of the instrumentation"
        )

    def test_the_protobuf_behaviour_this_depends_on_is_still_true(self) -> None:
        """Name the exact protobuf guarantee, so an upgrade that changes it fails here.

        Detection is only as good as this: that an unrecognised enum survives in the unknown
        fields under its own field number and wire type. If protobuf ever starts discarding it,
        this helper silently becomes a no-op and the warning stops firing - which is precisely
        the failure mode this test exists to prevent.
        """
        mod = self._real()
        field = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"]
        order = self._order_with_wire_status(mod, 6)
        unknown = list(order.UnknownFields())
        assert len(unknown) == 1, (
            f"expected exactly one unknown field, got {unknown!r}; protobuf has changed how it "
            "retains an unrecognised enum, so re-measure whether detection still works"
        )
        assert unknown[0].field_number == field.number, (
            "the retained unknown field is no longer under orderStatus's own field number"
        )
        assert unknown[0].wire_type == 0, (
            "the retained unknown field is no longer a varint; wire type 0 is what an enum "
            "arrives as, and the detection keys on it"
        )
        assert int(unknown[0].data) == 6


class TestAnExplicitlySentZeroPriceIsDistinguishable:
    """Item 10's remaining ambiguity did not exist.

    The backlog recorded that the decision about a zero execution price "needs the broker's
    behaviour, which is not knowable from here", and gave a reason: `executionPrice` is a protobuf
    double *with presence* whose unset value is `0.0`, "so an omitted price and a genuine zero
    arrive identically".

    Those two clauses contradict each other, and `optional double` settles it — the field really
    does carry presence:

        unset        -> HasField=False, value 0.0
        set 0.0      -> HasField=True,  value 0.0

    So the two cases *are* separable, and the blocker was an assumption rather than a limit. This
    detects which one occurred and logs it. **The returned value is unchanged** — whether such a
    row should be refused, flagged or corrected is still a trading decision, and one test pins that
    the detection has not quietly become one.
    """

    @staticmethod
    def _protos():
        return pytest.importorskip(
            "ctrader_open_api.messages.OpenApiModelMessages_pb2",
            reason="the installed protos are what make presence meaningful",
        )

    @staticmethod
    def _deal(explicit_zero: bool):
        mod = TestAnExplicitlySentZeroPriceIsDistinguishable._protos()
        deal = mod.ProtoOADeal()
        deal.dealId = 7
        if explicit_zero:
            deal.executionPrice = 0.0
        return deal

    def test_the_presence_that_separates_the_cases_is_what_the_protos_actually_have(self) -> None:
        """Name the guarantee, so a proto build that drops `optional` fails here.

        Without presence, an omitted price and a sent zero are the same number and the detection
        becomes a no-op - which is exactly the state the backlog believed was already true.
        """
        mod = self._protos()
        field = mod.ProtoOADeal.DESCRIPTOR.fields_by_name["executionPrice"]
        assert field.has_presence, (
            "executionPrice no longer carries presence, so an omitted price and a sent zero are "
            "again indistinguishable and this class's premise is gone - re-measure before trusting "
            "the warning"
        )
        assert self._deal(explicit_zero=True).HasField("executionPrice") is True
        assert self._deal(explicit_zero=False).HasField("executionPrice") is False

    def test_a_deal_that_sent_zero_is_reported_as_having_spoken(self) -> None:
        order = self._protos().ProtoOAOrder()
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(order, self._deal(True)) == ["deal"]

    def test_a_deal_that_sent_nothing_is_not(self) -> None:
        order = self._protos().ProtoOAOrder()
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(order, self._deal(False)) == []

    def test_a_real_price_is_never_reported(self) -> None:
        mod = self._protos()
        deal = mod.ProtoOADeal()
        deal.executionPrice = 261.7
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(mod.ProtoOAOrder(), deal) == []

    def test_the_warning_names_which_source_sent_the_zero(self, caplog) -> None:
        """A reader needs to know it was the deal and not the order to act on it."""
        mod = self._protos()
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            CTraderLiveAdapter._resolve_execution_price(
                order=mod.ProtoOAOrder(), deal=self._deal(True)
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "on the deal" in joined, f"the warning does not name the source: {joined!r}"
        assert "actual_price=0" in joined, (
            f"the warning does not say what the stored row will look like: {joined!r}"
        )

    def test_an_omitted_price_stays_silent(self, caplog) -> None:
        """The warning must stay rare enough to be read.

        A deal that sent no price at all is the pre-existing, already-characterised path. It is
        still `0.0` and still an open question, but warning on it here would drown the one case
        that looks like a real price and is not.
        """
        mod = self._protos()
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            CTraderLiveAdapter._resolve_execution_price(
                order=mod.ProtoOAOrder(), deal=self._deal(False)
            )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "an omitted price warned; only an explicitly-sent zero is the surprising case"
        )

    def test_a_resolvable_price_does_not_warn_even_when_the_deel_said_zero(self, caplog) -> None:
        """The order-level fallback still wins, so the row is not poisoned and there is nothing
        to report."""
        mod = self._protos()
        order = mod.ProtoOAOrder()
        order.executionPrice = 261.7
        with caplog.at_level(logging.WARNING, logger="live-runner"):
            price = CTraderLiveAdapter._resolve_execution_price(order=order, deal=self._deal(True))
        assert price == pytest.approx(261.7)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "warned even though the order-level fallback produced a real price, so the row is fine"
        )

    def test_the_returned_value_is_unchanged_by_the_detection(self) -> None:
        """Instrumentation only. The policy stays a human decision.

        Both cases still resolve to 0.0 exactly as before. What changed is that the log can now say
        which of the two produced it.
        """
        mod = self._protos()
        for deal in (self._deal(True), self._deal(False)):
            assert CTraderLiveAdapter._resolve_execution_price(
                order=mod.ProtoOAOrder(), deal=deal
            ) == 0.0

    def test_a_non_proto_object_carries_no_opinion(self) -> None:
        """The `SimpleNamespace` doubles used throughout these tests must not trip it.

        Absence of the ability to ask is not evidence of a sent zero.
        """
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(
            None, SimpleNamespace(executionPrice=0.0)
        ) == []
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(None, None) == []


class TestTheZeroDetectionsDoNotOverreach:
    """Both detections key on something specific, and each over-reach survived the sweep.

    The mutation sweep found that loosening either check broke nothing. Both are narrow on
    purpose - `_explicitly_sent_zero_prices` reports a *zero*, and
    `_unrecognised_status_wire_value` reports an *enum* - so a looser check would name the wrong
    thing in a log an operator is being asked to act on.
    """

    @staticmethod
    def _protos():
        return pytest.importorskip("ctrader_open_api.messages.OpenApiModelMessages_pb2")

    @staticmethod
    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b, n = n & 0x7F, n >> 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def test_an_explicitly_sent_negative_price_is_not_reported_as_a_sent_zero(self) -> None:
        """A negative price is item 10's *other* half, not this one.

        `-5.0` sent explicitly is a different problem from a zero sent explicitly: the zero might
        be a broker convention, while a negative is not a price any broker reports for a fill. The
        log names the situation, so it must not conflate them.
        """
        mod = self._protos()
        deal = mod.ProtoOADeal()
        deal.executionPrice = -5.0
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(mod.ProtoOAOrder(), deal) == [], (
            "an explicitly-sent -5.0 is being reported as a sent zero; the log says 'exactly 0', "
            "so a negative must not appear under that description"
        )

    def test_an_explicitly_sent_nan_price_is_not_reported_either(self) -> None:
        mod = self._protos()
        deal = mod.ProtoOADeal()
        deal.executionPrice = float("nan")
        assert CTraderLiveAdapter._explicitly_sent_zero_prices(mod.ProtoOAOrder(), deal) == []

    def test_an_unknown_field_of_another_wire_type_is_not_an_unnamed_status(self) -> None:
        """The status fingerprint must be an enum, and an enum arrives as a varint.

        Dropping the `wire_type == 0` check survived the sweep, because no message in the test
        suite carries an unknown field of any other kind. A length-delimited unknown field under
        `orderStatus`'s own number would then be reported as an unnameable status - a fabrication,
        and one an operator would go and look up.
        """
        mod = self._protos()
        number = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"].number
        order = mod.ProtoOAOrder()
        # Wire type 2 (length-delimited) at orderStatus's own field number, with a payload.
        order.ParseFromString(self._varint((number << 3) | 2) + self._varint(3) + b"abc")
        assert CTraderLiveAdapter._unrecognised_status_wire_value(order) is None, (
            "a length-delimited unknown field under orderStatus's number is being reported as an "
            "unrecognised enum; only a varint can be one"
        )

    def test_a_genuine_unnamed_status_is_still_detected_after_that(self) -> None:
        """The over-reach guard must not have cost the real detection."""
        mod = self._protos()
        number = mod.ProtoOAOrder.DESCRIPTOR.fields_by_name["orderStatus"].number
        order = mod.ProtoOAOrder()
        order.ParseFromString(self._varint((number << 3) | 0) + self._varint(6))
        assert CTraderLiveAdapter._unrecognised_status_wire_value(order) == 6

    def test_an_unknown_field_under_a_different_number_is_ignored(self) -> None:
        """The fingerprint keys on *this* field's number, not on any unknown field."""
        mod = self._protos()
        order = mod.ProtoOAOrder()
        order.ParseFromString(self._varint((999 << 3) | 0) + self._varint(6))
        assert CTraderLiveAdapter._unrecognised_status_wire_value(order) is None


class TestTheMoneyDigitsHelpersAreActuallyCallableAsProductionCallsThem:
    """A regression test for a live-money bug this batch introduced and the suite could not see.

    `be97e67` inserted `_honour_zero_money_digits` *between* the `@staticmethod` that belonged to
    `_resolve_money_digits` and the `def` it decorated. `_resolve_money_digits` was left as a plain
    function, so the production call

        self._resolve_money_digits(trader, context="account refresh")

    passed `self` as `trader` and raised `TypeError` on the live balance-refresh path.

    **Nothing caught it**, for two reasons worth keeping in mind:

    * every test reached the helper through the class (`CTraderLiveAdapter._resolve_money_digits`),
      which still works for an unbound plain function; and
    * both sync guards compare the *body* between `raw = getattr(...)` and `return int(raw)` - and a
      decorator sits above that slice, so the signature and the body were both unchanged.

    Hence the structural assertion below, and hence the instance-call test. A text comparison of a
    function cannot see whether it is still a staticmethod.
    """

    @pytest.mark.parametrize(
        "name", ["_resolve_money_digits", "_honour_zero_money_digits"]
    )
    def test_both_helpers_are_still_static_methods(self, name) -> None:
        entry = CTraderLiveAdapter.__dict__[name]
        assert isinstance(entry, staticmethod), (
            f"CTraderLiveAdapter.{name} is a {type(entry).__name__}, not a staticmethod. Without "
            "the decorator, `self.<helper>(...)` binds self to the first parameter and raises "
            "TypeError. A decorator lives *above* any body-slice comparison, so the sync tests "
            "cannot see this - only this assertion can."
        )

    def test_the_instance_call_shape_production_uses_actually_works(self) -> None:
        """`get_balance` calls it through `self`; that is the shape that was broken."""
        adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
        resolved = adapter._resolve_money_digits(
            SimpleNamespace(moneyDigits=2), context="account refresh"
        )
        assert resolved == 2, f"the instance call returned {resolved!r}"

    def test_the_account_discovery_copy_is_still_a_plain_module_function(self) -> None:
        """The other half of the copy: called as `_resolve_money_digits(...)`, not `self.`."""
        from live_runner import account_discovery

        entry = account_discovery.__dict__["_resolve_money_digits"]
        assert isinstance(entry, types.FunctionType), (
            f"account_discovery._resolve_money_digits is a {type(entry).__name__}; it is called "
            "as a module-level function at account_discovery.py:368, so it must stay one"
        )


class TestTheSweepHandlerConvertsAuthTerminalApiErrorsItself:
    """Defensive depth on a money path, and the reason one mutation survived.

    `_cancel_order_id` already calls `_raise_terminal_auth_from_api_error(exc)` before re-raising,
    so in production an auth-terminal `CTraderApiError` has become a `CTraderAuthTerminalError` -
    a `RuntimeError` - before the sweep's per-order handler could see it. The handler calls the
    converter again anyway, and that call is redundant on that path.

    It is not redundant on every path. Anything that reaches the handler without going through
    `_cancel_order_id`'s own conversion - a subclass override, a test double, a future refactor
    that inlines the request - would deliver a plain `CTraderApiError` carrying an auth-terminal
    code, and without the second call it would be logged and the sweep would carry on against a
    connection the broker has already rejected.

    Removing the call survived the suite for exactly that reason: every test stubs
    `_cancel_order_id`, so the inner conversion never ran. This test closes it.
    """

    def test_a_plain_api_error_with_an_auth_terminal_code_still_ends_the_sweep(self) -> None:
        from live_runner.ctrader_adapter import CTraderApiError

        class _Adapter:
            def __init__(self):
                self.attempted = []
                self._terminal = None

            def _raise_terminal_auth_from_api_error(self, exc):
                from live_runner.ctrader_adapter import CTraderLiveAdapter

                CTraderLiveAdapter._raise_terminal_auth_from_api_error(self, exc)

            def _mark_terminal_auth_failure(self, reason, *, error_code=None):
                self._terminal = (reason, error_code)

            @staticmethod
            def _is_working_order(order):
                """Minimal stand-in: this test is about the handler, not the status filter."""
                return True

        mod = pytest.importorskip(
            "ctrader_open_api.messages.OpenApiModelMessages_pb2",
            reason="the installed protos are what make a real working order",
        )
        adapter = _Adapter()
        orders = []
        for order_id in (11, 22):
            order = mod.ProtoOAOrder()
            order.orderId = order_id
            order.orderType = int(mod.ProtoOAOrderType.LIMIT)
            order.orderStatus = int(mod.ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)
            orders.append(order)

        async def _reconcile():
            return SimpleNamespace(order=orders)

        async def _cancel(order_id: int) -> None:
            adapter.attempted.append(order_id)
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "token invalid")

        adapter._reconcile_state = _reconcile
        adapter._cancel_order_id = _cancel
        adapter._ProtoOAOrderStatus = mod.ProtoOAOrderStatus
        adapter._ProtoOAOrderType = mod.ProtoOAOrderType

        with pytest.raises(CTraderAuthTerminalError):
            asyncio.run(CTraderLiveAdapter.cancel_open_orders(adapter))
        assert adapter.attempted == [11], (
            f"the sweep continued past an auth-terminal API error: {adapter.attempted}"
        )
