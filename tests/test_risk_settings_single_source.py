"""Characterization pins for the deployment-settings → ``RiskConfig`` seam.

Improvement-run proposal G03: the daemon used to re-type every ``RiskConfig``
default in its own builder (``_risk_config_for_deployment``). These tests pin
what that builder produces today -- valid inputs, the ``risk_per_trade_pct``
alias, and the exact error for every rejected value -- so moving the mapping to
``RiskConfig.from_settings`` cannot change a live deployment's risk policy by
accident.

The fallback-stop and sizing pins live here too: the same proposal unifies the
fallback-stop arithmetic and makes ``calculate_position_size`` return its
resolved stop instead of mutating the signal, so each of those behaviours is
pinned at its call site first.
"""

from __future__ import annotations

import pandas as pd
import pytest

from live_runner.daemon import _risk_config_for_deployment
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from runner.engine_pkg.orders import Order, OrderType
from runner.engine_pkg.trading_engine import TradeEngine
from runner.engines.base import BacktestConfig
from shared.models import LiveDeploymentConfig


def _deployment(**settings: object) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="risk-config-pin",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        settings=dict(settings),
    )


def _engine(**overrides: object) -> TradeEngine:
    config: dict[str, object] = {
        "strategy_name": "test",
        "symbols": ["T"],
        "timeframe": "1m",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "parameters": {"fees": "zero", "slippage": "none"},
    }
    config.update(overrides)
    return TradeEngine(BacktestConfig(**config), symbol="T")


# ---------------------------------------------------------------------------
# 1. The daemon's RiskConfig builder
# ---------------------------------------------------------------------------


def test_empty_settings_build_the_dataclass_defaults() -> None:
    assert _risk_config_for_deployment(_deployment()) == RiskConfig(
        max_risk_per_trade=0.01,
        max_total_exposure=0.05,
        max_positions=5,
        default_sl_atr_multiplier=2.0,
        fallback_stop_pct=0.02,
        max_notional_per_trade=0.20,
        allow_fallback_stop=True,
        reject_missing_stop=False,
    )


def test_every_setting_is_mapped() -> None:
    config = _risk_config_for_deployment(
        _deployment(
            max_risk_per_trade=0.02,
            max_total_exposure=0.10,
            max_positions=3,
            default_sl_atr_multiplier=1.5,
            fallback_stop_pct=0.03,
            max_notional_per_trade=0.50,
            allow_fallback_stop=False,
            reject_missing_stop=True,
        )
    )

    assert config == RiskConfig(
        max_risk_per_trade=0.02,
        max_total_exposure=0.10,
        max_positions=3,
        default_sl_atr_multiplier=1.5,
        fallback_stop_pct=0.03,
        max_notional_per_trade=0.50,
        allow_fallback_stop=False,
        reject_missing_stop=True,
    )


def test_risk_per_trade_pct_is_the_documented_alias() -> None:
    config = _risk_config_for_deployment(_deployment(risk_per_trade_pct=0.005))
    assert config.max_risk_per_trade == pytest.approx(0.005)

    # The canonical key wins when both are present.
    config = _risk_config_for_deployment(
        _deployment(risk_per_trade_pct=0.005, max_risk_per_trade=0.02)
    )
    assert config.max_risk_per_trade == pytest.approx(0.02)


def test_numeric_strings_and_integral_floats_are_accepted() -> None:
    config = _risk_config_for_deployment(
        _deployment(max_risk_per_trade="0.02", max_positions="3")
    )
    assert config.max_risk_per_trade == pytest.approx(0.02)
    assert config.max_positions == 3


