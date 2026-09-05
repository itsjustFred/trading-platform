"""Cleanup 18-C slice 7 — live win rate counts closed trades, not executions.

`_calculate_realized_live_pnl` counts wins/losses per round-trip close but the
denominator was `len(execution_rows)`, so every partial fill deflated win_rate.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _row(symbol: str, action: str, price: float, qty: float, minute: int) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "actual_price": price,
        "filled_qty": qty,
        "execution_timestamp": datetime(2026, 1, 1, 10, minute, tzinfo=timezone.utc),
    }


def test_win_rate_denominator_is_closed_trades_not_executions(monkeypatch):
    import viewer.main as viewer_main

    # 3 executions: buy 1.0, buy 0.5 (partial add), sell 1.5 -> one closed trade.
    rows = [
        _row("AAPL", "buy", 100.0, 1.0, 0),
        _row("AAPL", "buy", 105.0, 0.5, 5),
        _row("AAPL", "sell", 110.0, 1.5, 10),
    ]
    monkeypatch.setattr(
        viewer_main, "load_live_account_stats_inputs", lambda _account_id: ([], rows)
    )

    payload = viewer_main._live_account_stats_payload("acc-1")

    assert payload["total_trades"] == 1, "one closed round trip, three executions"
    assert payload["winning_trades"] == 1
    assert payload["losing_trades"] == 0
    assert payload["win_rate"] == 100.0


def test_win_rate_counts_each_closed_round_trip(monkeypatch):
    import viewer.main as viewer_main

    rows = [
        # Round trip 1: win
        _row("AAPL", "buy", 100.0, 1.0, 0),
        _row("AAPL", "sell", 110.0, 1.0, 5),
        # Round trip 2: loss
        _row("MSFT", "buy", 200.0, 1.0, 10),
        _row("MSFT", "sell", 190.0, 1.0, 15),
        # Open position: not closed yet, must not count.
        _row("TSLA", "buy", 250.0, 1.0, 20),
    ]
    monkeypatch.setattr(
        viewer_main, "load_live_account_stats_inputs", lambda _account_id: ([], rows)
    )

    payload = viewer_main._live_account_stats_payload("acc-1")

    assert payload["total_trades"] == 2
    assert payload["winning_trades"] == 1
    assert payload["losing_trades"] == 1
    assert payload["win_rate"] == 50.0


def test_win_rate_zero_when_no_closed_trades(monkeypatch):
    import viewer.main as viewer_main

    rows = [_row("AAPL", "buy", 100.0, 1.0, 0)]
    monkeypatch.setattr(
        viewer_main, "load_live_account_stats_inputs", lambda _account_id: ([], rows)
    )

    payload = viewer_main._live_account_stats_payload("acc-1")

    assert payload["total_trades"] == 0
    assert payload["win_rate"] == 0.0
    assert payload["total_pnl"] == 0.0
