"""Cleanup 18-C slice 12 — live polling rate and N+1 balance access.

The accounts stream refetched balances per account every second per open
WebSocket. Balances are now fetched in one batched query and the poll rate is
a few seconds, not one.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_poll_rates_are_not_one_hertz():
    import viewer.main as viewer_main

    assert viewer_main._LIVE_WS_ACCOUNTS_POLL_SECONDS >= 5.0
    assert viewer_main._LIVE_WS_POSITIONS_POLL_SECONDS >= 3.0
    assert viewer_main._LIVE_WS_SIGNALS_POLL_SECONDS >= 3.0
    assert viewer_main._LIVE_WS_EXECUTIONS_POLL_SECONDS >= 3.0


def test_batched_balance_lookup_serves_all_accounts_in_one_query(monkeypatch):
    import viewer.main as viewer_main
    from shared.models import LiveBalanceRecord

    accounts = [
        {"id": "acc-1", "balance": 1.0, "equity": 1.0},
        {"id": "acc-2", "balance": 2.0, "equity": 2.0},
    ]
    queries: list[list[str]] = []

    record_a = LiveBalanceRecord(
        id=uuid4(),
        account_id="acc-1",
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        balance=10.0,
        equity=11.0,
    )
    record_b = LiveBalanceRecord(
        id=uuid4(),
        account_id="acc-2",
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        balance=20.0,
        equity=21.0,
    )

    def _batched(account_ids):
        queries.append(sorted(account_ids))
        return {"acc-1": record_a, "acc-2": record_b}

    monkeypatch.setattr(viewer_main, "get_latest_live_balances", _batched)
    monkeypatch.setattr(
        viewer_main, "_cached_live_accounts_payload", lambda *, force_refresh=False: accounts
    )

    payload = viewer_main._live_accounts_stream_payload()

    assert queries == [["acc-1", "acc-2"]], "one batched query, not one per account"
    by_id = {item["id"]: item for item in payload["accounts"]}
    assert by_id["acc-1"]["balance"] == 10.0
    assert by_id["acc-2"]["equity"] == 21.0
