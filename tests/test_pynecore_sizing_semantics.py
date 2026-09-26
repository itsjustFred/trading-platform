"""PyneCore adapter sizing/fill regressions (P2 must-fix A-C3/A-H4/A-M9).

Mirrors ``tests/test_vectorbt_sizing_semantics.py`` F35 (A-M9: the identical
PyneCore branch was test-blind) plus the P2 fixes:

- A-C3 — ``_apply_risk_sizing`` mirrors vectorbt ``min()`` semantics: the
  position cap binds even when ``max_leverage`` would allow more (no
  ``max()`` leverage floor, no over-cap fill).
- A-H4 (D3) — ``_gate_warmup_trades`` drops pre-``start_date`` trades when
  ``warmup_start`` is set; ``_apply_fill_parity`` re-prices LONG entries to
  the ask and non-level exits per ``signal_exit_fill`` (SHORT open-exits pay
  the spread), leaving stop/TP level fills untouched.
"""

from __future__ import annotations
from tests.helpers import with_zero_fees

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import (
    _apply_fill_parity,
    _apply_risk_sizing,
    _gate_warmup_trades,
    _pyne_cli_end_bound,
    _pyne_cli_start_bound,
)
from runner.engines._shared import resolve_max_leverage as _resolve_max_leverage
from shared.models import Trade, TradeExit


def _trade(
    *,
    entry_price: float = 100.0,
    sl_price: float | None = 99.0,
    exit_price: float = 101.0,
    exit_reason: str | None = None,
    direction: str = "LONG",
    entry_time: str = "2024-01-02T00:00:00+00:00",
    exit_time: str = "2024-01-03T00:00:00+00:00",
    quantity: float = 1.0,
) -> Trade:
    meta: dict[str, Any] = {}
    if sl_price is not None:
        meta["sl_price"] = sl_price
    return Trade(
        id=1,
        symbol="T",
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[
            TradeExit(
                time=exit_time,
                price=exit_price,
                quantity=quantity,
                pnl=0.0,
                reason=exit_reason,
            )
        ],
        pnl=0.0,
        meta=meta,
    )


def _config(tmp_path: Path, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "pyne_sizing",
        "symbols": ["T"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-02-01",
        "initial_capital": 100_000,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-02 00:00", "2024-01-03 00:00"], utc=True
            ),
            "open": [100.0, 101.0],
            "high": [100.5, 101.5],
            "low": [99.5, 100.5],
            "close": [100.2, 101.2],
            "volume": [1000.0, 1000.0],
            "spread_price": [0.2, 0.2],
        }
    )


# ─── A-M9: F35 mirror — max_leverage is an independent cap ───────────────────


class TestLeverageSemantics:
    def test_resolver_keeps_conservative_leverage(self):
        assert _resolve_max_leverage(1.0) == 1.0
        assert _resolve_max_leverage(0.5) == 0.5

    def test_conservative_max_leverage_binds_notional(self, tmp_path):
        """max_leverage=1 must cap the notional at 1x equity — no override."""
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_exposure_pct=1.0,  # position cap allows full equity
            max_leverage=1.0,  # leverage forbids more than that
        )
        trades = [_trade()]
        _apply_risk_sizing(trades, config, "equities")
        assert trades, "fixture must produce a trade"
        for trade in trades:
            notional = trade.entry_price * trade.quantity
            assert notional <= 100_000.0 * 1.0 + 1e-6, (
                "max_leverage=1 must bind the notional to equity even when "
                "max_position_exposure_pct would allow more"
            )


# ─── A-C3: min() semantics — the position cap binds ──────────────────────────


