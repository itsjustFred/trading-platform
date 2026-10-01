from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
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


def test_a_missing_islive_flag_reads_as_demo_not_live(monkeypatch):
    """An absent `isLive` must fail toward demo. `getattr(..., False)` is that choice.

    The default decides which way an unanswerable question resolves, and this one
    resolves toward `is_demo=True`, i.e. the account is treated as paper. Flipping it
    to `True` leaves every collected test green, because no test builds an account
    item without the flag - so the safety direction of this default was unverified.

    The asymmetry is the point. An absent flag is *not* evidence of a live account,
    and a demo that silently trades real money is unrecoverable; a live account
    misreported as demo merely refuses to arm.
    """
    monkeypatch.setattr(account_discovery, "_request_ctrader_account_detail", lambda *_a, **_k: None)
    monkeypatch.setattr(account_discovery, "_load_ctrader_trader_details", lambda *_a, **_k: None)
    monkeypatch.setattr(account_discovery, "_ctrader_resolved_account_id", lambda _item: 12345)
    monkeypatch.setattr(
        account_discovery,
        "_ctrader_account_context",
        lambda *_a, **_k: SimpleNamespace(
            balance=100.0,
            trader_login="7",
            account_name_hint="",
            broker_name="cTrader",
            ignored_name_values=set(),
        ),
    )

    without_flag = SimpleNamespace()  # no `isLive` at all
    account = account_discovery._build_ctrader_live_account(
        "demo.ctraderapi.com", None, "tok", without_flag
    )
    assert account is not None, "the account was dropped, so this test proves nothing"
    assert account.is_demo is True, (
        "an absent isLive flag was read as a live account; the default must fail "
        "toward demo"
    )

    # And the explicit values still decide, so the default is not doing the work.
    monkeypatch.setattr(
        account_discovery, "_ctrader_host_matches_environment", lambda **_kw: True
    )
    assert (
        account_discovery._build_ctrader_live_account(
            "live.ctraderapi.com", None, "tok", SimpleNamespace(isLive=True)
        ).is_demo
        is False
    )
    assert (
        account_discovery._build_ctrader_live_account(
            "demo.ctraderapi.com", None, "tok", SimpleNamespace(isLive=False)
        ).is_demo
        is True
    )


