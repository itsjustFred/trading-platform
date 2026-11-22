from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_smoke
from scripts.live_smoke import (
    SmokeCheck,
    SmokeReport,
    _ensure_ctrader_smoke_deployment,
    _position_qty_for_symbol,
    main,
    run_smoke,
)


def test_alpaca_crypto_roundtrip_awaits_every_account_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three-iteration crypto roundtrip must await each iteration.

    A PERF401 rewrite once put the ``await`` inside the generator expression
    handed to ``list.extend``: the genexp became an async generator, so
    ``extend`` raised ``TypeError: 'async_generator' object is not iterable``
    and the crypto roundtrip never ran on any of the three accounts.
    """
    seen: list[int] = []

    async def _fake_iteration(
        env: dict[str, str],
        *,
        index: int,
        symbol: str,
        notional: float,
        timeout_seconds: int,
    ) -> SmokeCheck:
        seen.append(index)
        return SmokeCheck(name=f"crypto-acc{index}", ok=True, details={"index": index})

    monkeypatch.setattr(live_smoke, "_run_alpaca_crypto_iteration", _fake_iteration)

    checks = asyncio.run(
        live_smoke._alpaca_crypto_roundtrip(
            {},
            symbol="BTC/USD",
            notional=25.0,
            timeout_seconds=5,
        )
    )

    assert seen == [1, 2, 3]
    assert [check.name for check in checks] == ["crypto-acc1", "crypto-acc2", "crypto-acc3"]


def test_position_qty_for_symbol_matches_crypto_slash_alias() -> None:
    positions = [
        SimpleNamespace(symbol="ETHUSD", qty="0.5"),
        SimpleNamespace(symbol="BTCUSD", qty="0.000153092"),
    ]

    assert _position_qty_for_symbol(positions, "BTC/USD") == 0.000153092


def test_smoke_report_serializes_without_secret_fields() -> None:
    report = SmokeReport(
        mode="account-check",
        ok=True,
        started_at="2026-06-22T00:00:00+00:00",
        finished_at="2026-06-22T00:00:01+00:00",
        checks=[
            SmokeCheck(
                name="alpaca-paper-account",
                ok=True,
                details={"paper": True, "equity": 100000.0, "positions": 0},
            )
        ],
    )

    payload = asdict(report)

    assert payload["ok"] is True
    assert "api_key" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


def test_live_smoke_help_exposes_ctrader_broker_minimum_default(capsys) -> None:
    try:
        main(["--help"])
    except SystemExit:
        pass

    output = capsys.readouterr().out
    assert "--ctrader-volume" in output
    assert "100000" in output
    assert "alpaca" in output
    assert "ctrader" in output


def test_live_smoke_accepts_broker_mode_aliases(tmp_path: Path, monkeypatch) -> None:
    seen_modes: list[str] = []

    async def fake_run_smoke(**kwargs):
        seen_modes.append(kwargs["mode"])
        return SmokeReport(
            mode=kwargs["mode"],
            ok=True,
            started_at="2026-07-13T00:00:00+00:00",
            finished_at="2026-07-13T00:00:01+00:00",
            checks=[SmokeCheck(name="alias", ok=True)],
        )

    monkeypatch.setattr("scripts.live_smoke.run_smoke", fake_run_smoke)

    assert main(["--mode", "alpaca", "--reports-dir", str(tmp_path)]) == 0
    assert main(["--mode", "ctrader", "--reports-dir", str(tmp_path)]) == 0
    assert seen_modes == ["alpaca-crypto-roundtrip", "ctrader-demo-roundtrip"]


def _account(database, account_id: str) -> None:
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(id=account_id, broker="ctrader", name=account_id)
        )
        session.commit()


def _smoke_rows(database) -> list:
    from sqlmodel import select

    with database.get_session() as session:
        return list(session.exec(select(database.LiveDeploymentModel)))


def test_the_smoke_deployment_is_paused_and_namespaced(isolated_db) -> None:
    """The parent row must never be tradable, and must carry the smoke convention's label.

    PAUSED is the whole reason a synthetic row is safe: a runner that picked this up would trade a
    fabricated signal. The label lives in `strategy_name` because the primary key is a UUID.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    deployment_id = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    # The key is a UUID, not the descriptive label: LiveDeploymentConfig.id is typed uuid.UUID, and
    # a descriptive primary key made the daemon's whole deployment sync fail validation.
    uuid.UUID(deployment_id)
    rows = _smoke_rows(database)
    assert len(rows) == 1, rows
    assert rows[0].strategy_name == "ctrader-demo-smoke-44695443"
    assert rows[0].status == "PAUSED", (
        "an ACTIVE synthetic deployment is tradable, which is exactly what the PAUSED convention "
        "exists to prevent"
    )


