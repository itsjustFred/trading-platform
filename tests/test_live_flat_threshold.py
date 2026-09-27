"""Regression tests for the live-position flat-state threshold (Sub-Issue #46, S1244).

The live position state machine must use an explicit flat threshold
(`LIVE_POSITION_FLAT_THRESHOLD`) instead of exact float-zero equality when
deciding whether a position is flat. Residual float dust (|qty| < threshold)
must be treated as flat: the position is reset and the remaining dust is not
carried into the next average-price calculation.
"""

from __future__ import annotations

import math

from viewer.main import LIVE_POSITION_FLAT_THRESHOLD, _calculate_realized_live_pnl


def _row(symbol: str, action: str, price: float, qty: float, minute: int):
    from datetime import datetime, timezone

    return {
        "symbol": symbol,
        "action": action,
        "actual_price": price,
        "filled_qty": qty,
        "execution_timestamp": datetime(2026, 1, 1, 10, minute, tzinfo=timezone.utc),
    }


def test_flat_threshold_is_exported_and_strictly_positive():
    assert LIVE_POSITION_FLAT_THRESHOLD > 0.0
    # Small enough that regular trading quantities are never swallowed by it.
    assert LIVE_POSITION_FLAT_THRESHOLD <= 1e-6


def test_exact_zero_opening_is_treated_as_flat():
    # First fill for a symbol: flat -> open. No realized PnL, no win/loss.
    total_pnl, wins, losses = _calculate_realized_live_pnl(
        [_row("AAPL", "buy", 100.0, 2.0, 1)]
    )
    assert total_pnl == 0.0
    assert (wins, losses) == (0, 0)


def test_dust_below_threshold_is_treated_as_flat_on_close():
    # Buy 1.0, sell 1.0 + 1e-9 dust. The residual must be treated as flat
    # instead of leaving a 1e-9 short position open.
    rows = [
        _row("AAPL", "buy", 100.0, 1.0, 1),
        _row("AAPL", "sell", 110.0, 1.0 + 1e-9, 2),
        _row("AAPL", "buy", 120.0, 1.0, 3),
    ]
    total_pnl, wins, losses = _calculate_realized_live_pnl(rows)

    # Realized PnL from the first close: (110 - 100) * 1.0
    assert math.isclose(total_pnl, 10.0, rel_tol=0, abs_tol=1e-9)
    assert (wins, losses) == (1, 0)


def test_dust_below_threshold_does_not_pollute_average_price():
    # Close with dust, then reopen: the new position must open at the fresh
    # price (state fully reset), not at a dust-contaminated average.
    rows = [
        _row("AAPL", "buy", 100.0, 1.0, 1),
        _row("AAPL", "sell", 90.0, 1.0 + 5e-10, 2),
        _row("AAPL", "buy", 50.0, 1.0, 3),
        _row("AAPL", "sell", 60.0, 1.0, 4),
    ]
    total_pnl, wins, losses = _calculate_realized_live_pnl(rows)

    # Second round trip must realize (60 - 50) * 1.0 with the position reset.
    assert math.isclose(total_pnl, -10.0 + 10.0, rel_tol=0, abs_tol=1e-9)
    assert (wins, losses) == (1, 1)


def test_quantity_above_threshold_still_treated_as_open_position():
    # Sell more than we hold by a non-dust amount: the leftover short must
    # survive as an open position (not reset to flat).
    rows = [
        _row("AAPL", "buy", 100.0, 1.0, 1),
        _row("AAPL", "sell", 110.0, 1.5, 2),
        _row("AAPL", "buy", 120.0, 0.5, 3),
    ]
    total_pnl, wins, losses = _calculate_realized_live_pnl(rows)

    # First close realizes (110 - 100) * 1.0 = 10; the remaining short 0.5
    # is closed by the buy at 120 realizing (120 - 110) * 0.5 = -5 loss.
    assert math.isclose(total_pnl, 10.0 - 5.0, rel_tol=0, abs_tol=1e-9)
    assert (wins, losses) == (1, 1)


def test_sub_threshold_residual_flip_via_opposite_fill():
    # A tiny opposite fill (below threshold) must not open a new position.
    rows = [
        _row("AAPL", "buy", 100.0, 1.0, 1),
        _row("AAPL", "sell", 110.0, 1.0, 2),
        _row("AAPL", "sell", 105.0, 1e-10, 3),
        _row("AAPL", "buy", 120.0, 1.0, 4),
    ]
    total_pnl, wins, losses = _calculate_realized_live_pnl(rows)

    # The dust sell must be ignored (no open position to reduce), and the
    # final buy opens a fresh long that is never closed: realized stays 10.
    assert math.isclose(total_pnl, 10.0, rel_tol=0, abs_tol=1e-9)
    assert (wins, losses) == (1, 0)


def test_stats_endpoint_payload_uses_flat_threshold_math(monkeypatch):
    """End-to-end: /api/live/stats must reflect dust-flat close semantics."""
    import uuid
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module
    from shared.models import LiveBalanceRecord

    account_id = "acc-flat"
    balances = [
        LiveBalanceRecord(
            id=uuid.uuid4(),
            account_id=account_id,
            timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            balance=1000.0,
            equity=1000.0,
        ),
    ]
    execution_rows = [
        _row("AAPL", "buy", 100.0, 1.0, 1),
        _row("AAPL", "sell", 110.0, 1.0 + 1e-9, 2),
    ]
    monkeypatch.setattr(
        viewer_main_module,
        "load_live_account_stats_inputs",
        lambda _account_id: (balances, execution_rows),
    )

    client = TestClient(viewer_main_module.app)
    response = client.get(f"/api/live/stats/{account_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["winning_trades"] == 1
    assert payload["losing_trades"] == 0
    assert math.isclose(payload["total_pnl"], 10.0, rel_tol=0, abs_tol=1e-9)
