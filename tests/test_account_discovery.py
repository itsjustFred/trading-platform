from __future__ import annotations

import asyncio
import os
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
    monkeypatch.setitem(
        __import__("sys").modules,
        "alpaca",
        SimpleNamespace(trading=SimpleNamespace(client=fake_module)),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "alpaca.trading", SimpleNamespace(client=fake_module)
    )
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
    error_text = (
        "RuntimeError('TRADING_ACCOUNT_IS_NOT_AUTHORIZED: Trading account is not authorized')"
    )
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


# ── moneyDigits: a genuine zero is not a missing field ────────────────────────────────────────
#
# `ProtoOATrader.moneyDigits` is a `uint32` **with presence**, so an unset field reads back as
# 0 - the same value the broker sends for an account with no minor units. The previous
# `int(getattr(trader, "moneyDigits", 2) or 2)` collapsed both onto 2, so a 0-decimal account's
# balance was reported 100x too small. These tests pin the two cases apart on the wire
# representation that actually produces them, not on a hand-made dict.


def _proto_trader(*, money_digits: int | None, balance: int):
    """A real `ProtoOATrader`, so `HasField` answers the way the broker's payload does."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrader

    trader = ProtoOATrader()
    trader.balance = balance
    if money_digits is not None:
        trader.moneyDigits = money_digits
    return trader


def test_a_broker_sent_zero_money_digits_means_no_minor_units():
    """The blocker: 0 decimals must not be read as 2.

    Balance 123456 with `moneyDigits=0` is 123456 currency units. The old collapse divided it
    by 100 and reported 1234.56.
    """
    trader = _proto_trader(money_digits=0, balance=123_456)

    assert account_discovery._trader_balance(trader) == pytest.approx(123_456.0), (
        "a broker-sent moneyDigits=0 was scaled by 100, so the balance is 100x too small"
    )


def test_two_money_digits_still_scale_by_a_hundred():
    trader = _proto_trader(money_digits=2, balance=123_456)

    assert account_discovery._trader_balance(trader) == pytest.approx(1_234.56)


def test_an_omitted_money_digits_keeps_the_conventional_default():
    """The other half of the pair: absent is not zero.

    An unset `moneyDigits` reads back as 0, so a value-only test cannot tell it from a genuine
    zero. `HasField` can, and this asserts the distinction survives.
    """
    trader = _proto_trader(money_digits=None, balance=123_456)

    assert trader.HasField("moneyDigits") is False
    assert trader.moneyDigits == 0
    assert account_discovery._trader_balance(trader) == pytest.approx(1_234.56), (
        "an omitted moneyDigits was honoured as 0, so the default no longer applies"
    )


def test_a_payload_without_presence_is_taken_at_its_word():
    """`SimpleNamespace` doubles and replay adapters have no `HasField`.

    There the attribute being present at all is the only signal available, so its value is
    taken as sent - including 0. A missing attribute still means "not sent".
    """
    assert account_discovery._trader_balance(
        SimpleNamespace(moneyDigits=0, balance=123_456)
    ) == pytest.approx(123_456.0)
    assert account_discovery._trader_balance(
        SimpleNamespace(moneyDigits=2, balance=123_456)
    ) == pytest.approx(1_234.56)
    assert account_discovery._trader_balance(SimpleNamespace(balance=123_456)) == pytest.approx(
        1_234.56
    ), "a payload that never carried moneyDigits must still fall back to the default"


@pytest.mark.parametrize("money_digits", [13, 20, -1, -5, True, "not a number"])
def test_an_unusable_money_digits_count_falls_back_to_two(money_digits):
    """A value outside the scaleable range is not a decimal count; the default applies."""
    trader = SimpleNamespace(moneyDigits=money_digits, balance=123_456)

    assert account_discovery._trader_balance(trader) == pytest.approx(1_234.56)


def test_the_adapter_balance_uses_the_same_resolution():
    """`get_balance` must agree with discovery, or the viewer and the daemon disagree.

    Both call `money_digits_from_trader`; this asserts the second call site behaves the same on
    the wire payload rather than relying on a source-level check.
    """
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter._connected = True
    adapter._authenticated = True

    class _TraderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0

    trader = _proto_trader(money_digits=0, balance=123_456)
    adapter._ProtoOATraderReq = _TraderReq
    adapter._ProtoOATraderRes = type("_TraderRes", (), {})

    response = adapter._ProtoOATraderRes()
    response.trader = trader

    async def _send_response(_request, **_kwargs):
        return response

    adapter._send_request = _send_response

    balance, equity = asyncio.run(adapter.get_balance())

    assert balance == pytest.approx(123_456.0)
    assert equity == pytest.approx(123_456.0)


def test_a_negative_login_is_refused_not_accepted():
    """`"-1".isdigit()` is False, so the old `isdigit() and int(...) <= 0` guard passed it.

    `traderLogin` is an int64, so a negative is an ordinary wire value and every downstream
    validator rejects it; this guard must too, or the bad value reaches them as a login.
    """
    assert account_discovery._ctrader_extract_login(SimpleNamespace(traderLogin=-1)) == ""
    assert account_discovery._ctrader_extract_login(SimpleNamespace(traderLogin=0)) == ""
    assert account_discovery._ctrader_extract_login(SimpleNamespace(traderLogin=46736452)) == (
        "46736452"
    )


def test_loading_ctrader_protocol_does_not_install_twisted_reactor():
    """Discovery must not leave an epoll reactor that breaks CTraderLiveAdapter later."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import sys
        import live_runner.account_discovery as d
        assert "twisted.internet.reactor" not in sys.modules, "import installed reactor"
        d._load_ctrader_protocol()
        assert "twisted.internet.reactor" not in sys.modules, "load installed reactor"
        assert "twisted.internet.epollreactor" not in sys.modules
        from twisted.internet import asyncioreactor
        import asyncio
        loop = asyncio.new_event_loop()
        asyncioreactor.install(loop)
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