def test_the_smoke_deployment_links_to_its_account(isolated_db) -> None:
    """`account_id` must point at the real account row, not a blank or invented one.

    `live_deployments.account_id` carries a foreign key, so blanking it either breaks the insert or
    leaves a row nobody can attribute to an account - the orphan class this work removes.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    rows = _smoke_rows(database)
    assert [row.account_id for row in rows] == ["44695443"]


def test_the_smoke_deployment_records_the_upper_cased_symbol(isolated_db) -> None:
    """Symbols are stored upper-cased everywhere else, and a mixed-case one would not match."""
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="eurusd")

    rows = _smoke_rows(database)
    assert rows[0].symbols_json == ["EURUSD"], rows[0].symbols_json


def test_the_smoke_deployment_is_idempotent_per_account(isolated_db) -> None:
    """A second smoke run must reuse the parent, not create a second row for the same label.

    Otherwise every run adds a deployment, and the signals of earlier runs point at a different
    parent than later ones - the orphans multiply instead of resolving.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    _account(database, "44695443")

    first = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")
    second = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="EURUSD")

    assert first == second
    assert len(_smoke_rows(database)) == 1, "a second deployment row was created for one label"


def test_discord_replay_smoke_routes_to_fake_order(tmp_path: Path) -> None:
    """The accounts env is written here, not taken from the working directory.

    ``Path(".accounts.env")`` is relative to the cwd, which for a plain ``pytest`` is the repo
    root: the smoke run then read the checkout's real credential file, and a run in another
    directory read nothing at all.
    """
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        "ALPACA_API_KEY=smoke-key\nALPACA_SECRET_KEY=smoke-secret\n", encoding="utf-8"
    )
    report = asyncio.run(
        run_smoke(
            mode="discord-replay",
            accounts_env=accounts_env,
            alpaca_symbol="BTC/USD",
            alpaca_notional=10.0,
            ctrader_symbol="EURUSD",
            ctrader_volume=100000,
            timeout_seconds=1,
        )
    )

    assert report.ok is True
    assert report.checks[0].name == "discord-replay-routing"
    assert report.checks[0].details["symbol"] == "AAPL"
    assert report.checks[0].details["broker_order_id"] == "discord-smoke-order"


def test_stockkit_smoke_signal_validates_against_the_resolved_broker_login(
    monkeypatch, tmp_path: Path
) -> None:
    """The smoke deployment is validated against the account it really trades.

    Passing the number the route *expects* makes the check compare a value to
    itself, so a mis-resolved credential would still pass. The broker login the
    smoke run actually resolved is the only meaningful comparison.
    """
    import live_runner.credential_env as credential_env
    from scripts.live_smoke import _stockkit_deployment_and_signal

    monkeypatch.setattr(
        credential_env, "load_accounts_env_values", lambda *_args, **_kwargs: {}
    )
    accounts_env = tmp_path / ".accounts.env"

    trade_signal, deployment, _live_signal = _stockkit_deployment_and_signal(
        {"DISCORD_BOT_RAMJET_CHANNEL_ID": "100000000000000002"},
        accounts_env=accounts_env,
        route="ramjet",
        account_id="acc-1",
        broker_login="PA3T0MQAGQ51",
        symbol="AAPL",
        message_price=200.0,
        stock_prices={"reference": 200.0, "buy_limit": 201.0},
        extended_hours=False,
    )

    assert trade_signal.symbol == "AAPL"
    assert deployment.account_id == "acc-1"

    with pytest.raises(ValueError, match="must route to Alpaca account"):
        _stockkit_deployment_and_signal(
            {"DISCORD_BOT_RAMJET_CHANNEL_ID": "100000000000000002"},
            accounts_env=accounts_env,
            route="ramjet",
            account_id="acc-1",
            broker_login="PA-SOMEONE-ELSE",
            symbol="AAPL",
            message_price=200.0,
            stock_prices={"reference": 200.0, "buy_limit": 201.0},
            extended_hours=False,
        )


