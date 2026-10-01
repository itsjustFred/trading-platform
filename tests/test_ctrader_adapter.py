import asyncio
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
        assert any("connect/auth unavailable" in m for m in generic_messages), generic_messages
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

    def test_the_twisted_reactor_validation_is_left_unmeasured_on_purpose(self) -> None:
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
    """


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

    def test_a_negative_price_is_not_refused(self) -> None:
        """The `if value:` check is truthiness, not positivity.

        A negative price is truthy, so it is returned as an execution price. This is
        pinned so the truthiness check cannot be mistaken for a positivity check.
        """
        adapter = self._adapter()
        assert adapter._resolve_execution_price(
            order=None, deal=SimpleNamespace(executionPrice=-1.5)
        ) == -1.5

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
