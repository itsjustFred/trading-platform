"""#123: every engine/importer rejects an unknown per-entry direction instead of trading LONG."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import _csv_trade_direction
from runner.engines.vectorbt_adapter import _normalize_direction_value
from shared.database import _build_backtest_trades
from shared.models import normalize_direction_value
from tests.helpers import with_zero_fees


def _engine(tmp_path) -> TradeEngine:
    cfg = BacktestConfig(
        **with_zero_fees(
            {
                "strategy_name": "probe",
                "symbols": ["P"],
                "timeframe": "1h",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "initial_capital": 100_000,
                "risk_per_trade_pct": 0.01,
                "modeling_resolution": "main_timeframe",
                "data_dir": str(tmp_path),
                "parameters": {},
            }
        )
    )
    return TradeEngine(cfg, symbol="P")


@pytest.mark.parametrize("value", ["LONG", "long", 1, 1.0, np.int64(1)])
def test_shared_long(value) -> None:
    assert normalize_direction_value(value) == "LONG"


@pytest.mark.parametrize("value", ["SHORT", "short", -1, -1.0, np.int64(-1)])
def test_shared_short(value) -> None:
    assert normalize_direction_value(value) == "SHORT"


@pytest.mark.parametrize(
    "value",
    [math.nan, None, pd.NA, 0, "", "BUY", "SELL", "Long ", True, False, np.True_, np.False_],
)
def test_shared_and_vectorbt_raise_valueerror(value) -> None:
    with pytest.raises(ValueError, match="direction"):
        normalize_direction_value(value)
    with pytest.raises(ValueError, match="direction"):
        _normalize_direction_value(value)


@pytest.mark.parametrize("value", ["SHORT", "short", -1])
def test_native_short(tmp_path, value) -> None:
    assert _engine(tmp_path)._resolve_direction(pd.Series({"direction": value})) == "SHORT"


@pytest.mark.parametrize("value", [math.nan, None, 0, "BUY", "sell", ""])
def test_native_unknown_raises(tmp_path, value) -> None:
    with pytest.raises(ValueError, match="direction"):
        _engine(tmp_path)._resolve_direction(pd.Series({"direction": value}))


def test_native_without_direction_column_is_long_only(tmp_path) -> None:
    assert _engine(tmp_path)._resolve_direction(pd.Series({"close": 1.0})) == "LONG"


def test_native_and_vectorbt_agree_on_unknown_values(tmp_path) -> None:
    for value in [math.nan, 0, "BUY"]:
        with pytest.raises(ValueError):
            _engine(tmp_path)._resolve_direction(pd.Series({"direction": value}))
        with pytest.raises(ValueError):
            _normalize_direction_value(value)


def test_pynecore_csv_direction() -> None:
    assert _csv_trade_direction("entry long") == "LONG"
    assert _csv_trade_direction("entry short") == "SHORT"
    for bad in ["entry", "entry flat", "entry long short"]:
        with pytest.raises(ValueError, match="direction"):
            _csv_trade_direction(bad)


def test_stored_legacy_row_with_bad_direction_raises_valueerror() -> None:
    row = SimpleNamespace(
        trade_nr=1,
        symbol="AAPL",
        direction="long",
        entry_time="2024-01-01T00:00:00+00:00",
        entry_price=1.0,
        quantity=1.0,
        exits=[],
        pnl=0.0,
        position_size=None,
        risk_amount=None,
        risk_percent=None,
        exit_reason=None,
        pnl_pct=None,
        fees=None,
        net_pnl=None,
        r_multiple=None,
        mae=None,
        mfe=None,
        holding_bars=None,
        tags=[],
        meta={},
    )
    with pytest.raises(ValueError, match="direction"):
        _build_backtest_trades([(row,)])


def test_viewer_treats_contract_violating_stored_result_as_unavailable(monkeypatch) -> None:
    from shared.models import TradeContractError
    from viewer import main as viewer_main

    def boom(_id):
        raise TradeContractError("direction must be one of ('LONG', 'SHORT'), got 'long'")

    monkeypatch.setattr(viewer_main, "load_backtest_result", boom)
    assert viewer_main._load_stored_result("legacy") is None


def test_viewer_does_not_hide_unrelated_value_errors(monkeypatch) -> None:
    """Only the Trade-contract error is treated as unavailable; others propagate."""
    from viewer import main as viewer_main

    def boom(_id):
        raise ValueError("corrupt stored metadata")

    monkeypatch.setattr(viewer_main, "load_backtest_result", boom)
    with pytest.raises(ValueError, match="corrupt stored metadata"):
        viewer_main._load_stored_result("broken")
    with pytest.raises(ValueError, match="corrupt stored metadata"):
        viewer_main._load_result("broken")


def test_contract_violating_row_never_falls_back_to_json_rewrite(monkeypatch, tmp_path) -> None:
    """A row that exists but breaks the contract must not trigger the JSON fallback,
    which would rewrite the stored rows during a read."""
    from shared.models import TradeContractError
    from viewer import main as viewer_main

    (tmp_path / "legacy.trades.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(viewer_main, "BACKTESTS_DIR", tmp_path)

    def boom(_id):
        raise TradeContractError("direction must be one of ('LONG', 'SHORT'), got 'long'")

    def no_rewrite(_result):
        raise AssertionError("save_result_to_sqlite must not run for an existing row")

    monkeypatch.setattr(viewer_main, "load_backtest_result", boom)
    monkeypatch.setattr(viewer_main, "save_result_to_sqlite", no_rewrite)
    assert viewer_main._load_result("legacy") is None


def test_trade_direction_violation_is_a_trade_contract_error() -> None:
    from shared.models import Trade, TradeContractError

    with pytest.raises(TradeContractError):
        Trade(id=1, symbol="X", direction="long", entry_time="2024-01-01T00:00:00Z",
              entry_price=1.0, quantity=1.0, exits=[], pnl=0.0)