class TestPositionCapSemantics:
    def test_position_cap_binds_below_leverage_limit(self, tmp_path):
        """required > cap with leverage headroom must fill AT the cap."""
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,  # risk $5000, stop dist 1.0 -> 500k required
            max_position_exposure_pct=0.20,  # cap 20k
            max_leverage=2.0,  # leverage limit 200k
        )
        trades = [_trade()]
        _apply_risk_sizing(trades, config, "equities")
        notional = trades[0].entry_price * trades[0].quantity
        assert notional == pytest.approx(20_000.0), (
            f"position cap must bind (min semantics), got notional {notional}"
        )

    def test_leverage_binds_below_position_cap(self, tmp_path):
        """required > leverage limit with cap headroom must fill AT leverage."""
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_exposure_pct=1.0,  # cap 100k... use equities default 1.0
            max_leverage=0.10,  # leverage limit 10k binds
        )
        trades = [_trade()]
        _apply_risk_sizing(trades, config, "equities")
        notional = trades[0].entry_price * trades[0].quantity
        assert notional == pytest.approx(10_000.0), (
            f"leverage limit must bind (min semantics), got notional {notional}"
        )

    def test_small_required_passes_through(self, tmp_path):
        """required below both caps must fill the full risk-sized notional."""
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.0001,  # risk $10, stop dist 1.0 -> 1k required
            max_position_exposure_pct=0.20,
            max_leverage=2.0,
        )
        trades = [_trade()]
        _apply_risk_sizing(trades, config, "equities")
        notional = trades[0].entry_price * trades[0].quantity
        assert notional == pytest.approx(1_000.0)


# ─── A-H4 (D3): warmup gate + fill parity ────────────────────────────────────


class TestWarmupGate:
    def test_warmup_drops_pre_start_trades(self, tmp_path):
        config = _config(
            tmp_path,
            warmup_start="2024-01-01",
            start_date="2024-01-03",
        )
        trades = [
            _trade(entry_time="2024-01-02T00:00:00+00:00"),
            _trade(entry_time="2024-01-03T00:00:00+00:00"),
        ]
        kept = _gate_warmup_trades(trades, config)
        assert len(kept) == 1
        assert kept[0].entry_time.startswith("2024-01-03")

    def test_no_warmup_is_passthrough(self, tmp_path):
        config = _config(tmp_path)
        trades = [_trade(entry_time="2024-01-02T00:00:00+00:00")]
        assert _gate_warmup_trades(trades, config) == trades

    def test_warmup_without_start_date_fails_loudly(self, tmp_path):
        """A-M11 parity with native/vectorbt: warmup without a parseable
        start_date must raise — never silently trade the warmup bars."""
        config = _config(tmp_path, warmup_start="2024-01-01", start_date="")
        trades = [_trade(entry_time="2024-01-02T00:00:00+00:00")]
        with pytest.raises(ValueError, match="warmup_start"):
            _gate_warmup_trades(trades, config)