def test_stockkit_smoke_route_validation_reads_the_given_accounts_env(tmp_path: Path) -> None:
    """``--accounts-env`` must drive the route check, not the project default file.

    The smoke run trades with the credentials of the file it was pointed at, so
    validating the route against a different file compares the deployment to an
    account it is not trading on - the same split-brain the ``--accounts-env``
    flag exists to remove.
    """
    from scripts.live_smoke import _stockkit_deployment_and_signal

    accounts_env = tmp_path / "smoke-accounts.env"
    accounts_env.write_text(
        "STOCKKIT_ACCOUNT_NUMBER_RAMJET=SMOKEACCOUNT42\n", encoding="utf-8"
    )

    # The file's explicit route number wins over the static fallback table, so a
    # pass here proves the tmp file - not the default - was the one read.
    _stockkit_deployment_and_signal(
        {"DISCORD_BOT_RAMJET_CHANNEL_ID": "100000000000000002"},
        accounts_env=accounts_env,
        route="ramjet",
        account_id="acc-1",
        broker_login="SMOKEACCOUNT42",
        symbol="AAPL",
        message_price=200.0,
        stock_prices={"reference": 200.0, "buy_limit": 201.0},
        extended_hours=False,
    )

    with pytest.raises(ValueError, match="SMOKEACCOUNT42"):
        _stockkit_deployment_and_signal(
            {"DISCORD_BOT_RAMJET_CHANNEL_ID": "100000000000000002"},
            accounts_env=accounts_env,
            route="ramjet",
            account_id="acc-1",
            broker_login="PA3T0MQAGQ51",
            symbol="AAPL",
            message_price=200.0,
            stock_prices={"reference": 200.0, "buy_limit": 201.0},
            extended_hours=False,
        )


# --- cTrader smoke order: live quote and real SL/TP (#184 finding 4) -------------------------


def _trendbar(minute: int, low: int, delta_close: int) -> SimpleNamespace:
    return SimpleNamespace(utcTimestampInMinutes=minute, low=low, deltaClose=delta_close)


class _FakeQuoteAdapter:
    ctid_trader_account_id = 123

    def __init__(self, bars: list[SimpleNamespace]) -> None:
        self.bars = bars
        self.requests: list[object] = []

    async def _resolve_symbol_id(self, symbol: str) -> int:
        assert symbol.upper() == "EURUSD"
        return 1

    async def _send_request(self, request: object, *, timeout_seconds: int = 10) -> object:
        self.requests.append(request)
        return SimpleNamespace(trendbar=self.bars)


def test_live_reference_price_is_the_close_of_the_latest_m1_bar() -> None:
    from scripts.live_smoke import _ctrader_live_reference_price

    adapter = _FakeQuoteAdapter(
        [_trendbar(100, 108_000, 50), _trendbar(102, 108_500, 120), _trendbar(101, 1, 1)]
    )

    price = asyncio.run(_ctrader_live_reference_price(adapter, "eurusd", timeout_seconds=5))

    assert price == pytest.approx(1.08620)
    request = adapter.requests[0]
    assert request.symbolId == 1 and request.ctidTraderAccountId == 123
    assert request.toTimestamp > request.fromTimestamp


def test_live_reference_price_fails_clearly_without_recent_bars() -> None:
    from scripts.live_smoke import _ctrader_live_reference_price

    with pytest.raises(RuntimeError, match="market closed"):
        asyncio.run(
            _ctrader_live_reference_price(_FakeQuoteAdapter([]), "EURUSD", timeout_seconds=5)
        )


@pytest.mark.parametrize(
    "bar",
    [_trendbar(100, 0, 0), _trendbar(100, -1, 0), _trendbar(100, 0, -1)],
    ids=["zero-close", "negative-low", "negative-delta"],
)
def test_live_reference_price_rejects_a_non_positive_close(bar) -> None:
    """A zero or corrupt close must fail loudly here, not somewhere downstream.

    The SL/TP helper would also reject such a price, but as a ``ValueError`` that reads like a bug
    in this function rather than in the quote. This ``RuntimeError`` is the "refusing to build the
    smoke order on garbage" contract, and it is the last refusal before either worker trades.
    """
    from scripts.live_smoke import _ctrader_live_reference_price

    with pytest.raises(RuntimeError, match="Invalid M1 close"):
        asyncio.run(
            _ctrader_live_reference_price(
                _FakeQuoteAdapter([bar]), "EURUSD", timeout_seconds=5
            )
        )