def test_an_account_on_the_wrong_ctrader_host_is_dropped(monkeypatch):
    """A live account discovered on the demo host is a routing bug, and is dropped.

    The host says `live.` and the flag says demo, or the reverse. Keeping either
    would mean trading against an environment the credentials were not issued for -
    so the account is dropped and the mismatch is logged. Dropping the guard left
    every test green, since `_ctrader_host_matches_environment` is only pinned
    directly, never through the caller that acts on its answer.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        account_discovery,
        "_log_ctrader_host_environment_mismatch",
        lambda host, account_id, is_demo: calls.append(f"{host}:{account_id}:{is_demo}"),
    )
    monkeypatch.setattr(account_discovery, "_request_ctrader_account_detail", lambda *_a, **_k: None)
    monkeypatch.setattr(account_discovery, "_load_ctrader_trader_details", lambda *_a, **_k: None)
    monkeypatch.setattr(account_discovery, "_ctrader_resolved_account_id", lambda _item: 999)
    monkeypatch.setattr(
        account_discovery,
        "_ctrader_account_context",
        lambda *_a, **_k: SimpleNamespace(
            balance=1.0,
            trader_login=None,
            account_name_hint="",
            broker_name="cTrader",
            ignored_name_values=set(),
        ),
    )

    # A live account arriving on the demo host.
    assert (
        account_discovery._build_ctrader_live_account(
            "demo.ctraderapi.com", None, "tok", SimpleNamespace(isLive=True)
        )
        is None
    )
    assert calls == ["demo.ctraderapi.com:999:False"], (
        "the mismatch was not reported, so the drop would be silent: " + repr(calls)
    )

    # A demo account arriving on the live host, the other direction.
    calls.clear()
    assert (
        account_discovery._build_ctrader_live_account(
            "live.ctraderapi.com", None, "tok", SimpleNamespace(isLive=False)
        )
        is None
    )
    assert calls == ["live.ctraderapi.com:999:True"]


def test_alpaca_discovery_reports_the_configured_paper_mode(monkeypatch):
    """`is_demo` is the configured `paper` flag, for both settings.

    The file's docstring on `_iter_alpaca_credentials` states the reason: sharing the
    adapter's block parser keeps the discovered `paper` flag identical to the mode the
    runtime adapter is constructed with, so `is_demo` can never contradict the actual
    trading mode. Forcing it to `True` left every test green, because the one existing
    Alpaca discovery test passes `paper=True` and only asserts `broker_login`.

    This is the check that would catch a paper account being armed with live
    credentials, or the reverse - the contradiction the docstring warns about.
    """

    def _discover(paper: bool):
        class _FakeAccount:
            id = "acc-id"
            account_number = "broker-42"
            status = "ACTIVE"
            equity = 10.0
            cash = 10.0

        class _FakeTradingClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def get_account(self):
                return _FakeAccount()

        fake_module = SimpleNamespace(TradingClient=_FakeTradingClient)
        monkeypatch.setitem(sys.modules, "alpaca", SimpleNamespace(trading=SimpleNamespace(client=fake_module)))
        monkeypatch.setitem(sys.modules, "alpaca.trading", SimpleNamespace(client=fake_module))
        monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_module)
        return account_discovery.discover_alpaca_accounts("k", "s", paper)

    assert [a.is_demo for a in _discover(True)] == [True]
    assert [a.is_demo for a in _discover(False)] == [False], (
        "a live Alpaca account was discovered as demo, so the deployment could be "
        "armed against real money"
    )


def test_a_failed_verification_write_does_not_abort_discovery(monkeypatch):
    """The write is best-effort: discovery still returns, and the account stays unverified.

    The docstring is explicit that failure is logged and swallowed so discovery
    returns the account list anyway, leaving the account unverified and therefore
    fail-closed until the next successful discovery. Narrowing the `except Exception`
    to a single type leaves every test green, so a write failure would instead
    propagate out of discovery - which is how a headless deployment loses its account
    list entirely.
    """
    monkeypatch.setattr(
        account_discovery,
        "record_live_account_demo_verification",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db is locked")),
    )

    assert account_discovery.record_discovered_account_verification(
        "acc-1", is_demo=True, ctid="1", broker_login="7"
    ) is None

    # And it is not recorded, so the account stays unverified rather than looking fine.
    recorded: list[object] = []
    monkeypatch.setattr(
        account_discovery,
        "record_live_account_demo_verification",
        lambda *a, **k: recorded.append((a, k)),
    )
    account_discovery.record_discovered_account_verification(
        "acc-1", is_demo=True, ctid="1", broker_login="7"
    )
    assert len(recorded) == 1


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
    acc_indexes = _alpaca_acc_indexes(typo_env)
    with pytest.raises(ValueError, match="ALPACA_PAPER_ACC4"):
        _alpaca_credential_blocks(typo_env, acc_indexes)
    # Discovery delegates to the adapter parser (asserted above on the valid
    # blocks), so refusing here follows from the same strict parse.

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


def _tmp_accounts_env(content: str) -> Path:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    return Path(path)


def test_discover_configured_accounts_skips_block_with_invalid_bool(monkeypatch):
    """A typo'd ALPACA_PAPER_ACC{N} must skip that block with a warning, not
    abort the whole discovery run (which would also drop cTrader accounts)."""
    monkeypatch.setenv("CTRADER_APP_ID", "app")
    monkeypatch.setenv("CTRADER_APP_SECRET", "secret")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "token")
    env_path = _tmp_accounts_env(
        "ALPACA_API_KEY_ACC1=good-key\n"
        "ALPACA_SECRET_KEY_ACC1=good-secret\n"
        "ALPACA_PAPER_ACC1=false\n"
        "ALPACA_API_KEY_ACC2=bad-key\n"
        "ALPACA_SECRET_KEY_ACC2=bad-secret\n"
        "ALPACA_PAPER_ACC2=ture\n"
    )

    async def _discover(app_id, app_secret, access_token):
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

    accounts = asyncio.run(account_discovery.discover_configured_accounts(accounts_env=env_path))

    # The cTrader account survives; the typo'd block is skipped with a warning
    # instead of aborting the whole discovery run.
    assert [account.id for account in accounts if account.broker == "ctrader"] == ["47644617"]


def test_iter_alpaca_credentials_safe_keeps_valid_sibling_blocks(monkeypatch):
    """A typo'd ALPACA_PAPER_ACC{N} skips only that block: the other indexed
    blocks must still be enumerated, not silently dropped together with it."""
    env = {
        "ALPACA_API_KEY_ACC1": "good-key",
        "ALPACA_SECRET_KEY_ACC1": "good-secret",
        "ALPACA_PAPER_ACC1": "false",
        "ALPACA_API_KEY_ACC2": "bad-key",
        "ALPACA_SECRET_KEY_ACC2": "bad-secret",
        "ALPACA_PAPER_ACC2": "ture",
        "ALPACA_API_KEY_ACC3": "also-good",
        "ALPACA_SECRET_KEY_ACC3": "also-good-secret",
        "ALPACA_ENDPOINT_ACC3": "https://paper-api.alpaca.markets/v2",
    }

    discovered = account_discovery._iter_alpaca_credentials_safe(env)

    assert discovered == [
        {"key": "ACC1", "api_key": "good-key", "secret_key": "good-secret", "paper": False},
        {
            "key": "ACC3",
            "api_key": "also-good",
            "secret_key": "also-good-secret",
            "paper": True,
        },
    ]


def test_discover_configured_accounts_uses_shared_ctrader_credentials(monkeypatch):
    """The shared (non-indexed) cTrader credentials must be the ones used.

    Hermetic by construction: discovery reads the accounts-env FILE with
    file-first precedence and deliberately ignores ``os.environ``, so the test
    must pass an explicit tmp env file. Relying on ``monkeypatch.setenv`` here
    used to fall through to the real gitignored ``.accounts.env`` and print
    live cTrader credentials into the pytest assertion diff.
    """
    env_path = _tmp_accounts_env(
        "CTRADER_APP_ID=app\n"
        "CTRADER_APP_SECRET=secret\n"
        "CTRADER_ACCESS_TOKEN=token\n"
    )
    monkeypatch.setattr(account_discovery, "_iter_alpaca_credentials_safe", lambda _env: [])
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

    accounts = asyncio.run(
        account_discovery.discover_configured_accounts(accounts_env=env_path)
    )

    # Assert identity of the credentials, never their values: comparing raw
    # secrets would echo them into CI logs on failure.
    assert len(captured) == 1, "expected exactly one cTrader discovery call"
    used_app_id, used_app_secret, used_access_token = captured[0]
    assert used_app_id == "app"
    assert used_app_secret == "secret"
    assert used_access_token == "token"
    assert [account.id for account in accounts] == ["47644617"]
