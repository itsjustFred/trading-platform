from datetime import datetime, timezone

from risk.live import size_live_signal
from shared.models import LiveSignal


def _signal(metadata: dict):
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def test_size_live_signal_sets_qty_for_valid_risk():
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "timeframe": "1m",
            "pip_value": 1.0,
            "current_exposure": 0.0,
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    assert sized.position_size > 0
    assert signal.metadata["qty"] == sized.position_size
    assert signal.metadata["risk_amount"] == sized.risk_amount


def test_size_live_signal_blocks_when_max_positions_reached():
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "timeframe": "1m",
            "max_positions": 1,
            "current_positions": 1,
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    assert sized.position_size == 0.0
    assert signal.metadata["qty"] == 0.0