class TestFillParity:
    def test_long_entry_lifts_ask(self, tmp_path):
        config = _config(tmp_path)
        trades = [_trade(exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].entry_price == pytest.approx(100.2)

    def test_short_entry_hits_bid(self, tmp_path):
        config = _config(tmp_path)
        trades = [_trade(direction="SHORT", exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].entry_price == pytest.approx(100.0)

    def test_signal_exit_defaults_to_close(self, tmp_path):
        config = _config(tmp_path)  # signal_exit_fill="close"
        trades = [_trade(exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].exits[-1].price == pytest.approx(101.2)

    def test_signal_exit_open_fill(self, tmp_path):
        config = _config(tmp_path, signal_exit_fill="open")
        trades = [_trade(exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].exits[-1].price == pytest.approx(101.0)

    def test_short_open_exit_pays_spread(self, tmp_path):
        config = _config(tmp_path, signal_exit_fill="open")
        trades = [_trade(direction="SHORT", exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].exits[-1].price == pytest.approx(101.0 + 0.2)

    def test_stop_level_exit_untouched(self, tmp_path):
        config = _config(tmp_path, signal_exit_fill="open")
        trades = [_trade(exit_price=99.0, exit_reason="stop_loss")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "spread_price"}
        )
        assert trades[0].exits[-1].price == pytest.approx(99.0)

    def test_no_spread_column_leaves_prices(self, tmp_path):
        config = _config(tmp_path)
        trades = [_trade(exit_reason="signal")]
        before = (trades[0].entry_price, trades[0].exits[-1].price)
        _apply_fill_parity(trades, _frame(), config, {})
        # close-fill re-price to the located bar close is still applied;
        # entry without spread stays.
        assert trades[0].entry_price == pytest.approx(before[0])
        assert trades[0].exits[-1].price == pytest.approx(101.2)

    def test_uppercase_spread_column_still_prices_spread(self, tmp_path):
        """A-L6 parity with native/_vbt: frame columns load lowercased, so an
        uppercase configured name must not silently price 0.0 spread."""
        config = _config(tmp_path)
        trades = [_trade(exit_reason="signal")]
        _apply_fill_parity(
            trades, _frame(), config, {"spread_price_col": "Spread_Price"}
        )
        assert trades[0].entry_price == pytest.approx(100.2)


class TestPyneCliDateBounds:
    def test_date_only_end_bound_includes_full_end_day(self):
        # F9: a date-only --to is midnight-exclusive in the pyne CLI, so the
        # adapter must translate it to the inclusive end-of-day instant —
        # otherwise the replay window silently drops the end date's bars.
        assert _pyne_cli_end_bound("2024-01-31") == "2024-01-31T23:59:59.999999+00:00"

    def test_end_bound_with_time_passes_through(self):
        assert _pyne_cli_end_bound("2024-01-31T14:30:00Z") == "2024-01-31T14:30:00Z"

    def test_date_only_start_bound_is_host_tz_independent(self):
        # F9: a naive date-only --from resolves in the HOST timezone inside
        # the CLI — the adapter must pass an aware UTC start-of-day instant.
        assert _pyne_cli_start_bound("2024-01-01") == "2024-01-01T00:00:00+00:00"

    def test_start_bound_with_time_passes_through(self):
        assert _pyne_cli_start_bound("2024-01-01T09:00:00+00:00") == "2024-01-01T09:00:00+00:00"


def test_pyne_cli_receives_full_day_bounds_for_date_only_config(monkeypatch, tmp_path):
    """C2: bound contract — date-only bounds reach the CLI as full-day
    instants, so pyne replays the same window as native/vectorbt. Anything
    narrower fails loudly (midnight truncation) instead of silently."""
    import subprocess

    from runner.engines.pynecore_adapter import PyneCoreAdapter

    seen: dict[str, str] = {}

    def _fake_which(_name):
        return "/usr/bin/pyne"

    def _fake_run(cmd, **kwargs):
        args = list(cmd)
        seen["from"] = args[args.index("--from") + 1]
        seen["to"] = args[args.index("--to") + 1]
        trade_csv = Path(args[args.index("--trade") + 1])
        trade_csv.write_text(
            "Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,"
            "Profit USD,Profit %,Cumulative profit USD,Cumulative profit %,"
            "Run-up USD,Run-up %,Drawdown USD,Drawdown %\n"
        )
        stats_csv = Path(args[args.index("--strat") + 1])
        stats_csv.write_text("stat,value\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", _fake_which)
    import runner.engines.pynecore_adapter as pyne_module

    monkeypatch.setattr(pyne_module.shutil, "which", _fake_which)
    monkeypatch.setattr(pyne_module.subprocess, "run", _fake_run)

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC"),
            "open": [100.0] * 48,
            "high": [101.0] * 48,
            "low": [99.0] * 48,
            "close": [100.5] * 48,
            "volume": [1000.0] * 48,
        }
    )
    ohlcv_dir = tmp_path / "ohlcv" / "test" / "PYNE" / "1h.parquet"
    ohlcv_dir.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(ohlcv_dir, index=False)
    strategy = tmp_path / "pyne_bound.py"
    strategy.write_text(
        "STRATEGY_META = {'name': 'pyne_bound'}\n"
        "def generate_signals(df, params):\n"
        "    import pandas as pd\n"
        "    n = len(df)\n"
        "    return (pd.Series([False] * n, index=df.index),\n"
        "            pd.Series([False] * n, index=df.index), {})\n",
        encoding="utf-8",
    )
    from runner.engines.base import BacktestConfig

    config = BacktestConfig(
        strategy_name="pyne_bound",
        symbols=["PYNE"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-02",
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
        data_dir=str(tmp_path),
        parameters={"fees": "zero"},
    )
    PyneCoreAdapter().run(config, str(strategy))

    assert seen["from"] == "2024-01-01T00:00:00+00:00"
    assert seen["to"] == "2024-01-02T23:59:59.999999+00:00"
