"""Tests for risk consistency between backtest and live engines."""
from datetime import datetime

import pytest

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.live import size_live_signal
from risk.models import TradeSignal
from shared.models import LiveSignal


def test_risk_engine_and_live_use_same_defaults() -> None:
    """Test that both engines use same RiskConfig defaults."""
    engine = RiskEngine()
    live_config = RiskConfig()

    assert engine.config.max_risk_per_trade == live_config.max_risk_per_trade
    assert engine.config.max_total_exposure == live_config.max_total_exposure
    assert engine.config.max_positions == live_config.max_positions
    assert engine.config.default_sl_atr_multiplier == live_config.default_sl_atr_multiplier
    assert engine.config.fallback_stop_pct == live_config.fallback_stop_pct


@pytest.mark.parametrize(
    ("action", "entry_price", "stop_loss", "take_profit", "pip_value", "expected_size"),
    [
        # risk 1% of 100000 = 1000; buy: 1000/(2*10)=50 shares, notional 5000
        # < cap -> uncapped; sell: 1000/(5*10)=20 shares.
        ("buy", 100.0, 98.0, 110.0, 10.0, 50.0),
        ("sell", 100.0, 105.0, 90.0, 10.0, 20.0),
    ],
)
def test_risk_engine_and_live_size_match_for_valid_signals(
    action: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    pip_value: float,
    expected_size: float,
) -> None:
    """Parametrized parity test for identical valid inputs."""
    engine = RiskEngine(RiskConfig(max_notional_per_trade=1.0))
    backtest_signal = TradeSignal(
        action=action,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        symbol="EURUSD",
        timeframe="1h",
    )
    live_signal = LiveSignal(
        deployment_id="test-deployment",
        symbol="EURUSD",
        action=action,
        theoretical_price=entry_price,
        timestamp=datetime.now(),
        metadata={
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
    )

    backtest_result = engine.calculate_position_size(
        signal=backtest_signal,
        account_balance=100000.0,
        pip_value=pip_value,
        current_exposure=0.0,
    )
    live_result = size_live_signal(
        live_signal,
        account_balance=100000.0,
        config=RiskConfig(max_notional_per_trade=1.0),
        pip_value=pip_value,
    )

    assert backtest_result.position_size == pytest.approx(expected_size)
    assert live_result.position_size == pytest.approx(expected_size)
    assert live_result.risk_amount == pytest.approx(1000.0)
    assert live_result.risk_percent == pytest.approx(0.01)


def test_live_size_rejects_wrong_stop_direction() -> None:
    """Test that live sizing rejects wrong stop direction."""
    live_signal = LiveSignal(
        deployment_id="test-deployment",
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(),
        metadata={
            "enforce_stop_direction": True,
            "entry_price": 100.0,
            "stop_loss": 105.0,  # Wrong direction for LONG
            "take_profit": 110.0,
        },
    )

    result = size_live_signal(live_signal, account_balance=100000.0)

    assert result.position_size == 0
    assert result.risk_amount == 0
    assert result.risk_percent == 0


def test_backtest_engine_allows_wrong_stop_direction() -> None:
    """Test that backtest engine allows wrong stop direction by default."""
    engine = RiskEngine()
    signal = TradeSignal(
        action="buy",
        entry_price=100.0,
        stop_loss=105.0,  # Wrong direction for LONG
        take_profit=110.0,
        symbol="EURUSD",
        timeframe="1h",
    )

    result = engine.calculate_position_size(
        signal=signal,
        account_balance=100000.0,
        pip_value=10.0,
        current_exposure=0.0,
    )

    assert result.position_size > 0
    assert result.risk_amount > 0


def _stop_less_pair(
    action: str,
    atr: float | str | None,
    entry_price: float = 100.0,
    take_profit: float = 120.0,
) -> tuple[TradeSignal, LiveSignal]:
    """One stop-less signal, expressed for both paths from a single description.

    The backtest path takes a `TradeSignal`, whose `action` accepts buy/sell/long/
    short. The live path takes a `LiveSignal`, a pydantic model whose `action` is
    only `buy` or `sell`. The matrix therefore stays within the intersection, and
    that asymmetry is itself worth knowing: a long/short spelling that the backtest
    accepts cannot reach the live path at all.
    """
    atr_metadata: dict = {} if atr is None else {"atr": atr}
    backtest = TradeSignal(
        action=action,
        entry_price=entry_price,
        stop_loss=None,
        take_profit=take_profit,
        symbol="EURUSD",
        timeframe="1h",
        metadata=dict(atr_metadata),
    )
    live = LiveSignal(
        deployment_id="test-deployment",
        symbol="EURUSD",
        action=action,
        theoretical_price=entry_price,
        timestamp=datetime.now(),
        metadata={"entry_price": entry_price, "take_profit": take_profit, **atr_metadata},
    )
    return backtest, live


@pytest.mark.parametrize(
    ("action", "atr", "expected_stop"),
    [
        # No ATR -> percent-of-price fallback: 100.0 * 0.02 = 2.0 of distance.
        ("buy", None, 98.0),
        ("sell", None, 102.0),
        # ATR present -> atr * default_sl_atr_multiplier (2.0).
        ("buy", 2.0, 96.0),
        ("sell", 2.0, 104.0),
        ("buy", 0.5, 99.0),
        ("sell", 0.5, 101.0),
        ("buy", 1.25, 97.5),
        ("sell", 1.25, 102.5),
    ],
)
def test_atr_fallback_is_computed_identically_in_both_paths(
    action: str,
    atr: float | None,
    expected_stop: float,
) -> None:
    """The third case `docs/live-risk-consistency.md` asks for, which was unpinned.

    A stop-less signal is the only case where the fallback fires, and the fallback is
    what decides the position size: the risk distance comes from the stop. So if the
    two paths computed it differently, every stop-less backtest would be a claim
    about a sizing that live trading does not use - and the suite would not notice,
    because the two paths share `RiskEngine` and nothing asserted they agreed.

    The expected stop is asserted explicitly rather than only comparing the paths to
    each other. Parity alone is satisfiable by both being wrong the same way: if the
    ATR branch were dead, both would compute the percent fallback and the comparison
    would still pass.
    """
    backtest_signal, live_signal = _stop_less_pair(action, atr)
    engine = RiskEngine(RiskConfig(max_notional_per_trade=1.0))

    backtest_result = engine.calculate_position_size(
        signal=backtest_signal,
        account_balance=100000.0,
        pip_value=10.0,
        current_exposure=0.0,
    )
    live_result = size_live_signal(
        live_signal,
        account_balance=100000.0,
        config=RiskConfig(max_notional_per_trade=1.0),
        pip_value=10.0,
    )

    # Both paths record the realised stop under the same key, because the daemon
    # reads `resolved_stop_loss` back out of signal metadata. If either stopped
    # writing it, the operator would lose the realised stop on a fallback order.
    backtest_stop = backtest_signal.metadata.get("resolved_stop_loss")
    live_stop = live_signal.metadata.get("resolved_stop_loss")

    assert backtest_stop == pytest.approx(expected_stop), (
        f"the backtest fallback resolved to {backtest_stop}, not {expected_stop}"
    )
    assert live_stop == pytest.approx(expected_stop), (
        f"the live fallback resolved to {live_stop}, not {expected_stop} - the two "
        "paths compute a different stop, so a backtest does not predict live sizing"
    )
    assert backtest_stop == pytest.approx(live_stop)
    assert backtest_result.position_size == pytest.approx(live_result.position_size)
    assert backtest_result.risk_amount == pytest.approx(live_result.risk_amount)
    assert backtest_result.risk_percent == pytest.approx(live_result.risk_percent)


@pytest.mark.parametrize(
    "atr",
    [0.0, -1.0, float("nan"), float("inf"), "abc"],
    ids=["zero", "negative", "nan", "inf", "unparseable"],
)
def test_a_bad_atr_is_refused_by_both_paths(atr: float | str) -> None:
    """Parity on the *refusal* is the half that matters, and it is the easier one to lose.

    The fallback guards against an uninterpretable stop distance: NaN, a
    non-positive ATR and a non-numeric ATR all raise rather than being healed. If one
    path healed and the other raised, the healing path would size a position against
    a stop distance that was never meaningful - and, unlike a raised error, it would
    look like a successful trade.

    Note what this test does and does not pin. It pins the *behaviour* - a bad ATR is
    refused - not the mechanism. Disabling the ATR-specific guard leaves the suite
    green, because a second, independent guard on the resulting distance catches the
    same inputs. That redundancy is deliberate and is the reason: the pin would still
    fail if either path stopped refusing altogether, and it would fail if the paths
    diverged, which is the case the contract is about.
    """
    backtest_signal, live_signal = _stop_less_pair("buy", atr)
    engine = RiskEngine(RiskConfig(max_notional_per_trade=1.0))

    with pytest.raises(ValueError):
        engine.calculate_position_size(
            signal=backtest_signal,
            account_balance=100000.0,
            pip_value=10.0,
            current_exposure=0.0,
        )
    with pytest.raises(ValueError):
        size_live_signal(
            live_signal,
            account_balance=100000.0,
            config=RiskConfig(max_notional_per_trade=1.0),
            pip_value=10.0,
        )


def test_the_fallback_distance_follows_the_configured_multiplier() -> None:
    """The multiplier and the percent fallback are the two inputs the contract names.

    Asserting both, with a config that differs from the defaults, is what stops this
    from passing because the paths happen to agree on the default values alone.
    """
    config = RiskConfig(default_sl_atr_multiplier=3.0, fallback_stop_pct=0.05)
    engine = RiskEngine(config)

    with_atr, _ = _stop_less_pair("buy", 1.0)
    engine.calculate_position_size(
        signal=with_atr, account_balance=100000.0, pip_value=10.0, current_exposure=0.0
    )
    assert with_atr.metadata["resolved_stop_loss"] == pytest.approx(100.0 - 3.0)

    without_atr, _ = _stop_less_pair("buy", None)
    engine.calculate_position_size(
        signal=without_atr, account_balance=100000.0, pip_value=10.0, current_exposure=0.0
    )
    assert without_atr.metadata["resolved_stop_loss"] == pytest.approx(100.0 - 5.0)