def test_boolean_flags_accept_the_config_spellings() -> None:
    config = _risk_config_for_deployment(
        _deployment(allow_fallback_stop="false", reject_missing_stop=1)
    )
    assert config.allow_fallback_stop is False
    assert config.reject_missing_stop is True


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"max_risk_per_trade": float("nan")}, "max_risk_per_trade must be finite"),
        ({"max_risk_per_trade": float("inf")}, "max_risk_per_trade must be finite"),
        ({"max_risk_per_trade": -0.1}, "max_risk_per_trade must be >= 0.0"),
        ({"max_risk_per_trade": "abc"}, "Invalid max_risk_per_trade: 'abc'"),
        ({"max_risk_per_trade": True}, "Invalid max_risk_per_trade: True"),
        ({"max_risk_per_trade": None}, "Invalid max_risk_per_trade: None"),
        ({"max_risk_per_trade": 0.0}, "max_risk_per_trade must be > 0 and <= 1"),
        ({"max_risk_per_trade": 1.5}, "max_risk_per_trade must be > 0 and <= 1"),
        ({"max_total_exposure": 0.0}, "max_total_exposure must be > 0 and <= 1"),
        ({"max_total_exposure": 1.5}, "max_total_exposure must be > 0 and <= 1"),
        ({"max_positions": 2.5}, "max_positions must be an integer"),
        ({"max_positions": "abc"}, "Invalid max_positions: 'abc'"),
        ({"max_positions": True}, "Invalid max_positions: True"),
        ({"max_positions": 0}, "max_positions must be >= 1"),
        (
            {"default_sl_atr_multiplier": 0.0},
            "default_sl_atr_multiplier must be > 0",
        ),
        (
            {"default_sl_atr_multiplier": float("nan")},
            "default_sl_atr_multiplier must be finite",
        ),
        ({"default_sl_atr_multiplier": -1.0}, "default_sl_atr_multiplier must be >= 0.0"),
        (
            {"fallback_stop_pct": 0.0},
            "fallback_stop_pct must be between 0 and 1",
        ),
        (
            {"fallback_stop_pct": 1.0},
            "fallback_stop_pct must be between 0 and 1",
        ),
        ({"fallback_stop_pct": "abc"}, "Invalid fallback_stop_pct: 'abc'"),
        ({"max_notional_per_trade": 0.0}, "max_notional_per_trade must be > 0"),
        ({"max_notional_per_trade": "abc"}, "Invalid max_notional_per_trade: 'abc'"),
        ({"max_notional_per_trade": float("nan")}, "max_notional_per_trade must be finite"),
        (
            {"allow_fallback_stop": "yes"},
            "allow_fallback_stop must be boolean",
        ),
        ({"allow_fallback_stop": 2}, "allow_fallback_stop must be boolean"),
        (
            {"reject_missing_stop": "yes"},
            "reject_missing_stop must be boolean",
        ),
        ({"reject_missing_stop": -1}, "reject_missing_stop must be boolean"),
    ],
)
def test_invalid_values_keep_their_exact_error(
    settings: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        _risk_config_for_deployment(_deployment(**settings))
    assert str(excinfo.value) == message


# ---------------------------------------------------------------------------
# 2. The percent / ATR fallback stop, pinned through the risk engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "entry_price", "fallback_stop_pct", "expected"),
    [
        ("buy", 100.0, 0.02, 98.0),
        ("sell", 100.0, 0.02, 102.0),
        ("buy", 250.5, 0.03, 250.5 - 250.5 * 0.03),
        ("sell", 250.5, 0.03, 250.5 + 250.5 * 0.03),
    ],
)
def test_risk_engine_percent_fallback_stop_price(
    action: str,
    entry_price: float,
    fallback_stop_pct: float,
    expected: float,
) -> None:
    engine = RiskEngine(RiskConfig(fallback_stop_pct=fallback_stop_pct))
    signal = TradeSignal(
        action=action,
        symbol="T",
        entry_price=entry_price,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata={"current_positions": 0},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.decision is not None
    assert sized.decision.stop_price_used == pytest.approx(expected)
    assert sized.decision.metadata["stop_source"] == "fallback_percent"
    # The resolved stop is returned, not written onto the input signal.
    assert sized.resolved_stop_loss == pytest.approx(expected)
    assert signal.stop_loss is None
    assert "resolved_stop_loss" not in signal.metadata


@pytest.mark.parametrize(("action", "expected"), [("buy", 97.0), ("sell", 103.0)])
def test_risk_engine_atr_fallback_stop_price(action: str, expected: float) -> None:
    engine = RiskEngine(RiskConfig(default_sl_atr_multiplier=2.0))
    signal = TradeSignal(
        action=action,
        symbol="T",
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata={"current_positions": 0, "atr": 1.5},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.decision is not None
    assert sized.decision.stop_price_used == pytest.approx(expected)
    assert sized.decision.metadata["stop_source"] == "fallback_atr"


# ---------------------------------------------------------------------------
# 3. Native-engine fallback stops (creation estimate, pending-order exposure,
#    fill-time re-derivation)
# ---------------------------------------------------------------------------


def _signal_bar(close: float = 100.0) -> pd.Series:
    return pd.Series(
        {
            "timestamp": pd.Timestamp("2024-01-01T00:00:00Z"),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        }
    )


@pytest.mark.parametrize(
    ("direction", "close", "expected"),
    [
        ("LONG", 100.0, 98.0),
        ("SHORT", 100.0, 102.0),
        ("LONG", 250.5, 250.5 - 250.5 * 0.02),
        ("SHORT", 250.5, 250.5 + 250.5 * 0.02),
    ],
)
def test_native_engine_creation_time_fallback_exposure_stop(
    direction: str, close: float, expected: float
) -> None:
    engine = _engine()
    engine._create_order(_signal_bar(close), 0, direction)

    assert len(engine.pending_orders) == 1
    assert engine.pending_orders[0].stop_loss is None
    assert engine.pending_orders[0]._sizing_stop_loss == pytest.approx(expected)


@pytest.mark.parametrize(
    ("direction", "reference_price", "expected_exposure"),
    [
        # |reference - fallback stop| * qty * pip / sizing equity
        ("LONG", 100.0, (100.0 * 0.02 * 2.0) / 100_000.0),
        ("SHORT", 100.0, (100.0 * 0.02 * 2.0) / 100_000.0),
        ("LONG", 250.5, (250.5 * 0.02 * 2.0) / 100_000.0),
        ("SHORT", 250.5, (250.5 * 0.02 * 2.0) / 100_000.0),
    ],
)
def test_native_engine_pending_order_exposure_fallback_stop(
    direction: str, reference_price: float, expected_exposure: float
) -> None:
    # The pending order's own trigger equals the reference price, so the
    # exposure estimate falls back to the percent stop -- its own call site.
    engine = _engine()
    engine.pending_orders.append(
        Order(
            symbol="T",
            direction=direction,
            order_type=OrderType.LIMIT,
            created_index=0,
            created_time="2024-01-01T00:00:00Z",
            quantity=2.0,
            limit_price=reference_price,
        )
    )

    assert engine._current_risk_exposure(reference_price) == pytest.approx(
        expected_exposure
    )


def _gap_frame(fill_open: float) -> pd.DataFrame:
    """Signal bar closes at 100, the next bar opens at ``fill_open``."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=3, freq="min", tz="UTC"),
            "open": [100.0, fill_open, fill_open],
            "high": [100.5, fill_open + 0.5, fill_open + 0.5],
            "low": [99.5, fill_open - 0.5, fill_open - 0.5],
            "close": [100.0, fill_open, fill_open],
        }
    )


def _no_stop_strategy(df: pd.DataFrame, params: dict):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["LONG", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


def _short_no_stop_strategy(df: pd.DataFrame, params: dict):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


@pytest.mark.parametrize(
    ("fill_open", "strategy", "expected"),
    [
        (120.0, _no_stop_strategy, 120.0 * 0.98),
        (85.0, _no_stop_strategy, 85.0 * 0.98),
        (120.0, _short_no_stop_strategy, 120.0 * 1.02),
        (85.0, _short_no_stop_strategy, 85.0 * 1.02),
    ],
)
def test_native_engine_fill_time_fallback_stop_re_derives_from_fill(
    fill_open: float, strategy, expected: float
) -> None:
    result = _engine(risk_per_trade_pct=0.01).run(_gap_frame(fill_open), strategy)

    assert len(result.trades) == 1
    assert result.trades[0].meta["sizing_stop_loss"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 4. The sizing function's full result (including today's hidden channels)
# ---------------------------------------------------------------------------


def test_sizing_returns_the_resolved_stop_without_hidden_channels() -> None:
    engine = RiskEngine(RiskConfig())
    signal = TradeSignal(
        action="buy",
        symbol="T",
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata={"current_positions": 0},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    # risk 100 / distance 2 = 50 units, capped by 0.2 * 10_000 / 100 = 20.
    assert sized.position_size == pytest.approx(20.0)
    assert sized.risk_amount == pytest.approx(100.0)
    assert sized.risk_percent == pytest.approx(0.01)
    assert sized.decision is not None
    assert sized.decision.stop_price_used == pytest.approx(98.0)
    assert sized.decision.risk_distance == pytest.approx(2.0)
    assert sized.decision.metadata["capped_by_notional"] is True
    # The full result is on the return value: resolved stop and signal id.
    assert sized.resolved_stop_loss == pytest.approx(98.0)
    assert sized.decision.signal_id == RiskEngine._signal_id(signal)
    assert sized.decision.signal_id.startswith("legacy:")
    # ... and the input signal is untouched (no hidden channels).
    assert signal.stop_loss is None
    assert "resolved_stop_loss" not in signal.metadata
    assert "_risk_legacy_signal_id" not in signal.metadata


def test_sizing_on_an_explicit_stop_leaves_the_stop_alone() -> None:
    engine = RiskEngine(RiskConfig())
    signal = TradeSignal(
        action="sell",
        symbol="T",
        entry_price=250.5,
        stop_loss=255.0,
        take_profit=None,
        timeframe="1h",
        metadata={"current_positions": 0},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    # risk 100 / distance 4.5 = 22.22 units, capped by 0.2 * 10_000 / 250.5.
    assert sized.position_size == pytest.approx(2_000.0 / 250.5)
    assert sized.decision is not None
    assert sized.decision.stop_price_used == pytest.approx(255.0)
    assert sized.decision.metadata["stop_source"] == "signal"
    assert signal.stop_loss == pytest.approx(255.0)
    assert "resolved_stop_loss" not in signal.metadata


# ---------------------------------------------------------------------------
# 5. The shared fallback-stop computation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "entry_price", "kwargs", "expected"),
    [
        ("buy", 100.0, {}, 98.0),
        ("long", 100.0, {}, 98.0),
        ("sell", 100.0, {}, 102.0),
        ("short", 250.5, {}, 250.5 + 250.5 * 0.02),
        ("LONG", 100.0, {"stop_distance": 7.5}, 92.5),
        ("SHORT", 100.0, {"stop_distance": 7.5}, 107.5),
        # An explicit non-positive distance falls back to the percent rule,
        # exactly like every call site did before the unify.
        ("LONG", 100.0, {"stop_distance": 0.0}, 98.0),
        ("LONG", 100.0, {"stop_distance": None}, 98.0),
        ("sell", 100.0, {"atr": 1.5}, 103.0),
        # An explicit distance wins over ATR.
        ("sell", 100.0, {"atr": 1.5, "stop_distance": 4.0}, 104.0),
    ],
)
def test_risk_engine_fallback_stop(
    action: str, entry_price: float, kwargs: dict, expected: float
) -> None:
    engine = RiskEngine(RiskConfig(fallback_stop_pct=0.02, default_sl_atr_multiplier=2.0))

    assert engine.fallback_stop(
        action=action, entry_price=entry_price, **kwargs
    ) == pytest.approx(expected)


@pytest.mark.parametrize("atr", ["abc", float("nan"), float("inf"), 0.0, -1.0])
def test_risk_engine_fallback_stop_rejects_unusable_atr(atr) -> None:
    engine = RiskEngine()
    with pytest.raises(ValueError, match="ATR value"):
        engine.fallback_stop(action="buy", entry_price=100.0, atr=atr)


def test_backtest_notional_ceiling_deliberately_differs_from_live() -> None:
    """The named backtest constant is not silently aligned with live.

    Improvement-run G03: the backtest engine sizes with 1.0 while live
    deployments default to 0.20; whether to align them is an open maintainer
    decision, so the divergence is pinned rather than changed.
    """
    from runner.engine_pkg.trading_engine import BACKTEST_MAX_NOTIONAL_PER_TRADE

    assert BACKTEST_MAX_NOTIONAL_PER_TRADE == 1.0
    assert RiskConfig().max_notional_per_trade == 0.20
