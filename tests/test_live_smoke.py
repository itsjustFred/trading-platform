from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.live_smoke import SmokeCheck, SmokeReport, _position_qty_for_symbol, main, run_smoke


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


def _cleanup_adapter(*, cancel_exc=None, position_exc=None, positions=()):
    """An adapter shaped like the one `_cancel_stockkit_buy_order` reaches into."""
    client = SimpleNamespace()

    def cancel_order_by_id(order_id: str) -> None:
        if cancel_exc is not None:
            raise cancel_exc

    client.cancel_order_by_id = cancel_order_by_id

    async def get_positions():
        if position_exc is not None:
            raise position_exc
        return list(positions)

    return SimpleNamespace(_trading_client=client, get_positions=get_positions)


def _run_cleanup(**kwargs) -> dict:
    from scripts.live_smoke import _cancel_stockkit_buy_order

    details = {"remaining_qty": None, "filled_qty": 0.0}
    asyncio.run(
        _cancel_stockkit_buy_order(
            _cleanup_adapter(**kwargs),
            symbol="AAPL",
            buy_order_id="order-1",
            details=details,
        )
    )
    return details


def test_a_failed_cancel_is_reported_rather_than_swallowed() -> None:
    """The failure-path cleanup can fail, and the report has to say so.

    `_cancel_stockkit_buy_order` runs when a check has already failed and a buy
    order may have been left open. It swallowed both the cancel error and the
    position re-read, so a cancel that failed produced a report identical to one
    where everything was fine.

    The sibling helper `_cancel_unfilled_alpaca_order` already got this right,
    returning `cancel_failed:<Type>`, so the pattern to follow was in the same file
    twenty lines away.
    """
    details = _run_cleanup(cancel_exc=RuntimeError("no such order"))
    assert details["cancel_status"] == "cancel_failed:RuntimeError"
    assert "swallowed" not in details.get("cancel_status", "")


def test_a_clean_cancel_is_reported_as_such() -> None:
    details = _run_cleanup()
    assert details["cancel_status"] == "buy_order_cancelled"
    assert details["position_known"] is True
    assert details["remaining_qty"] == 0.0


def test_a_position_that_could_not_be_read_is_not_reported_as_flat() -> None:
    """`remaining_qty` starts as None, so silence used to read like a clean zero.

    An operator reading a failed smoke report needs to tell "the position is flat"
    from "the broker would not tell us". The two were the same value before.
    """
    details = _run_cleanup(position_exc=OSError("broker unreachable"))
    assert details["position_known"] is False, (
        "an unreadable position must be marked unknown, not left indistinguishable "
        "from a flat one"
    )
    assert details["remaining_qty"] is None
    assert details["position_error"] == "OSError"


def test_a_position_still_open_after_a_failed_cancel_is_visible() -> None:
    """The whole point: the residue of a failure must reach the report.

    Cancel failed, and the position is still there. Before, the report said nothing
    about either.
    """
    details = _run_cleanup(
        cancel_exc=RuntimeError("no such order"),
        positions=[SimpleNamespace(symbol="AAPL", qty="3")],
    )
    assert details["cancel_status"] == "cancel_failed:RuntimeError"
    assert details["position_known"] is True
    assert details["remaining_qty"] == 3.0, (
        "a position left open by a failed cancel must appear in the report"
    )


def test_both_the_cancel_and_the_position_read_can_fail_at_once() -> None:
    """The worst case, and the one that was previously indistinguishable from clean."""
    details = _run_cleanup(cancel_exc=ValueError("bad request"), position_exc=OSError("down"))
    assert details["cancel_status"] == "cancel_failed:ValueError"
    assert details["position_known"] is False
    assert details["position_error"] == "OSError"


def _route_env(route: str, broker_login: str) -> dict:
    """An accounts-env binding one named route to one account number."""
    return {
        "ALPACA_API_KEY_ACC1": "key-one",
        "ALPACA_SECRET_KEY_ACC1": "secret-one",
        "ALPACA_ACCOUNT_NUMBER_ACC1": broker_login,
        "ALPACA_ACCOUNT_NAME_ACC1": route,
        "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
    }