def test_live_reference_price_asks_for_the_documented_lookback_window() -> None:
    """The window is a documented constant, not a magic span: 30 minutes back.

    Too short and a slow tick source returns nothing on a thin symbol (the smoke refuses to run);
    too long and a stale bar gets used as if it were live.
    """
    from scripts.live_smoke import _ctrader_live_reference_price

    adapter = _FakeQuoteAdapter([_trendbar(100, 108_500, 120)])

    asyncio.run(_ctrader_live_reference_price(adapter, "EURUSD", timeout_seconds=5))

    request = adapter.requests[0]
    assert request.toTimestamp - request.fromTimestamp == 30 * 60_000, "30-minute lookback"


def test_trendbar_price_divisor_matches_the_adapter_price_unit_scale() -> None:
    """The smoke's trendbar scaling must be the adapter's cTrader price-unit scaling.

    The constants are separate by design (trendbar prices are absolute, SL/TP are relative
    distances) but they describe the same 1/100000 price unit, so a one-sided change would either
    misread every quote by orders of magnitude or mis-place every stop.
    """
    from live_runner.ctrader_adapter import CTRADER_RELATIVE_PRICE_UNIT_DIVISOR
    from scripts.live_smoke import _CTRADER_TRENDBAR_PRICE_DIVISOR

    assert _CTRADER_TRENDBAR_PRICE_DIVISOR == CTRADER_RELATIVE_PRICE_UNIT_DIVISOR == 100_000


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_smoke_sl_tp_are_on_the_correct_side_and_pass_the_adapter_conversion(action) -> None:
    """The smoke SL/TP must satisfy the adapter's own guards (right side, non-zero distance)."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter
    from scripts.live_smoke import _ctrader_smoke_sl_tp

    price = 1.0862
    stop_loss, take_profit = _ctrader_smoke_sl_tp(price, action)
    if action == "buy":
        assert stop_loss < price < take_profit
    else:
        assert take_profit < price < stop_loss

    signal = SimpleNamespace(symbol="EURUSD", action=action, theoretical_price=price)
    stub = SimpleNamespace(_scale_for=lambda _symbol: SimpleNamespace(price_digits=5))
    relative_stop, relative_take = CTraderLiveAdapter._resolve_relative_sl_tp(
        stub, signal, stop_loss, take_profit
    )
    assert relative_stop > 0 and relative_take > 0


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_smoke_sl_tp_rejects_an_unusable_price(price) -> None:
    from scripts.live_smoke import _ctrader_smoke_sl_tp

    with pytest.raises(ValueError):
        _ctrader_smoke_sl_tp(price, "buy")


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_smoke_sl_tp_distance_is_the_documented_fraction(action) -> None:
    """Both legs sit exactly 0.5 % away, symmetric, and rounded to 5 price decimals.

    The side-only test above would pass with a stop five pips or fifty pips away; the distance is
    a deliberate choice (deliberately wide, and irrelevant to a position closed within seconds),
    so it is pinned here rather than left to "far enough for the adapter to accept".
    """
    from scripts.live_smoke import _ctrader_smoke_sl_tp

    price = 1.0862
    stop_loss, take_profit = _ctrader_smoke_sl_tp(price, action)

    # 0.005 * 1.0862 == 0.005431, rounded to 5 price decimals on both legs.
    if action == "buy":
        assert stop_loss == 1.08077
        assert take_profit == 1.09163
    else:
        assert stop_loss == 1.09163
        assert take_profit == 1.08077


def test_order_worker_signal_carries_live_price_and_sl_tp(monkeypatch, isolated_db) -> None:
    """The order worker builds the signal from the fetched quote, not the old 1.0 placeholder."""
    import scripts.live_smoke as smoke

    database, _engine, _data_dir = isolated_db
    database.init_db()
    submitted: dict[str, object] = {}

    class _Adapter(_FakeQuoteAdapter):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__([_trendbar(10, 108_500, 120)])

        async def connect(self) -> None:
            return None

        async def submit_order(self, signal, *, qty=None) -> str:
            submitted["signal"] = signal
            raise RuntimeError("stop after submit")

    async def _no_position(*_args, **_kwargs):
        return None

    import live_runner.ctrader_adapter as ctrader_adapter

    monkeypatch.setattr(ctrader_adapter, "CTraderLiveAdapter", _Adapter)
    monkeypatch.setattr(ctrader_adapter, "_ensure_asyncio_twisted_reactor", lambda _loop: None)
    monkeypatch.setattr(smoke, "_find_ctrader_open_position", _no_position)
    monkeypatch.setattr(smoke, "_ensure_ctrader_smoke_deployment", lambda **_kw: str(uuid.uuid4()))
    env_file = Path(database.__file__).parent / "does-not-exist.env"
    monkeypatch.setattr(smoke, "_env_values", lambda _path: {})

    with pytest.raises(RuntimeError, match="stop after submit"):
        asyncio.run(
            smoke._ctrader_order_worker(
                account_id="44695443",
                accounts_env=env_file,
                symbol="EURUSD",
                volume=1000,
                timeout_seconds=5,
            )
        )

    metadata = submitted["signal"].metadata
    assert submitted["signal"].theoretical_price == pytest.approx(1.0862)
    assert metadata["entry_price"] == pytest.approx(1.0862)
    assert metadata["stop_loss"] < 1.0862 < metadata["take_profit"]


def test_strategy_e2e_worker_signal_carries_live_price_and_sl_tp(monkeypatch) -> None:
    """The e2e worker builds its signal from a live quote too, with real SL/TP.

    This is the worker that sizes through ``submit_order_with_risk``; its old
    ``theoretical_price=1.0`` / ``stop_loss=0.99`` pair measured a position off an invented price.
    It is the one path that opens a demo position without the explicit qty the order worker passes,
    so its quote, SL/TP and metadata are asserted here rather than left to the live run.
    """
    import live_runner.ctrader_adapter as ctrader_adapter
    import live_runner.daemon as daemon
    import scripts.live_smoke as smoke

    # Bound before the monkeypatching below, so the adapter's own SL/TP conversion is still the
    # real one when the captured signal is checked against it.
    real_adapter_class = ctrader_adapter.CTraderLiveAdapter
    submitted: dict[str, object] = {}

    class _Adapter(_FakeQuoteAdapter):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__([_trendbar(10, 108_500, 120)])

        async def connect(self) -> None:
            return None

    async def _submit(adapter, deployment, signal):
        submitted["signal"] = signal
        submitted["deployment"] = deployment
        raise RuntimeError("stop after submit")

    async def _no_position(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ctrader_adapter, "CTraderLiveAdapter", _Adapter)
    monkeypatch.setattr(ctrader_adapter, "_ensure_asyncio_twisted_reactor", lambda _loop: None)
    monkeypatch.setattr(daemon, "submit_order_with_risk", _submit)
    monkeypatch.setattr(smoke, "_find_ctrader_open_position", _no_position)
    monkeypatch.setattr(smoke, "_env_values", lambda _path: {})

    with pytest.raises(RuntimeError, match="stop after submit"):
        asyncio.run(
            smoke._ctrader_strategy_e2e_worker(
                account_id="44695443",
                accounts_env=Path("does-not-exist.env"),
                timeout_seconds=5,
            )
        )

    signal = submitted["signal"]
    deployment = submitted["deployment"]
    metadata = signal.metadata
    assert signal.symbol == "EURUSD" and signal.action == "buy"
    assert signal.theoretical_price == pytest.approx(1.0862)
    # Exact values, not just sides: the old e2e values were entry 1.0 with stop 0.99, which are on
    # the correct side of 1.0862 and would survive a side-only assertion.
    assert metadata["entry_price"] == pytest.approx(1.0862)
    assert metadata["stop_loss"] == 1.08077
    assert metadata["take_profit"] == 1.09163
    assert metadata["timeframe"] == deployment.timeframe == "1m"

    # The captured signal must still satisfy the adapter's own SL/TP guards, i.e. the values that
    # reach the wire are derived from the fetched quote and not just from each other.
    stub = SimpleNamespace(_scale_for=lambda _symbol: SimpleNamespace(price_digits=5))
    relative_stop, relative_take = real_adapter_class._resolve_relative_sl_tp(
        stub, signal, metadata["stop_loss"], metadata["take_profit"]
    )
    assert relative_stop > 0 and relative_take > 0
