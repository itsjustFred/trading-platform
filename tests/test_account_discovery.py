from __future__ import annotations

from types import SimpleNamespace

import live_runner.account_discovery as account_discovery


def test_ctrader_account_name_prefers_resolved_account_label():
    counters: dict[tuple[str, bool], int] = {}
    name = account_discovery._ctrader_account_name(
        broker_name="Spotware",
        account_label="Fusion Markets Demo",
        is_demo=True,
        fallback_counters=counters,
    )

    assert name == "Fusion Markets Demo"
    assert counters == {}


def test_ctrader_account_name_fallback_uses_stable_order_per_broker_and_mode():
    counters: dict[tuple[str, bool], int] = {}
    first = account_discovery._ctrader_account_name(
        broker_name="",
        account_label="",
        is_demo=True,
        fallback_counters=counters,
    )
    second = account_discovery._ctrader_account_name(
        broker_name="",
        account_label="",
        is_demo=True,
        fallback_counters=counters,
    )
    live = account_discovery._ctrader_account_name(
        broker_name="",
        account_label="",
        is_demo=False,
        fallback_counters=counters,
    )

    assert first == "cTrader Demo #1"
    assert second == "cTrader Demo #2"
    assert live == "cTrader Live #1"


def test_ctrader_extract_account_label_ignores_numeric_identifier_fields():
    account_item = SimpleNamespace(name="123456", displayName="")
    trader_res = SimpleNamespace(
        trader=SimpleNamespace(brokerName="Fusion Markets", nickname="Fusion Markets Live #3")
    )

    resolved = account_discovery._ctrader_extract_account_label(
        *account_discovery._ctrader_message_name_sources(account_item),
        *account_discovery._ctrader_message_name_sources(trader_res),
        ignored_values={"123456"},
    )

    assert resolved == "Fusion Markets Live #3"
