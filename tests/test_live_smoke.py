from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.live_smoke import SmokeCheck
from scripts.live_smoke import SmokeReport
from scripts.live_smoke import main
from scripts.live_smoke import _position_qty_for_symbol
from scripts.live_smoke import run_smoke


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


def test_discord_replay_smoke_routes_to_fake_order() -> None:
    report = asyncio.run(
        run_smoke(
            mode="discord-replay",
            accounts_env=Path(".accounts.env"),
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


def test_stockkit_smoke_signal_validates_against_the_resolved_broker_login(monkeypatch) -> None:
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

    trade_signal, deployment, _live_signal = _stockkit_deployment_and_signal(
        {"DISCORD_BOT_RAMJET_CHANNEL_ID": "100000000000000002"},
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
            route="ramjet",
            account_id="acc-1",
            broker_login="PA-SOMEONE-ELSE",
            symbol="AAPL",
            message_price=200.0,
            stock_prices={"reference": 200.0, "buy_limit": 201.0},
            extended_hours=False,
        )
