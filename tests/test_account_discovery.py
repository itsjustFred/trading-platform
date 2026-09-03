from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import live_runner.account_discovery as account_discovery
from live_runner.alpaca_adapter import _alpaca_acc_indexes, _alpaca_credential_blocks
from shared.models import LiveAccount


def test_ctrader_account_name_prefers_resolved_account_label():
    name = account_discovery._ctrader_account_name(
        account_id=10105152,
        account_label="Fusion Markets Demo",
    )

    assert name == "Fusion Markets Demo"


def test_ctrader_account_name_fallback_uses_account_id():
    resolved = account_discovery._ctrader_account_name(account_id=10105152, account_label="")
    assert resolved == "cTrader 10105152"


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


def test_message_to_debug_fields_extracts_scalar_and_repeated_values():
    class _FieldDescriptor:
        LABEL_REPEATED = 3

        def __init__(self, name: str, label: int) -> None:
            self.name = name
            self.label = label

    class _Message:
        def ListFields(self):
            return [
                (_FieldDescriptor("ctidTraderAccountId", 1), 10105152),
                (_FieldDescriptor("permissions", _FieldDescriptor.LABEL_REPEATED), [1, 2]),
            ]

    payload = account_discovery._message_to_debug_fields(_Message())
    assert payload["ctidTraderAccountId"] == "10105152"
    assert payload["permissions"] == ["1", "2"]


def test_discover_alpaca_accounts_sets_broker_login(monkeypatch):
    class _FakeAccount:
        id = "acc-id"
        account_number = "broker-42"
        status = "ACTIVE"
        equity = 1234.56
        cash = 1234.56

    class _FakeTradingClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_account(self):
            return _FakeAccount()

    fake_module = SimpleNamespace(TradingClient=_FakeTradingClient)
    monkeypatch.setitem(__import__("sys").modules, "alpaca", SimpleNamespace(trading=SimpleNamespace(client=fake_module)))
    monkeypatch.setitem(__import__("sys").modules, "alpaca.trading", SimpleNamespace(client=fake_module))
    monkeypatch.setitem(__import__("sys").modules, "alpaca.trading.client", fake_module)

    accounts = account_discovery.discover_alpaca_accounts("k", "s", True)
    assert len(accounts) == 1
    assert accounts[0].broker_login == "broker-42"


def test_ctrader_host_matches_environment_filters_demo_and_live_hosts():
    assert account_discovery._ctrader_host_matches_environment(
        host="demo.ctraderapi.com",
        is_live=False,
    )
    assert not account_discovery._ctrader_host_matches_environment(
        host="demo.ctraderapi.com",
        is_live=True,
    )
    assert account_discovery._ctrader_host_matches_environment(
        host="live.ctraderapi.com",
        is_live=True,
    )
    assert not account_discovery._ctrader_host_matches_environment(
        host="live.ctraderapi.com",
        is_live=False,
    )


def test_is_expected_ctrader_host_route_error_detects_environment_mismatch():
    error_text = "RuntimeError('CANT_ROUTE_REQUEST: No environment connection')"
    assert account_discovery._is_expected_ctrader_host_route_error(error_text)


def test_is_expected_ctrader_host_route_error_ignores_unrelated_errors():
    error_text = "RuntimeError('UNAUTHORIZED: invalid token')"
    assert not account_discovery._is_expected_ctrader_host_route_error(error_text)


def test_is_ctrader_account_not_authorized_error_detects_phrase():
    error_text = "RuntimeError('TRADING_ACCOUNT_IS_NOT_AUTHORIZED: Trading account is not authorized')"
    assert account_discovery._is_ctrader_account_not_authorized_error(error_text)


def test_is_ctrader_account_not_authorized_error_ignores_other_errors():
    error_text = "RuntimeError('TIMEOUT: endpoint unavailable')"
    assert not account_discovery._is_ctrader_account_not_authorized_error(error_text)


