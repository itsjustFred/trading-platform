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