def test_the_smoke_route_reports_the_account_its_credentials_belong_to() -> None:
    """The reported account must come from the env, not the hard-coded route table.

    `STOCKKIT_ACCOUNT_ROUTES` is a literal table in `stockkit_routing.py`, and the
    accounts env is operator-supplied. The two can disagree: a bot named "nitro" can
    be bound in the env to an account other than the one the table records.

    The smoke run used to resolve credentials for the env's account and then report
    the table's, so it would trade one account while `details["account_id"]` named
    another - and the routing check below it, which is fed the reported value, would
    compare the table's number against itself and pass. A false "routing OK" for an
    account nobody traded.
    """
    from live_runner.stockkit_routing import STOCKKIT_ACCOUNT_ROUTES
    from scripts.live_smoke import _stockkit_route_with_credentials

    env = _route_env("nitro", "PA0ENVBOUND99")
    route, account, credentials = _stockkit_route_with_credentials(env)

    assert route == "nitro"
    assert credentials is not None
    assert account == "PA0ENVBOUND99", (
        f"reported {account!r} - the account the credentials belong to is "
        f"PA0ENVBOUND99, not the route table's "
        f"{STOCKKIT_ACCOUNT_ROUTES['nitro']!r}"
    )
    assert account != STOCKKIT_ACCOUNT_ROUTES["nitro"], (
        "this test is only meaningful while the env and the table disagree"
    )


def test_the_smoke_routing_check_can_actually_fail() -> None:
    """End to end: an env that resolves the route to one account but the credentials to
    another is now refused, rather than waved through.

    `expected_account_number_for_route` resolves in three steps - an explicit
    `STOCKKIT_ACCOUNT_NUMBER_<ROUTE>`, then the ACC block whose name label matches,
    then the static table. `_stockkit_route_with_credentials` finds credentials by the
    *name label* rule. So an env that sets the explicit entry to one account while a
    differently-numbered block carries the bot's name puts the two resolvers on
    different accounts, and the smoke run would trade the labelled one on a route that
    is required to use the explicit one.

    Before the fix this passed. The deployment was labelled with a value derived the
    same way the validator derived its expectation, so the comparison was circular. A
    check that cannot fail is not a check, and this is the check in the smoke script
    an operator runs to convince themselves a Discord route is safe.
    """
    from live_runner.stockkit_routing import validate_stockkit_deployment_route
    from scripts.live_smoke import _stockkit_route_with_credentials
    from shared.models import LiveDeploymentConfig

    # The route is explicitly required to be PA0REQUIRED99 ...
    env = _route_env("nitro", "PA0LABELLED77")
    env["STOCKKIT_ACCOUNT_NUMBER_NITRO"] = "PA0REQUIRED99"

    route, account, _ = _stockkit_route_with_credentials(env)
    assert account == "PA0LABELLED77", "credentials resolve by the name label"
    assert account != "PA0REQUIRED99"

    deployment = LiveDeploymentConfig(
        strategy_name="stockkit_discord_e2e",
        account_id=account,
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source_type="discord",
        signal_source="discord",
        signal_source_id=route.upper(),
        signal_config={"service_name": route, "channel_id": "1234567890123456789"},
    )

    with pytest.raises(ValueError, match="must route to Alpaca account"):
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login=account,
            accounts_env_values=env,
        )


def test_an_env_matching_the_route_table_still_validates() -> None:
    """The fix must not refuse a correctly configured operator."""
    from live_runner.stockkit_routing import (
        STOCKKIT_ACCOUNT_ROUTES,
        validate_stockkit_deployment_route,
    )
    from scripts.live_smoke import _stockkit_route_with_credentials
    from shared.models import LiveDeploymentConfig

    env = _route_env("nitro", STOCKKIT_ACCOUNT_ROUTES["nitro"])
    route, account, _ = _stockkit_route_with_credentials(env)
    deployment = LiveDeploymentConfig(
        strategy_name="stockkit_discord_e2e",
        account_id=account,
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source_type="discord",
        signal_source="discord",
        signal_source_id=route.upper(),
        signal_config={"service_name": route, "channel_id": "1234567890123456789"},
    )

    validate_stockkit_deployment_route(
        deployment, account_broker_login=account, accounts_env_values=env
    )
    assert account == STOCKKIT_ACCOUNT_ROUTES["nitro"]
