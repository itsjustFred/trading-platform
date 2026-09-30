from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

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