def test_iter_alpaca_credentials_share_adapter_block_semantics():
    """B-12: discovery and the runtime adapter must agree on how a credential
    block is parsed, so discovered is_demo matches the paper flag the adapter
    is actually constructed with.

    Per block: ALPACA_ENDPOINT_ACC{N} containing 'paper' wins, then
    ALPACA_PAPER_ACC{N}, then default True (the adapter's paper default).
    """
    env = {
        "ALPACA_API_KEY_ACC1": "k1",
        "ALPACA_SECRET_KEY_ACC1": "s1",
        "ALPACA_PAPER_ACC1": "false",
        "ALPACA_API_KEY_ACC2": "k2",
        "ALPACA_SECRET_KEY_ACC2": "s2",
        "ALPACA_ENDPOINT_ACC2": "https://paper-api.alpaca.markets/v2",
        "ALPACA_API_KEY_ACC3": "k3",
        "ALPACA_SECRET_KEY_ACC3": "s3",
    }

    discovered = account_discovery._iter_alpaca_credentials(env)
    indexes = _alpaca_acc_indexes(env)
    blocks = _alpaca_credential_blocks(env, indexes)

    by_key = {block["key"]: block for block in discovered}
    by_adapter_key = {f"ACC{index}": block for index, block in zip(indexes, blocks)}

    assert by_key["ACC1"]["paper"] is False
    assert by_adapter_key["ACC1"]["paper"] is False
    assert by_key["ACC2"]["paper"] is True
    assert by_adapter_key["ACC2"]["paper"] is True
    # Unset paper flag: both default to paper.
    assert by_key["ACC3"]["paper"] is True
    assert by_adapter_key["ACC3"]["paper"] is True
    for block in discovered:
        adapter_block = by_adapter_key[block["key"]]
        assert block["api_key"] == adapter_block["api_key"]
        assert block["secret_key"] == adapter_block["secret_key"]
        assert block["paper"] == adapter_block["paper"]

    # Typo'd paper flag: both must refuse, not silently pick live.
    typo_env = {
        "ALPACA_API_KEY_ACC4": "k4",
        "ALPACA_SECRET_KEY_ACC4": "s4",
        "ALPACA_PAPER_ACC4": "tru",
    }
    with pytest.raises(ValueError, match="ALPACA_PAPER_ACC4"):
        account_discovery._iter_alpaca_credentials(typo_env)
    with pytest.raises(ValueError, match="ALPACA_PAPER_ACC4"):
        _alpaca_credential_blocks(typo_env, _alpaca_acc_indexes(typo_env))

    for block in discovered:
        adapter_block = by_adapter_key[block["key"]]
        assert block["api_key"] == adapter_block["api_key"]
        assert block["secret_key"] == adapter_block["secret_key"]
        assert block["paper"] == adapter_block["paper"]


def test_iter_alpaca_credentials_default_block_falls_back_to_adapter_default_pair():
    """With no ACC{N} blocks, discovery falls back to the default pair with the
    same default-paper semantics the adapter uses."""
    discovered = account_discovery._iter_alpaca_credentials(
        {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}
    )

    assert discovered == [
        {"key": "DEFAULT", "api_key": "k", "secret_key": "s", "paper": True}
    ]

    with pytest.raises(ValueError, match="ALPACA_PAPER"):
        account_discovery._iter_alpaca_credentials(
            {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "wahr"}
        )


def test_discover_configured_accounts_uses_shared_ctrader_credentials(monkeypatch):
    monkeypatch.setenv("CTRADER_APP_ID", "app")
    monkeypatch.setenv("CTRADER_APP_SECRET", "secret")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "token")
    monkeypatch.setattr(account_discovery, "_iter_alpaca_credentials", lambda _env: [])
    captured: list[tuple[str, str, str]] = []

    async def _discover(app_id, app_secret, access_token):
        captured.append((app_id, app_secret, access_token))
        return [
            LiveAccount(
                id="47644617",
                name="cTrader Demo",
                broker="ctrader",
                is_demo=True,
                ctid="47644617",
            )
        ]

    monkeypatch.setattr(account_discovery, "discover_ctrader_accounts", _discover)

    accounts = asyncio.run(account_discovery.discover_configured_accounts())

    assert captured == [("app", "secret", "token")]
    assert [account.id for account in accounts] == ["47644617"]
