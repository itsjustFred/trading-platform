"""#125 review follow-up: the entry-deviation gate must not judge a stale quote,
and its per-deployment settings are bounded.

Review finding (confirmed): ``_check_entry_deviation`` never looked at
``LiveQuote.timestamp``. Alpaca's latest IEX quote can be hours old, so a
Discord buy at 21:00 ET compared against the 16:00 close passed the gate and
the DAY market bracket filled at the next open after a gap.

Decisions pinned here:

* The quote must carry a broker timestamp. Missing -> ``quote_time_missing``;
  not a timezone-aware datetime -> ``quote_time_invalid``; more than the
  clock-skew allowance in the future -> ``quote_time_in_future``; older than
  the deployment's ``max_entry_quote_age_seconds`` (default 10 s, > 0, never
  from the signal) -> ``quote_stale``. Freshness is checked before any price
  rule: a stale quote says nothing about where the market is.
* The quote age is recorded with the other gate fields.
* ``max_entry_deviation_sl_fraction`` must be within ``[0, 1]`` (``25`` meant as
  25 % would otherwise disable the limit). Both settings are validated when a
  deployment is created and again at dispatch.
* Alpaca returns the quote's own timestamp (never now()); cTrader returns the
  spot event time, else the time the event was received (and says which).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import BaseLiveAdapter, LiveQuote
from shared.models import LiveDeploymentConfig, LiveSignal

#: 21:00 ET on a trading day: the review's after-hours Discord buy.
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)

_BUY = {"stop_loss": 96.0, "take_profit": 112.0}


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    # raising=False: before the fix the hook does not exist and the gate ignores
    # the quote time entirely, which is exactly what these tests catch.
    monkeypatch.setattr(daemon, "_utc_now", lambda: NOW, raising=False)


def _quote(*, age: float | None = 1.0, bid: float = 99.99, ask: float = 100.0, **kwargs):
    timestamp = kwargs.pop("timestamp", None)
    if timestamp is None and age is not None:
        timestamp = NOW - timedelta(seconds=age)
    return LiveQuote(bid=bid, ask=ask, timestamp=timestamp, **kwargs)


@dataclass
class _Adapter(BaseLiveAdapter):
    quote: Any = None
    quote_calls: list[str] = field(default_factory=list)
    submitted: list[LiveSignal] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append(signal)
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_pip_value(self, symbol):
        # #146: a USD-quoted share in a USD account moves 1.0 per 1.0.
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=1.0, units_per_qty=1.0, conversion_rate=1.0,
            quote_asset="USD", account_asset="USD", source="test",
        )

    async def get_quote(self, symbol: str):
        self.quote_calls.append(symbol)
        return self.quote


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={"max_notional_per_trade": 1.0, **settings},
    )


def _signal(deployment: LiveDeploymentConfig, metadata: dict | None = None) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=NOW,
        metadata=dict(_BUY if metadata is None else metadata),
    )


def _prepare(adapter, deployment, signal):
    return asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))


def _assert_rejected(adapter, deployment, signal, rule: str) -> None:
    with pytest.raises(ValueError, match=f"entry deviation gate.*{rule}"):
        _prepare(adapter, deployment, signal)
    assert adapter.submitted == []


# ── quote freshness ───────────────────────────────────────────────────────────


def test_after_hours_quote_from_the_close_is_rejected_as_stale():
    """The review scenario: 16:00 ET close quote, signal at 21:00 ET."""
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=5 * 3600.0))

    _assert_rejected(adapter, deployment, _signal(deployment), "quote_stale")


def test_quote_without_a_timestamp_is_rejected():
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=None))

    _assert_rejected(adapter, deployment, _signal(deployment), "quote_time_missing")


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 11, 10, 1, 59, 59),  # naive: no zone, cannot be compared safely
        "2026-11-10T01:59:59+00:00",  # not a datetime
        1_762_740_000_000,  # raw epoch number
    ],
)
def test_quote_with_an_unusable_timestamp_is_rejected(timestamp):
    deployment = _deployment()
    adapter = _Adapter(quote=LiveQuote(bid=99.99, ask=100.0, timestamp=timestamp))  # type: ignore[arg-type]

    _assert_rejected(adapter, deployment, _signal(deployment), "quote_time_invalid")


@pytest.mark.parametrize(
    ("seconds_ahead", "rule"),
    [
        (1.0, None),
        (2.0, None),  # the clock-skew allowance is inclusive
        (2.01, "quote_time_in_future"),
        (3600.0, "quote_time_in_future"),
    ],
)
def test_quote_from_the_future_is_rejected_beyond_clock_skew(seconds_ahead, rule):
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=-seconds_ahead))
    signal = _signal(deployment)

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["entry_quote_age_seconds"] == pytest.approx(-seconds_ahead)
    else:
        _assert_rejected(adapter, deployment, signal, rule)


@pytest.mark.parametrize(
    ("age", "rule"),
    [
        (0.0, None),
        (9.99, None),
        (10.0, None),  # "older than" the limit rejects; exactly at it does not
        (10.01, "quote_stale"),
        (60.0, "quote_stale"),
    ],
)
def test_default_max_quote_age_is_ten_seconds(age, rule):
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=age))
    signal = _signal(deployment)

    if rule is None:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["max_entry_quote_age_seconds"] == pytest.approx(10.0)
    else:
        _assert_rejected(adapter, deployment, signal, rule)


@pytest.mark.parametrize(("age", "accepted"), [(59.0, True), (61.0, False)])
def test_max_quote_age_is_configurable_per_deployment(age, accepted):
    deployment = _deployment(max_entry_quote_age_seconds=60)
    adapter = _Adapter(quote=_quote(age=age))
    signal = _signal(deployment)

    if accepted:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["max_entry_quote_age_seconds"] == pytest.approx(60.0)
    else:
        _assert_rejected(adapter, deployment, signal, "quote_stale")


def test_max_quote_age_is_never_read_from_signal_metadata():
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=30.0))
    signal = _signal(deployment, {**_BUY, "max_entry_quote_age_seconds": 100_000})

    _assert_rejected(adapter, deployment, signal, "quote_stale")


@pytest.mark.parametrize(
    "quote",
    [
        # At the entry: no price rule would fire.
        _quote(age=30.0, bid=99.99, ask=100.0),
        # Through the stop and far beyond the deviation limit: the stale quote
        # is still named as the reason, because its price says nothing.
        _quote(age=30.0, bid=94.9, ask=95.0),
        _quote(age=30.0, bid=129.9, ask=130.0),
    ],
)
def test_freshness_is_checked_before_any_price_rule(quote):
    deployment = _deployment()
    adapter = _Adapter(quote=quote)

    _assert_rejected(adapter, deployment, _signal(deployment), "quote_stale")


def test_accepted_entry_records_quote_age_and_time_source():
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=3.0, timestamp_source="broker"))

    prepared, _ = _prepare(adapter, deployment, _signal(deployment))

    metadata = prepared.metadata
    assert metadata["entry_quote_age_seconds"] == pytest.approx(3.0)
    assert metadata["max_entry_quote_age_seconds"] == pytest.approx(10.0)
    assert metadata["entry_quote_time"] == (NOW - timedelta(seconds=3)).isoformat()
    assert metadata["entry_quote_time_source"] == "broker"
    assert metadata["entry_gate_rejection_rule"] is None


def test_payload_cannot_spoof_the_quote_age():
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=3.0))
    signal = _signal(
        deployment,
        {
            **_BUY,
            "entry_quote_age_seconds": 0.0,
            "max_entry_quote_age_seconds": 9999.0,
            "entry_quote_time_source": "publisher",
        },
    )

    prepared, _ = _prepare(adapter, deployment, signal)

    assert prepared.metadata["entry_quote_age_seconds"] == pytest.approx(3.0)
    assert prepared.metadata["max_entry_quote_age_seconds"] == pytest.approx(10.0)
    assert prepared.metadata["entry_quote_time_source"] == "broker"


def test_stale_rejection_is_logged_with_age_and_rule(caplog):
    deployment = _deployment()
    adapter = _Adapter(quote=_quote(age=42.0))

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        _assert_rejected(adapter, deployment, _signal(deployment), "quote_stale")

    records = [r for r in caplog.records if "entry deviation" in r.getMessage().lower()]
    assert records, "the stale-quote rejection was not logged"
    data = getattr(records[-1], "data", {})
    assert data["entry_gate_rejection_rule"] == "quote_stale"
    assert data["entry_quote_age_seconds"] == pytest.approx(42.0)
    assert data["max_entry_quote_age_seconds"] == pytest.approx(10.0)


def test_exit_only_orders_still_skip_the_gate():
    @dataclass
    class _Book(_Adapter):
        async def get_positions(self) -> list[dict[str, Any]]:
            return [{"symbol": "AAPL", "qty": 4.0}]

    deployment = _deployment()
    adapter = _Book(quote=_quote(age=None))
    signal = _signal(deployment, {"exit_only": True}).model_copy(update={"action": "sell"})

    _, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(4.0)
    assert adapter.quote_calls == []


# ── settings bounds at dispatch ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(0.0, True), (0.25, True), (1.0, True), (1.0001, False), (25, False), (100.0, False)],
)
def test_deviation_fraction_must_be_at_most_one(value, accepted):
    deployment = _deployment(max_entry_deviation_sl_fraction=value)
    adapter = _Adapter(quote=_quote())
    signal = _signal(deployment)

    if accepted:
        prepared, _ = _prepare(adapter, deployment, signal)
        assert prepared.metadata["max_entry_deviation_sl_fraction"] == pytest.approx(value)
    else:
        with pytest.raises(ValueError, match="max_entry_deviation_sl_fraction"):
            _prepare(adapter, deployment, signal)
        assert adapter.submitted == []
        assert adapter.quote_calls == [], "invalid config must refuse before the quote"


@pytest.mark.parametrize("bad", [0, 0.0, -1.0, float("nan"), float("inf"), "abc", True, None])
def test_invalid_max_quote_age_is_rejected_at_dispatch(bad):
    deployment = _deployment(max_entry_quote_age_seconds=bad)
    adapter = _Adapter(quote=_quote())

    with pytest.raises(ValueError, match="max_entry_quote_age_seconds"):
        _prepare(adapter, deployment, _signal(deployment))

    assert adapter.submitted == []
    assert adapter.quote_calls == []


# ── settings bounds at deployment creation ────────────────────────────────────


@pytest.mark.parametrize(
    "settings",
    [
        {"max_entry_deviation_sl_fraction": 25},
        {"max_entry_deviation_sl_fraction": -0.1},
        {"max_entry_deviation_sl_fraction": "abc"},
        {"max_entry_deviation_sl_fraction": None},
        {"max_entry_quote_age_seconds": 0},
        {"max_entry_quote_age_seconds": -5},
        {"max_entry_quote_age_seconds": float("inf")},
        {"max_entry_quote_age_seconds": True},
    ],
)
def test_entry_gate_settings_validator_rejects_invalid_values(settings):
    from risk.live import entry_gate_settings

    with pytest.raises(ValueError, match=next(iter(settings))):
        entry_gate_settings(settings)


def test_entry_gate_settings_validator_defaults():
    from risk.live import entry_gate_settings

    resolved = entry_gate_settings({})

    assert resolved.max_deviation_sl_fraction == pytest.approx(0.25)
    assert resolved.max_quote_age_seconds == pytest.approx(10.0)


def _seed_account(database, account_id: str) -> None:
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id=account_id, broker="alpaca", name="paper", is_demo=True
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "settings",
    [
        {"max_entry_deviation_sl_fraction": 25},
        {"max_entry_quote_age_seconds": 0},
    ],
)
def test_create_live_deployment_rejects_invalid_gate_settings(isolated_db_sandbox, settings):
    database, _, _ = isolated_db_sandbox
    _seed_account(database, "acc-gate")
    deployment = _deployment(**settings).model_copy(update={"account_id": "acc-gate"})

    with pytest.raises(ValueError, match=next(iter(settings))):
        database.create_live_deployment(deployment)

    assert database.list_live_deployments() == []


def test_create_live_deployment_accepts_valid_gate_settings(isolated_db_sandbox):
    database, _, _ = isolated_db_sandbox
    _seed_account(database, "acc-gate")
    deployment = _deployment(
        max_entry_deviation_sl_fraction=0.5, max_entry_quote_age_seconds=30
    ).model_copy(update={"account_id": "acc-gate"})

    created = database.create_live_deployment(deployment)

    assert created.settings["max_entry_deviation_sl_fraction"] == 0.5


def _api_client(monkeypatch, created: list):
    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module

    monkeypatch.setattr(viewer_main_module, "_seed_default_live_accounts", lambda: None)
    monkeypatch.setattr(
        viewer_main_module,
        "_ensure_live_account_for_deployment",
        lambda account_id: {
            "id": account_id,
            "broker": "alpaca",
            "name": "Alpaca Paper",
            "broker_login": "PA0000000001",
        },
    )

    def _create(deployment):
        created.append(deployment)
        return deployment

    monkeypatch.setattr(viewer_main_module, "create_live_deployment", _create)
    return TestClient(viewer_main_module.app)


def _api_payload(settings: dict) -> dict:
    return {
        "strategy_name": "volatility_breakout",
        "account_id": "acc-gate",
        "symbols": ["AAPL"],
        "timeframe": "1h",
        "signal_source_type": "internal",
        "signal_source": "internal",
        "settings": settings,
    }


@pytest.mark.parametrize(
    "settings",
    [{"max_entry_deviation_sl_fraction": 25}, {"max_entry_quote_age_seconds": -1}],
)
def test_api_refuses_invalid_gate_settings_before_storing(monkeypatch, settings):
    created: list = []
    client = _api_client(monkeypatch, created)

    response = client.post("/api/live/deployments", json=_api_payload(settings))

    assert response.status_code == 400
    assert next(iter(settings)) in response.text
    assert created == []


def test_api_accepts_valid_gate_settings(monkeypatch):
    created: list = []
    client = _api_client(monkeypatch, created)

    response = client.post(
        "/api/live/deployments",
        json=_api_payload(
            {"max_entry_deviation_sl_fraction": 0.3, "max_entry_quote_age_seconds": 15}
        ),
    )

    assert response.status_code == 201, response.text
    assert len(created) == 1


# ── adapter quote timestamps ──────────────────────────────────────────────────


def test_alpaca_quote_without_a_timestamp_is_not_stamped_with_now():
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Client:
        def get_stock_latest_quote(self, request):
            return {"AAPL": SimpleNamespace(bid_price=99.9, ask_price=100.0, timestamp=None)}

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = _Client()

    quote = asyncio.run(adapter.get_quote("AAPL"))

    assert quote.timestamp is None
    assert quote.timestamp_source == "broker"


def test_alpaca_quote_keeps_its_own_timestamp():
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    stamp = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)

    class _Client:
        def get_stock_latest_quote(self, request):
            return {"AAPL": SimpleNamespace(bid_price=99.9, ask_price=100.0, timestamp=stamp)}

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = _Client()

    quote = asyncio.run(adapter.get_quote("AAPL"))

    assert quote.timestamp == stamp
    assert quote.timestamp_source == "broker"


def _ctrader_with_waiter():
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )
    return module, adapter


def test_ctrader_quote_uses_the_spot_event_time_when_sent(monkeypatch):
    module, adapter = _ctrader_with_waiter()
    monkeypatch.setattr(
        module, "_utc_now", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc), raising=False
    )

    async def _run():
        waiter = module._SpotWaiter(future=asyncio.get_running_loop().create_future())
        adapter._spot_waiters[1] = waiter
        adapter._handle_spot_event(
            SimpleNamespace(symbolId=1, bid=100_000, ask=100_010, timestamp=1_762_740_000_000)
        )
        return await waiter.future

    quote = asyncio.run(_run())

    assert quote.timestamp == datetime.fromtimestamp(1_762_740_000, tz=timezone.utc)
    assert quote.timestamp_source == "broker"


def test_ctrader_quote_falls_back_to_the_receive_time(monkeypatch):
    module, adapter = _ctrader_with_waiter()
    received = datetime(2026, 11, 10, 1, 59, 58, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "_utc_now", lambda: received, raising=False)

    async def _run():
        waiter = module._SpotWaiter(future=asyncio.get_running_loop().create_future())
        adapter._spot_waiters[1] = waiter
        adapter._handle_spot_event(SimpleNamespace(symbolId=1, bid=100_000))
        adapter._handle_spot_event(SimpleNamespace(symbolId=1, ask=100_010))
        return await waiter.future

    quote = asyncio.run(_run())

    assert quote.timestamp == received
    assert quote.timestamp_source == "receive"
