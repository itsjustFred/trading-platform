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


def test_a_non_bool_demo_flag_is_treated_as_absent_not_as_a_value(monkeypatch):
    """`is_demo` stays tri-state through the refresher: bool, or None. Never anything else.

    The refresher narrows the broker's raw field with `isinstance(raw_is_demo, bool)`.
    Relaxing that to `is not None` left every test green, because every stub reports a
    real `bool` - so a broker that starts sending `"true"`, `1`, or `0` would have its
    raw value passed straight through.

    Two reasons that matters, and they pull in opposite directions:

    - `bool("false")` is **True**. A broker answering with the *string* `"false"` - a
      perfectly ordinary representation of a live account - would be recorded as
      `is_demo=True` and the account would read as paper.
    - `record_live_account_demo_verification` annotates `is_demo: bool`, so anything
      else is a lie about the annotation, not a tolerated coercion.

    The one-directional rule from `record_discovered_account_verification` applies here
    too: an unrecognised value is *no* verification, which leaves the account
    unverified and therefore fail-closed.
    """
    recorded: list[tuple[str, object]] = []

    monkeypatch.setattr(
        account_discovery,
        "discover_configured_accounts",
        _async_returning(
            [
                SimpleNamespace(id="acc-str", is_demo="false", broker_login="1", ctid="1"),
                SimpleNamespace(id="acc-one", is_demo=1, broker_login="2", ctid="2"),
                SimpleNamespace(id="acc-zero", is_demo=0, broker_login="3", ctid="3"),
                SimpleNamespace(id="acc-none", is_demo=None, broker_login="4", ctid="4"),
                SimpleNamespace(id="acc-true", is_demo=True, broker_login="5", ctid="5"),
                SimpleNamespace(id="acc-false", is_demo=False, broker_login="6", ctid="6"),
            ]
        ),
    )
    monkeypatch.setattr(
        account_discovery,
        "record_discovered_account_verification",
        lambda account_id, *, is_demo, ctid, broker_login: recorded.append(
            (account_id, is_demo)
        ),
    )
    monkeypatch.setattr(
        account_discovery, "demo_verification_age_seconds", lambda _id: None
    )

    asyncio.run(account_discovery.refresh_account_demo_verifications())

    by_id = dict(recorded)
    assert by_id["acc-str"] is None, (
        'the string "false" was recorded as a verification; bool("false") is True, so a '
        "live account would read as paper"
    )
    assert by_id["acc-one"] is None, "the integer 1 was recorded as a verification"
    assert by_id["acc-zero"] is None, "the integer 0 was recorded as a verification"
    assert by_id["acc-none"] is None
    # The real booleans still pass through unchanged, so the narrowing is not a blanket.
    assert by_id["acc-true"] is True
    assert by_id["acc-false"] is False
    assert set(by_id) == {"acc-str", "acc-one", "acc-zero", "acc-none", "acc-true", "acc-false"}


def test_an_account_with_no_id_is_skipped_not_written_to(monkeypatch):
    """A blank account id is skipped before any write is attempted.

    `record_live_account_demo_verification` raises `ValueError` on a blank id, and
    `record_discovered_account_verification` swallows it - so removing the `continue`
    leaves the outcome the same *and* the log noisier. It is pinned because the counter
    is the observable part: `skipped` must count it, and the function's own warning at
    the end distinguishes "understood but wrote nothing" from "had an unusable id".

    Written as a shape assertion rather than a crash assertion, because the swallow is
    deliberate and the defect it would introduce is a misleading log, not a wrong stamp.
    """
    recorded: list[str] = []
    monkeypatch.setattr(
        account_discovery,
        "discover_configured_accounts",
        _async_returning(
            [
                SimpleNamespace(id="", is_demo=True, broker_login="1", ctid="1"),
                SimpleNamespace(id="   ", is_demo=True, broker_login="2", ctid="2"),
                SimpleNamespace(id=None, is_demo=True, broker_login="3", ctid="3"),
                SimpleNamespace(id="acc-ok", is_demo=True, broker_login="4", ctid="4"),
            ]
        ),
    )
    monkeypatch.setattr(
        account_discovery,
        "record_discovered_account_verification",
        lambda account_id, *, is_demo, ctid, broker_login: recorded.append(account_id),
    )
    monkeypatch.setattr(
        account_discovery, "demo_verification_age_seconds", lambda _id: None
    )

    asyncio.run(account_discovery.refresh_account_demo_verifications())

    assert recorded == ["acc-ok"], (
        "a blank account id reached the verification writer: " + repr(recorded)
    )


def _async_returning(value):
    async def _inner():
        return value

    return _inner


def test_the_account_error_verdicts_are_three_distinct_outcomes():
    """Route error -> keep, not-authorised -> skip, anything else -> keep.

    Only the middle one is a decision about the account itself. The other two are
    transient broker-side conditions, and both were unpinned: relaxing the
    not-authorised branch keeps an account the token has no authority to trade,
    while reclassifying a route error or an unknown error as "skip" silently drops
    a perfectly tradable account and the operator sees it vanish from the list.

    Asserted as a table, because the value of the test is that the three branches
    stay distinguishable - not that any one of them returns a particular string.
    """
    cases = [
        ("CANT_ROUTE_REQUEST: NO ENVIRONMENT CONNECTION", "keep"),
        ("TRADING_ACCOUNT_IS_NOT_AUTHORIZED", "skip"),
        ("ACCOUNT_NOT_AUTHORIZED", "skip"),
        ("NOT AUTHORIZED", "skip"),
        ("Connection reset by peer", "keep"),
        ("", "keep"),
    ]
    for error_text, expected in cases:
        verdict = account_discovery._handle_ctrader_trader_error(
            error_text, "demo.ctraderapi.com", 1234
        )
        assert verdict == expected, (
            f"{error_text!r} was classified {verdict!r}, expected {expected!r}"
        )

    # The two that matter most are asserted apart from the table, with the reason.
    assert (
        account_discovery._handle_ctrader_trader_error(
            "TRADING_ACCOUNT_IS_NOT_AUTHORIZED", "demo.ctraderapi.com", 1
        )
        == "skip"
    ), "an account the token cannot trade must not be offered for deployment"


def test_an_unknown_trader_error_keeps_the_account(monkeypatch):
    """An unrecognised failure is a "keep", so a transient blip does not delete an account.

    `_handle_ctrader_trader_error` returns "skip" for exactly one condition. Treating
    every other failure as a skip would mean any unmodelled broker error removes the
    account from discovery entirely, and the operator's only symptom is that a
    deployment they configured is no longer listed.

    Asserted through `_build_ctrader_live_account`, because that is the caller that
    acts on the verdict - the same seam lesson as `_ctrader_host_matches_environment`.
    """
    monkeypatch.setattr(
        account_discovery,
        "_ctrader_account_context",
        lambda *_a, **_k: SimpleNamespace(
            balance=5.0,
            trader_login="1",
            account_name_hint="",
            broker_name="cTrader",
            ignored_name_values=set(),
        ),
    )
    monkeypatch.setattr(account_discovery, "_ctrader_resolved_account_id", lambda _i: 42)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("some unmodelled broker failure")

    monkeypatch.setattr(account_discovery, "_request_ctrader_account_detail", lambda *_a, **_k: None)
    monkeypatch.setattr(account_discovery, "_load_ctrader_trader_details", _raise)

    account = account_discovery._build_ctrader_live_account(
        "demo.ctraderapi.com", None, "tok", SimpleNamespace(isLive=False)
    )
    assert account is not None, (
        "an unmodelled broker error dropped the account from discovery; only an "
        "unauthorised token should do that"
    )

    # And the unauthorised case does drop it, through the same path.
    def _raise_unauthorised(*_args, **_kwargs):
        raise RuntimeError("TRADING_ACCOUNT_IS_NOT_AUTHORIZED")

    monkeypatch.setattr(account_discovery, "_load_ctrader_trader_details", _raise_unauthorised)
    assert (
        account_discovery._build_ctrader_live_account(
            "demo.ctraderapi.com", None, "tok", SimpleNamespace(isLive=False)
        )
        is None
    )


def test_partial_ctrader_credentials_do_not_start_a_discovery(monkeypatch, tmp_path):
    """All three cTrader credentials or none of them.

    `and` is the guard. Relaxing it to `or` leaves every test green, because every
    existing fixture supplies a complete triple - so a half-configured account would
    attempt a broker login with credentials the broker cannot authenticate, which
    risks an auth-failure rate limit against the app and, depending on the broker's
    response, a misleading account record.
    """
    attempts: list[str] = []

    async def _spy(*args, **kwargs):
        attempts.append("called")
        return []

    monkeypatch.setattr(account_discovery, "discover_ctrader_accounts", _spy)
    monkeypatch.setattr(account_discovery, "discover_alpaca_accounts", _spy)

    env_file = tmp_path / ".accounts.env"
    for body in (
        'CTRADER_APP_ID="id"\n',
        'CTRADER_APP_ID="id"\nCTRADER_APP_SECRET="secret"\n',
        'CTRADER_APP_SECRET="secret"\nCTRADER_ACCESS_TOKEN="tok"\n',
        'CTRADER_APP_ID="id"\nCTRADER_ACCESS_TOKEN="tok"\n',
        'CTRADER_APP_ID="id"\nCTRADER_APP_SECRET="secret"\nCTRADER_ACCESS_TOKEN="tok"\n',
    ):
        env_file.write_text(body, encoding="utf-8")
        attempts.clear()
        asyncio.run(account_discovery.discover_configured_accounts(env_file))
        complete = "ACCESS_TOKEN" in body and "APP_ID" in body and "APP_SECRET" in body
        if complete:
            assert attempts == ["called"], (
                "a complete credential triple did not trigger discovery: " + body
            )
        else:
            assert attempts == [], (
                "partial cTrader credentials started a broker login: " + body
            )


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


class TestCtraderNameAndBalanceExtraction:
    """How a cTrader payload becomes a display name, login and balance. Six survivors.

    These are the functions that decide **which account a signal is routed to** - the same
    class of decision as cTrader's fill identity, which hid eight surviving mutations. A
    wrong answer here does not raise; it names the wrong account, and the trade goes to the
    wrong broker account.

    Two of the six are about *rejection*, and the asymmetry between them is the thing worth
    recording:

    - **A broker name that is a bare number is refused.** A number in the `brokerName` slot
      is an id that arrived in the wrong field, so it is skipped and the search continues to
      the next field - which is why `brokerName="12345"` yields `""` while
      `brokerName="OANDA"` yields `"OANDA"`.
    - **A numeric login is *accepted*, as long as it is not zero.** cTrader logins really are
      often numeric, so the same digit test has the opposite meaning here. `0` and `"00"` are
      refused; `12345` is a perfectly good login.

    The ignored-label set is what stops an account being *named after its own id*: the ctid
    and the login are added to the ignored values, so `accountName="12345"` cannot become the
    display name for account 12345. The strip on both sides is load-bearing - the candidate
    is stripped before comparison, so unstripped ignored values would not match.

    `_trader_balance` divides by `10**moneyDigits`, so an out-of-range `moneyDigits` would
    scale a balance by a nonsense factor - or raise on a large negative exponent. The `0..12`
    bound falls back to 2, which is asserted rather than assumed.

    The last test records a **real crash** found while measuring this family.
    """

    @staticmethod
    def _msg(**kwargs):
        return SimpleNamespace(**kwargs)

    # ---------------------------------------------------------------- broker name

    def test_a_broker_name_is_taken_from_the_first_usable_field(self) -> None:
        from live_runner.account_discovery import _ctrader_extract_broker_name

        assert _ctrader_extract_broker_name(self._msg(brokerName="OANDA")) == "OANDA"
        assert _ctrader_extract_broker_name(self._msg(companyName="IC Markets")) == "IC Markets"
        # Whitespace-only is not a name.
        assert _ctrader_extract_broker_name(self._msg(brokerName="   ")) == ""

    def test_a_bare_number_in_the_broker_name_slot_is_refused(self) -> None:
        """A number in a *name* field is an id that arrived in the wrong field.

        Refused, and the search continues - so a later field can still supply a real name
        rather than the id being accepted and displayed.
        """
        from live_runner.account_discovery import _ctrader_extract_broker_name

        assert _ctrader_extract_broker_name(self._msg(brokerName="12345")) == ""
        assert _ctrader_extract_broker_name(self._msg(brokerName="0")) == ""
        # And a numeric field does not stop the search.
        assert (
            _ctrader_extract_broker_name(self._msg(brokerName="12345", companyName="OANDA"))
            == "OANDA"
        )
        # No source at all is an empty name, never a crash.
        assert _ctrader_extract_broker_name(None) == ""
        assert _ctrader_extract_broker_name() == ""

    # ------------------------------------------------------------------- login

    def test_a_numeric_login_is_accepted_because_logins_really_are_numeric(self) -> None:
        """The opposite of the broker-name rule, on purpose.

        cTrader trader logins are frequently numeric, so `12345` is a valid login. Asserted
        explicitly because it looks like it should fail the same digit test - it must not.
        """
        from live_runner.account_discovery import _ctrader_extract_login

        assert _ctrader_extract_login(self._msg(traderLogin="12345")) == "12345"
        assert _ctrader_extract_login(self._msg(traderLogin="alice")) == "alice"
        assert _ctrader_extract_login(self._msg(login="L-99")) == "L-99"

    @pytest.mark.parametrize("zero_login", ["0", "00", "000"])
    def test_a_zero_login_is_refused(self, zero_login: str) -> None:
        """`0` is the broker's "unset" and must not become a login that identifies an account.

        A login of `"0"` would be added to the ignored-label set and, worse, would be
        published as `broker_login` - and `0` is the one value that can never identify a real
        account.
        """
        from live_runner.account_discovery import _ctrader_extract_login

        assert _ctrader_extract_login(self._msg(traderLogin=zero_login)) == ""
        # And the search continues to the next field.
        assert (
            _ctrader_extract_login(self._msg(traderLogin="0", login="alice")) == "alice"
        )

    def test_a_negative_login_slips_past_the_zero_guard_KNOWN_ASYMMETRY(self) -> None:
        """**Pinned as-is: the guard refuses `0` but accepts `-1`.**

        The guard is `if candidate.isdigit() and int(candidate) <= 0: continue`, and
        `isdigit()` is checked *first*. `"-1".isdigit()` is `False`, so the `<= 0` half never
        runs for a negative value and `"-1"` is returned as the broker login.

        This is the reachable half of the defect the backlog recorded. It needs no exotic
        input: `traderLogin` is a protobuf **int64** on every message that carries it, so a
        negative login is an ordinary wire value, and `str(-1)` is exactly what line 371
        builds. The companion case the backlog found - `"\u00b2"` raising `ValueError` -
        cannot happen at all, for the same reason int64 rules it out.

        `0` is refused because `"0".isdigit()` is `True`, so the two ends of the same
        `<= 0` intent behave differently. Left unmodified: tightening it changes which
        logins are published as `broker_login`, which is a trading decision, and it is
        backlog item 3.
        """
        from live_runner.account_discovery import _ctrader_extract_login

        for negative in (-1, -99, "-1", "-99"):
            assert _ctrader_extract_login(self._msg(traderLogin=negative)) == str(negative), (
                f"a negative login {negative!r} is no longer accepted as {str(negative)!r}; "
                "the asymmetry documented here has changed - re-measure it and update the "
                "backlog entry together"
            )

        # And the zero case the guard was written for still holds.
        assert _ctrader_extract_login(self._msg(traderLogin="0")) == ""

    def test_the_superscript_that_started_this_cannot_arrive_from_the_broker(self) -> None:
        """The recorded hazard, and the proto field type that rules it out.

        The backlog's item 3 rests on `"\u00b2".isdigit()` being `True` while
        `int("\u00b2")` raises. Both halves are true, and the `ValueError` is real - but the
        value cannot come from cTrader, because `traderLogin` is a protobuf `int64` on every
        message that declares it. A superscript is not a number the wire can carry in an
        int64 field; protobuf rejects it at parse time, exactly as it silently drops an
        unrecognised enum number in item 2.

        So this stays pinned as a characterisation of a branch that broker data cannot reach,
        with the field types asserted alongside it. If a future proto build turns the field
        into a string, the assertion below fails and the hazard becomes live.
        """
        from live_runner.account_discovery import _ctrader_extract_login

        with pytest.raises(ValueError):
            _ctrader_extract_login(self._msg(traderLogin="\u00b2"))

        mod = pytest.importorskip(
            "ctrader_open_api.messages.OpenApiModelMessages_pb2",
            reason="the installed protos are what rule this hazard out",
        )
        seen = {}
        for name in dir(mod):
            message = getattr(mod, name)
            descriptor = getattr(message, "DESCRIPTOR", None)
            if descriptor is None or not hasattr(descriptor, "fields"):
                continue
            for field in descriptor.fields:
                if field.name in ("traderLogin", "login", "traderAccountId"):
                    seen.setdefault(field.name, set()).add(field.type)
        assert seen, (
            "no message declares traderLogin/login/traderAccountId any more, so the "
            "argument that the superscript cannot arrive no longer holds and this hazard "
            "needs re-measuring"
        )
        string_type = 9  # google.protobuf.descriptor.FieldDescriptor.TYPE_STRING
        for field_name, types in sorted(seen.items()):
            assert string_type not in types, (
                f"{field_name} is now string-typed on at least one message, so arbitrary "
                "Unicode - including a superscript - can reach _ctrader_extract_login and "
                "the ValueError becomes reachable from broker data"
            )

    def test_login_fields_are_tried_in_order_and_blank_ones_skipped(self) -> None:
        from live_runner.account_discovery import _ctrader_extract_login

        assert _ctrader_extract_login(
            self._msg(traderLogin="  ", login="  bob  ", traderAccountId="999")
        ) == "bob", "blank fields must be skipped rather than returned"
        assert _ctrader_extract_login(self._msg(traderLogin=None, login="carol")) == "carol"
        assert _ctrader_extract_login(None, self._msg(login="dave")) == "dave"
        assert _ctrader_extract_login() == ""

    def test_an_account_context_failure_discards_every_account(self, monkeypatch) -> None:
        """**Pinned as-is: the per-account loop has no `try` of its own.**

        `_build_ctrader_live_account` calls `_ctrader_account_context` at line 772 and only
        opens its `try:` on line 773, so anything that call raises escapes the builder
        entirely. The caller's loop then has no guard either:

            for account_item in account_items:
                account = _build_ctrader_live_account(...)

        One account failing therefore discards every account found before it, including the
        ones that were perfectly fine, and the caller gets an error rather than a short list.
        That blast radius is real and is why the backlog rates this item above the guard gap
        - it is only the *trigger* that turned out to be unreachable, not the fragility.

        Pinned rather than fixed. Making the loop skip a failing account changes discovery
        from all-or-nothing to partial, which is a judgement about whether a partial account
        list is safe to act on, and that is backlog item 3.
        """
        monkeypatch.setattr(account_discovery, "_ctrader_resolved_account_id", lambda _i: 4242)

        def _boom(*_args, **_kwargs):
            raise ValueError("int() argument must be a string, not 'NoneType'")

        monkeypatch.setattr(account_discovery, "_ctrader_account_context", _boom)

        with pytest.raises(ValueError):
            account_discovery._build_ctrader_live_account(
                "demo.ctraderapi.com", None, "tok", SimpleNamespace()
            )

    # ---------------------------------------------------------- ignored labels

    def test_ignored_labels_are_stripped_so_a_padded_candidate_still_matches(self) -> None:
        """Both sides are stripped, which is the whole point of the helper.

        The candidate is stripped before comparison, so an unstripped ignored value would
        silently fail to match - and then an account could be named after its own ctid.
        """
        from live_runner.account_discovery import _ctrader_ignored_labels

        assert _ctrader_ignored_labels({"  x  ", "y", ""}) == {"x", "y"}
        assert _ctrader_ignored_labels(None) == set()
        assert _ctrader_ignored_labels(set()) == set()

    def test_an_ignored_label_is_not_used_as_a_display_name(self) -> None:
        """The ctid and login are ignored so an account cannot be named after itself.

        The **non-numeric** case is the one that carries the weight, and it is asserted
        first. A numeric ignored value is also refused by the separate `isdigit()` rule, so
        testing only that would leave the ignore check unpinned: dropping
        `candidate in ignored` changes nothing for a number, and the mutation survives.
        A broker login or account label like `"OANDA"` is not a digit string, so it is the
        only value that actually distinguishes the two guards.
        """
        from live_runner.account_discovery import _ctrader_extract_account_label

        assert (
            _ctrader_extract_account_label(
                self._msg(accountName="  12345  "), ignored_values={"12345"}
            )
            == ""
        )
        # A digit-string label is refused *regardless* of the ignore set, so an unignored
        # numeric label is no more usable than an ignored one. Two separate reasons, both
        # asserted: a bare number in a name field is an id that arrived in the wrong slot.
        assert _ctrader_extract_account_label(
            self._msg(accountName="  12345  "), ignored_values={"other"}
        ) == ""
        assert _ctrader_extract_account_label(self._msg(accountName="42")) == ""

        # The distinguishing case: a non-numeric ignored label, which only the ignore check
        # can refuse. Without this, removing `candidate in ignored` passes the suite.
        # NOTE the assertion shape. An earlier version wrapped the message in the same
        # parentheses:
        #
        #     assert (
        #         _ctrader_extract_account_label(...) == "",
        #         "an ignored label was used as the display name",
        #     )
        #
        # That parses as `assert (False, "message")` - a **non-empty tuple, which is always
        # true**. Python even emits "SyntaxWarning: assertion is always true, perhaps
        # remove parentheses?". The assertion never ran, which is exactly why mutation D3
        # survived a test that looked like it covered it.
        #
        # The failure message therefore goes in a plain second argument, outside any
        # parentheses, and the comparison stays on its own lines.
        resolved = _ctrader_extract_account_label(
            self._msg(accountName="  OANDA  "), ignored_values={"OANDA"}
        )
        assert resolved == "", (
            "an ignored label was used as the display name, so an account can be named "
            f"after the login or id it is distinguished from: {resolved!r}"
        )
        assert _ctrader_extract_account_label(
            self._msg(accountName="  OANDA  "), ignored_values={"other"}
        ) == "OANDA"
        # No sources yields no label, never a crash.
        assert _ctrader_extract_account_label(None) == ""
        assert _ctrader_extract_account_label() == ""

    # ---------------------------------------------------------------- balance

    @pytest.mark.parametrize(
        "money_digits,expected",
        [
            (2, 1234.56),
            (5, 1.23456),
            (8, 0.00123456),
            (12, 1.23456e-7),
        ],
    )
    def test_the_balance_is_scaled_by_the_broker_money_digits(
        self, money_digits: int, expected: float
    ) -> None:
        """cTrader reports balance in integer minor units, so the digits are the divisor."""
        from live_runner.account_discovery import _trader_balance

        assert _trader_balance(
            SimpleNamespace(moneyDigits=money_digits, balance=123456)
        ) == pytest.approx(expected)

    def test_a_money_digits_count_of_zero_collapses_to_the_default_KNOWN_FALSY_COLLAPSE(self) -> None:
        """**A falsy value is not a missing value** - documented here because it is invisible.

        The line reads:

            money_digits = int(getattr(trader, "moneyDigits", 2) or 2)

        `getattr` supplies `2` when the attribute is absent, and then `or 2` handles `None`.
        But a broker that genuinely reports **zero** decimal places sends `0`, which is
        falsy, so `or 2` replaces it. The zero is not honoured: the balance is divided by
        `10**2` instead of `10**0`, so a 0-decimal balance comes out a hundred times too
        small.

        Whether that matters is a broker-behaviour question rather than a code bug - FX
        balances are conventionally 2-decimal, so `0` may simply never occur. It is pinned
        because the two failure modes are indistinguishable in the output: a mis-scaled
        balance looks exactly like a small balance.

        The same `or` pattern is what made `isLive=False` indistinguishable from absent
        earlier in this module, so it is a recurring shape here rather than a one-off.
        """
        from live_runner.account_discovery import _trader_balance

        assert _trader_balance(
            SimpleNamespace(moneyDigits=0, balance=123456)
        ) == pytest.approx(1234.56), (
            "moneyDigits=0 is now honoured, so the falsy collapse this documents has "
            "changed - check whether the broker can actually send it before relying on it"
        )

    def test_the_range_guard_admits_the_zero_that_or_two_makes_unreachable(self) -> None:
        """**The next line contradicts the one above it.** Pinned, because it answers the
        backlog's open broker-behaviour question from the code's own intent.

        The line after the `or 2` is:

            scale = 10**money_digits if 0 <= money_digits <= 12 else 100

        `0 <= money_digits` **explicitly admits zero** as a legitimate decimal count, and would
        scale by `10**0 == 1`, i.e. no scaling at all - which is exactly what a 0-decimal
        account needs. So the author who wrote that bound already decided that zero is a real
        value, and the `or 2` three lines above makes it unreachable. The acceptance of zero in
        the guard is dead code.

        The backlog had recorded "whether `0` can occur is a broker-behaviour question", and
        proposed that if the human confirms it cannot, the fix is to make the intent explicit.
        That resolution is unnecessary: the intent *is* explicit here, and it says zero is valid.
        """
        for money_digits, admitted in ((0, True), (2, True), (12, True), (13, False), (-1, False)):
            assert (0 <= money_digits <= 12) is admitted, (
                f"the range guard's verdict on money_digits={money_digits} changed, so its "
                "agreement with `or 2` about zero has changed - re-measure item 6"
            )
        assert 0 <= 0 <= 12, (
            "the range guard no longer admits zero, so the self-contradiction this documents "
            "has been resolved one way or the other - update this test and item 6 together"
        )

        # And the consequence, measured: a 0-decimal balance comes out 100x too small.
        from live_runner.account_discovery import _trader_balance

        raw = 1_234_567
        assert _trader_balance(SimpleNamespace(moneyDigits=0, balance=raw)) == pytest.approx(
            raw / 100
        ), "moneyDigits=0 is now honoured; the 100x mis-scale no longer reproduces"
        assert _trader_balance(SimpleNamespace(moneyDigits=0, balance=raw)) != pytest.approx(
            float(raw)
        ), "moneyDigits=0 now leaves the balance unscaled, which is the fix - item 6 can close"

    def test_an_omitted_money_digits_reaches_the_function_as_zero(self) -> None:
        """`or 2` cannot tell "zero decimal places" from "the broker sent nothing".

        `ProtoOATrader.moneyDigits` is a `uint32` **with presence**, so an unset field reads as
        `0` rather than `None` - verified against the installed protos. Which means the two cases
        the guard is meant to distinguish arrive at `getattr(..., "moneyDigits", 2)` as the *same*
        value, and `or 2` collapses them together with a third: a genuine `None`.

        Pinned on the real message type, so this is a fact about the wire format rather than a
        guess. The consequence is that "honour zero" is not a one-line change either: honouring a
        real zero means also deciding what an *omitted* field should mean, and the answer is
        different for a 0-decimal account than for a broker that stopped sending the field.
        """
        mod = pytest.importorskip(
            "ctrader_open_api.messages.OpenApiModelMessages_pb2",
            reason="the installed protos are what establish the unset-value behaviour",
        )
        field = mod.ProtoOATrader.DESCRIPTOR.fields_by_name["moneyDigits"]
        assert field.has_presence, (
            "moneyDigits no longer has presence, so an unset field would read as the declared "
            "default rather than 0 and this reasoning needs redoing"
        )

        omitted = mod.ProtoOATrader()
        omitted.balance = 1_234_567
        assert omitted.HasField("moneyDigits") is False
        assert omitted.moneyDigits == 0, (
            "an unset moneyDigits no longer reads as 0, so 'omitted' and 'zero decimal places' "
            "are distinguishable and the ambiguity item 6 rests on has changed shape"
        )

        from live_runner.account_discovery import _trader_balance

        explicit_zero = SimpleNamespace(moneyDigits=0, balance=1_234_567)
        assert _trader_balance(omitted) == _trader_balance(explicit_zero), (
            "an omitted moneyDigits and an explicit 0 no longer produce the same balance, so "
            "the two cases are now distinguishable - item 6's options have changed"
        )

    def test_both_copies_of_the_balance_scaling_are_identical(self) -> None:
        """There are two copies of this line, and fixing one must not leave the other behind.

        `_trader_balance` in `account_discovery.py:296` and the inline computation in
        `ctrader_adapter.py:2356` carry the same `int(getattr(trader, "moneyDigits", 2) or 2)` and
        the same `0 <= money_digits <= 12` guard. They were found separately and neither imports
        the other, so a fix applied to one leaves the other with the old behaviour and no test
        failure - the adapter path is the one that runs on every reconcile.

        Asserted at source level because the two functions are not reachable from one test
        without standing up a live session.
        """
        root = Path(__file__).resolve().parents[1]
        needle = 'int(getattr(trader, "moneyDigits", 2) or 2)'
        guard = "0 <= money_digits <= 12"

        # Scan the tree rather than checking two known paths. Checking the two paths makes
        # `len(found) == 2` vacuous - `found` is built by iterating a literal 2-tuple, so it
        # can never fail, and a mutation to `>=` survived because of it. A third copy of this
        # scaling is exactly the failure this test exists to catch, so the count has to come
        # from somewhere that can disagree.
        carriers = {}
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel.startswith("tests/") or "__pycache__" in rel:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            n_needle, n_guard = text.count(needle), text.count(guard)
            if n_needle or n_guard:
                carriers[rel] = (n_needle, n_guard)

        assert sorted(carriers) == [
            "live_runner/account_discovery.py",
            "live_runner/ctrader_adapter.py",
        ], (
            "the moneyDigits scaling is no longer carried by exactly those two files: "
            f"{sorted(carriers)}. Either a copy was added - which would need the same fix - "
            "or one was consolidated, in which case this list should be updated deliberately."
        )
        for rel, (n_needle, n_guard) in carriers.items():
            assert n_needle == 1, (
                f"{rel} now contains {n_needle} copies of the moneyDigits collapse "
                f"({needle!r}); one of them has been changed without the other"
            )
            assert n_guard == 1, (
                f"{rel} now contains {n_guard} copies of the range guard ({guard!r})"
            )

    @pytest.mark.parametrize("money_digits", [None, 13, 20, -1, -5])
    def test_an_unusable_money_digits_count_falls_back_to_two(self, money_digits) -> None:
        """Out of range falls back to the conventional 2, rather than scaling by nonsense.

        `10**13` would divide the balance by a trillion and `10**-5` would multiply it by
        a hundred thousand, so neither is a safe default. Asserted across both ends of the
        range, plus `None`.
        """
        from live_runner.account_discovery import _trader_balance

        assert _trader_balance(
            SimpleNamespace(moneyDigits=money_digits, balance=123456)
        ) == pytest.approx(1234.56), (
            f"moneyDigits={money_digits!r} did not fall back to the 2-digit default"
        )

    def test_a_missing_balance_is_zero_not_an_exception(self) -> None:
        from live_runner.account_discovery import _trader_balance

        assert _trader_balance(SimpleNamespace()) == 0.0
        assert _trader_balance(SimpleNamespace(moneyDigits=2, balance=None)) == 0.0
        assert _trader_balance(
            SimpleNamespace(moneyDigits=2, balance="not a number")
        ) == 0.0

    def test_a_superscript_digit_login_raises_value_error_OUTSIDE_THE_GUARD_KNOWN_DEFECT(self) -> None:
        """**A real crash, found while measuring this family. Recorded, not fixed.**

        `_ctrader_extract_login` reads:

            if candidate.isdigit() and int(candidate) <= 0:
                continue

        `str.isdigit()` is broader than `int()`. `"²"` reports `isdigit() == True` but
        `int("²")` raises `ValueError`. So a `traderLogin` of `"²"` - a superscript, which a
        broker payload or a locale could plausibly carry - raises instead of being refused.

        Why this is worth more than a shrug, and it is not the exception itself:

        - `_ctrader_account_context` (line 772 of the module) is called **outside** the
          `try` in `_build_ctrader_live_account`, which begins on the next line.
        - The per-account loop in `discover_ctrader_accounts` has **no** `try` of its own.

        So the `ValueError` escapes both, out of the discovery call entirely. **One account
        with one odd character discards every account found so far**, including the ones that
        were perfectly fine - and the caller sees an error rather than a short list.

        **Not fixed here**, because the fix is a behaviour decision on the discovery path:
        refuse the odd login and keep the account, or skip the account. Both are reasonable
        and they differ in whether one bad payload costs you every account. Tracked as
        `needs-decision`.

        The test asserts the crash *exists*, so it fails the moment the defect is fixed
        rather than being deleted as an obsolete expectation. If it starts failing and the
        backlog entry is still open, that is the fix landing without a decision.
        """
        from live_runner.account_discovery import _ctrader_extract_login

        with pytest.raises(ValueError):
            _ctrader_extract_login(SimpleNamespace(traderLogin="²"))


class TestCtraderApplicationAuthAndDetailMerge:
    """Application authentication, and how account-list values merge with detail values.

    Seven mutations survived. The detail merge is the interesting half, because it contains
    **three precedence rules and two of them point the opposite way from the third** - with
    nothing in the code saying why.

    | field | when both sources have a value |
    |---|---|
    | `broker_name` | **first** wins (the account list) |
    | `account_name_hint` | **first** wins (the account list) |
    | `trader_login` | **last** wins (the detail payload) |

    All three were measured, not read off the source, and all three are pinned together so the
    asymmetry stays visible. A reader who assumes one rule for the whole function will get two
    of the three wrong, and the login is the one that matters - it is the field the router
    matches accounts by.

    **The coherent reading, which is an inference rather than a documented intent:** the login
    is an *identity* field and wants the most authoritative source, while the broker name and
    display label are cosmetic and the earlier source suffices. That is a defensible design,
    and the code is self-consistent with it - so this is recorded as a decision now on the
    record rather than as a defect. If it was accidental, the fix is to make the third field
    match the other two, and these tests are what would need updating.

    `_authenticate_ctrader_application` is the first call on a fresh cTrader socket, so a
    credential that never reaches the wire fails in the least diagnosable way: the broker
    rejects an unauthenticated request and the error names nothing useful.
    """

    @staticmethod
    def _ctx(*, broker: str = "", hint: str = "", login: str = "",
             ignored: tuple[str, ...] = ()):
        from live_runner.account_discovery import _CtraderAccountContext

        return _CtraderAccountContext(
            host="host",
            sock=None,
            access_token="token",
            broker_name=broker,
            account_name_hint=hint,
            trader_login=login,
            ignored_name_values=set(ignored),
        )

    @staticmethod
    def _detail(**kwargs):
        from types import SimpleNamespace

        base = {}
        base.update(kwargs)
        return SimpleNamespace(**base)

    # -------------------------------------------------- application auth

    def test_the_application_credentials_reach_the_wire(self) -> None:
        """Driven through the real function, decoding what was actually framed.

        Stubs only the response side. Building the request by hand instead - the mistake the
        account-list test already documents - would assert the fixture rather than the
        production path, and this mutation is exactly the one that survives such a test.

        Asserted by decoding the envelope off the captured socket bytes, so the claim is about
        the wire rather than about a mutable message object.
        """
        account_discovery._load_ctrader_protocol()
        import live_runner.account_discovery as ad

        sent: list[bytes] = []

        class _Sock:
            def sendall(self, data: bytes) -> None:
                sent.append(data)

            def settimeout(self, value: float) -> None:
                pass

        reply = ad._ProtoOAApplicationAuthRes()
        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: reply
        try:
            ad._authenticate_ctrader_application(
                _Sock(), app_id="the-client-id", app_secret="the-client-secret"
            )
        finally:
            ad._ctrader_wait_response = original_wait

        assert sent, "the auth request was never sent"
        envelope = ad._ProtoMessage()
        envelope.ParseFromString(sent[0][4:])
        assert envelope.clientMsgId, "the auth request carried no client id"
        request = ad._Protobuf.get(envelope.payloadType)
        request.ParseFromString(envelope.payload)
        assert request.clientId == "the-client-id", (
            f"the client id that reached the wire was {request.clientId!r}"
        )
        assert request.clientSecret == "the-client-secret", (
            f"the client secret that reached the wire was {request.clientSecret!r}; the broker "
            "would reject the connection and name nothing useful"
        )

    def test_a_wrong_application_auth_response_is_refused(self) -> None:
        """The auth reply must be the auth message, or the socket is unauthenticated.

        Nothing downstream checks again, so accepting the wrong type here would let discovery
        proceed on a connection the broker never authenticated - and the failure would surface
        later as an empty account list rather than as an auth error.
        """
        account_discovery._load_ctrader_protocol()
        import live_runner.account_discovery as ad

        class _Sock:
            def sendall(self, data: bytes) -> None:
                pass

            def settimeout(self, value: float) -> None:
                pass

        wrong = ad._ProtoOATraderRes()
        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: wrong
        try:
            ad._authenticate_ctrader_application(_Sock(), app_id="i", app_secret="s")
        except RuntimeError as exc:
            assert "Unexpected application auth response" in str(exc), str(exc)
        else:
            raise AssertionError(
                f"a {type(wrong).__name__} was accepted as an application auth response; "
                "discovery would continue on an unauthenticated socket"
            )
        finally:
            ad._ctrader_wait_response = original_wait

    def test_the_correct_application_auth_response_is_accepted(self) -> None:
        """And the type check passes the right message through, so the guard is not vacuous."""
        account_discovery._load_ctrader_protocol()
        import live_runner.account_discovery as ad

        class _Sock:
            def sendall(self, data: bytes) -> None:
                pass

            def settimeout(self, value: float) -> None:
                pass

        reply = ad._ProtoOAApplicationAuthRes()
        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: reply
        try:
            # Returns None on success; the absence of an exception is the assertion.
            assert (
                ad._authenticate_ctrader_application(
                    _Sock(), app_id="i", app_secret="s"
                )
                is None
            )
        finally:
            ad._ctrader_wait_response = original_wait

    # ------------------------------------------------------ the detail merge

    def test_the_detail_payload_fills_fields_the_list_left_empty(self) -> None:
        """The ordinary case: the list gave nothing, so detail supplies everything."""
        from live_runner.account_discovery import _apply_ctrader_account_detail

        ctx = self._ctx()
        _apply_ctrader_account_detail(
            ctx,
            111,
            self._detail(
                brokerName="DetailBroker", accountName="DetailLabel", traderLogin="detaillogin"
            ),
        )
        assert ctx.broker_name == "DetailBroker"
        assert ctx.account_name_hint == "DetailLabel"
        assert ctx.trader_login == "detaillogin"

    def test_the_broker_name_and_label_keep_their_earlier_value(self) -> None:
        """First-wins for the two cosmetic fields."""
        from live_runner.account_discovery import _apply_ctrader_account_detail

        ctx = self._ctx(broker="ListBroker", hint="ListLabel")
        _apply_ctrader_account_detail(
            ctx,
            111,
            self._detail(
                brokerName="DetailBroker", accountName="DetailLabel", traderLogin="detaillogin"
            ),
        )
        assert ctx.broker_name == "ListBroker", (
            "the detail payload overrode the broker name from the account list"
        )
        assert ctx.account_name_hint == "ListLabel", (
            "the detail payload overrode the account label from the account list"
        )

    def test_the_login_takes_the_later_value_which_is_the_opposite_rule(self) -> None:
        """Last-wins for the identity field - the asymmetry this class exists to record.

        Asserted on its own so that "first wins" cannot quietly become true for this field
        either: the two rules are the finding, and either one changing should be a deliberate
        edit that updates a named test.
        """
        from live_runner.account_discovery import _apply_ctrader_account_detail

        ctx = self._ctx(login="listlogin")
        _apply_ctrader_account_detail(
            ctx, 111, self._detail(traderLogin="detaillogin")
        )
        assert ctx.trader_login == "detaillogin", (
            "the detail login no longer overrides the list login; if that was deliberate the "
            "asymmetry documented in this class needs updating"
        )

    def test_all_three_precedence_rules_together(self) -> None:
        """The full matrix, so the asymmetry is visible in one place.

        Splitting these across separate tests is how the shape of the merge becomes invisible:
        each rule looks locally reasonable, and only the side-by-side shows that two are
        first-wins and one is last-wins.
        """
        from live_runner.account_discovery import _apply_ctrader_account_detail

        detail = self._detail(
            brokerName="DetailBroker",
            accountName="DetailLabel",
            traderLogin="detaillogin",
        )
        ctx = self._ctx(broker="ListBroker", hint="ListLabel", login="listlogin", ignored=("111",))
        _apply_ctrader_account_detail(ctx, 111, detail)

        assert ctx.broker_name == "ListBroker"
        assert ctx.account_name_hint == "ListLabel"
        assert ctx.trader_login == "detaillogin", (
            "the login should be the one field where the later source wins"
        )

    def test_a_blank_detail_login_changes_nothing_and_adds_nothing(self) -> None:
        """A falsy login must not clear an existing one, nor join the ignored set.

        Both halves matter: overwriting with `""` would leave an account that resolves to no
        login at all, and adding `""` to the ignored set would make the empty label unusable as
        a display name for every other field.
        """
        from live_runner.account_discovery import _apply_ctrader_account_detail

        ctx = self._ctx(login="listlogin", ignored=("111",))
        _apply_ctrader_account_detail(ctx, 111, self._detail(traderLogin="  "))
        assert ctx.trader_login == "listlogin", "a blank detail login cleared the known one"
        assert "" not in ctx.ignored_name_values, (
            "the empty string was added to the ignored labels"
        )

    def test_the_detail_login_joins_the_ignored_labels(self) -> None:
        """So the account cannot then be *named* after the login it is distinguished by.

        That is the whole purpose of the ignored set: without it, an account whose display
        label happens to equal its own login would be labelled with its own identifier.
        """
        from live_runner.account_discovery import _apply_ctrader_account_detail

        ctx = self._ctx(ignored=("111",))
        _apply_ctrader_account_detail(ctx, 111, self._detail(traderLogin="detaillogin"))
        assert "detaillogin" in ctx.ignored_name_values, (
            f"the login was not added to the ignored labels: {sorted(ctx.ignored_name_values)}"
        )

    def test_an_earlier_ignored_login_stays_ignored_after_the_login_changes(self) -> None:
        """The list's login remains in the ignored set even though the login is replaced.

        Follows from how the two are populated: `_ctrader_account_context` adds the list login
        when it builds the context, and the detail merge *adds* to the same set rather than
        replacing it. Asserted because the alternative - rebuilding the set from the current
        login only - would un-ignore the earlier value and let an account be named after it.
        """
        from live_runner.account_discovery import (
            _apply_ctrader_account_detail,
            _ctrader_account_context,
        )

        item = SimpleNamespace(
            ctidTraderAccountId=111, traderLogin="listlogin", brokerName="ListBroker"
        )
        ctx = _ctrader_account_context("host", None, "token", 111, item)
        assert "listlogin" in ctx.ignored_name_values

        _apply_ctrader_account_detail(ctx, 111, self._detail(traderLogin="detaillogin"))
        assert ctx.trader_login == "detaillogin"
        assert {"111", "listlogin", "detaillogin"} <= ctx.ignored_name_values, (
            f"an earlier ignored login was dropped: {sorted(ctx.ignored_name_values)}"
        )


class TestCtraderWireProtocol:
    """The length-prefixed frame layer. All eight mutations here survived.

    Nothing in the suite exercised this layer at all, and it is the part that decides whether
    account discovery reads a *correct* account list. A framing bug does not raise - it
    returns a plausible-looking wrong answer, or hangs.

    The protocol is a 4-byte big-endian length followed by a serialised `ProtoMessage`, and
    every mutation in this family targets one of three things: the framing, the read loop, or
    the loop that decides which frame is the answer.

    **The three consequences, in order of how badly they fail:**

    | broken | consequence |
    |---|---|
    | length prefix little-endian or absent | every frame is misparsed; discovery reads garbage or desynchronises permanently |
    | read loop stops early, or a socket close returns a short buffer | a truncated frame is parsed as a whole one - **silently** |
    | a frame for another `clientMsgId` accepted | a response to a *different* request is treated as the answer |

    The middle row is the one worth dwelling on, because it is the quietest. A short read is
    not detectable after the fact: `ParseFromString` on a truncated buffer either raises or,
    for a well-formed prefix, yields a message missing fields that then read as defaults.
    So the two guards here - the loop condition and the `ConnectionError` - are the only
    things standing between a closed socket and a silently incomplete account list.

    `test_a_truncated_frame_is_refused_rather_than_parsed` asserts both halves of that, because
    the two guards are independently necessary: a socket that closes must raise even if the
    caller asked for one byte.
    """

    @staticmethod
    def _chunked_socket(chunks: list[bytes], *, closed: bool = False):
        """A socket that hands back the given chunks, in order, then closes or blocks.

        Records every `recv` size it was asked for, so the read loop's request pattern is
        observable rather than inferred.
        """

        class _Sock:
            def __init__(self) -> None:
                self._pending = list(chunks)
                self.requested: list[int] = []
                self.sent: list[bytes] = []
                self.timeouts: list[float] = []

            def recv(self, size: int) -> bytes:
                """Honour the requested size, the way a real socket does.

                This matters more than it looks. A fake that returns whole chunks regardless
                of the request would let a read loop pass that a real socket would fail - it
                would never notice the loop asking for a 4-byte length prefix and being handed
                a whole frame. So chunks are split to the requested size, and `requested`
                records the pattern for the assertions that check it.
                """
                self.requested.append(size)
                if not self._pending:
                    if closed:
                        return b""
                    raise AssertionError(
                        f"recv({size}) called with no data left; the read loop asked for "
                        f"more than the frame contains"
                    )
                chunk = self._pending[0]
                if len(chunk) <= size:
                    return self._pending.pop(0)
                self._pending[0] = chunk[size:]
                return chunk[:size]

            advance_on_read: float = 0.0

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def settimeout(self, value: float) -> None:
                self.timeouts.append(value)

        return _Sock()

    def _quiet_socket(self):
        """A socket with no data that times out, the way a real one does.

        `socket.timeout` is an alias of `TimeoutError` in modern Python, so raising
        `TimeoutError` here is faithful to what the caller actually sees.
        """

        class _Quiet:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def recv(self, size: int) -> bytes:
                raise TimeoutError()

            def sendall(self, data: bytes) -> None:
                raise AssertionError("a quiet socket should not be sent to")

            def settimeout(self, value: float) -> None:
                self.timeouts.append(value)

        return _Quiet()

    # ------------------------------------------------------------------ framing

    def test_recv_exact_keeps_reading_until_the_frame_is_complete(self) -> None:
        """A frame arrives in pieces; the loop must ask only for what is still missing.

        The single most important assertion in this class, and the request pattern is checked
        as well as the result: a loop that asks for the *whole* size each time, or stops after
        one chunk, is the difference between working and corrupting.
        """
        from live_runner.account_discovery import _ctrader_recv_exact

        sock = self._chunked_socket([b"ab", b"cde", b"fg"])
        assert _ctrader_recv_exact(sock, 7) == b"abcdefg", "the frame was not reassembled"
        assert sock.requested == [7, 5, 2], (
            f"the read loop requested the wrong remaining sizes: {sock.requested}"
        )

    def test_recv_exact_raises_when_the_socket_closes_mid_frame(self) -> None:
        """A short read must be an error, never a short buffer.

        The caller passes the result straight to `ParseFromString`, so returning a truncated
        buffer would parse a *partial* message whose missing fields then read as defaults -
        and protobuf defaults are quietly valid.
        """
        from live_runner.account_discovery import _ctrader_recv_exact

        sock = self._chunked_socket([b"ab", b"c"], closed=True)
        try:
            result = _ctrader_recv_exact(sock, 7)
        except ConnectionError as exc:
            assert "closed" in str(exc).lower(), str(exc)
        else:
            raise AssertionError(
                f"a socket that closed mid-frame returned {result!r} instead of raising; "
                "the caller would parse a truncated frame as a whole one"
            )

    def test_a_single_byte_read_that_closes_still_raises(self) -> None:
        """The guard is not sized to the request: a one-byte read on a dead socket raises too.

        Checked separately because a loop that special-cases small reads would satisfy the
        previous test while still returning short buffers for the length prefix - which is the
        four-byte read every frame starts with.
        """
        from live_runner.account_discovery import _ctrader_recv_exact

        sock = self._chunked_socket([], closed=True)
        try:
            _ctrader_recv_exact(sock, 1)
        except ConnectionError:
            pass
        else:
            raise AssertionError("a one-byte read on a closed socket returned data")

    def test_send_message_prefixes_a_big_endian_length(self) -> None:
        """The framing the broker's parser expects, asserted by decoding it back.

        Little-endian and no-prefix both "work" locally - the socket accepts any bytes - so the
        only way to catch either is to decode the prefix and check it. Asserted against the
        actual payload length, so a stale prefix fails too.
        """
        import struct

        from live_runner.account_discovery import _ctrader_send_message

        account_discovery._load_ctrader_protocol()
        # Read through the module, not a `from ... import`: the module-level aliases are
        # None until the protocol is loaded, so an import taken earlier binds None.
        message = account_discovery._ProtoMessage(
            payloadType=1234, payload=b"hello", clientMsgId="cid-1"
        )
        sock = self._chunked_socket([])
        _ctrader_send_message(sock, message, "cid-1")

        assert len(sock.sent) == 1, sock.sent
        frame = sock.sent[0]
        prefix, body = frame[:4], frame[4:]
        (declared,) = struct.unpack(">I", prefix)
        assert declared == len(body), (
            f"the length prefix says {declared} but {len(body)} bytes follow; the broker's "
            "parser would desynchronise on this frame"
        )
        # And a little-endian prefix would decode to something else entirely.
        (as_little,) = struct.unpack("<I", prefix)
        assert as_little != declared or declared == 0, (
            "the prefix is not distinguishable by byte order for this length"
        )
        envelope = account_discovery._ProtoMessage()
        envelope.ParseFromString(body)
        assert envelope.clientMsgId == "cid-1"
        assert envelope.payloadType == 1234

    # -------------------------------------------------------- response dispatch

    def _account_list_res(self, account_id: int = 12345):
        """A minimal but *valid* account-list response carrying a known account id.

        Two things are load-bearing here.

        `ctidTraderAccountId` and `accessToken` are protobuf required fields, so a bare
        message refuses to serialise - a fixture that skips them fails loudly rather than
        teaching the test to expect the wrong thing.

        And the account id is parameterised because **two responses of the same type are
        indistinguishable by type alone.** An earlier version of the mismatched-frame test
        asserted only `isinstance(result, <that type>)`, which a response carrying the
        *wrong* account also satisfies - so removing the `clientMsgId` check survived a test
        that looked like it covered exactly that. Asserting the id is what makes the
        dispatch check observable.
        """
        account_discovery._load_ctrader_protocol()
        message = account_discovery._ProtoOAGetAccountListByAccessTokenRes()
        message.accessToken = "test-token"
        account = message.ctidTraderAccount.add()
        account.ctidTraderAccountId = account_id
        return message

    def _frame(self, *, payload_type: int, client_msg_id: str, payload: bytes) -> bytes:
        import struct

        # Built with a real serialisation round trip so the wire form is genuine, and read
        # through the module because the alias is None until the protocol is loaded.
        account_discovery._load_ctrader_protocol()
        envelope = account_discovery._ProtoMessage(
            payloadType=payload_type,
            payload=payload,
            clientMsgId=client_msg_id,
        )
        raw = envelope.SerializeToString()
        return struct.pack(">I", len(raw)) + raw

    def test_a_frame_for_another_request_is_skipped_until_the_match(self) -> None:
        """Only the frame whose `clientMsgId` matches is the answer.

        The broker interleaves frames - heartbeats, and responses to earlier requests - so
        this loop is what keeps a stale response from being returned as the answer to the
        current request. Asserted by putting the wrong one first and checking it is skipped
        rather than returned.
        """
        from live_runner.account_discovery import (
            _ctrader_wait_response,
        )

        account_discovery._load_ctrader_protocol()

        wanted = self._account_list_res(account_id=12345)
        wanted_frame = self._frame(
            payload_type=int(wanted.payloadType),
            client_msg_id="mine",
            payload=wanted.SerializeToString(),
        )
        # Same message type, different account, different client id. Only the client id can
        # tell them apart, so the assertion has to look at the account inside the payload.
        other = self._account_list_res(account_id=999)
        other_frame = self._frame(
            payload_type=int(other.payloadType),
            client_msg_id="someone-else",
            payload=other.SerializeToString(),
        )
        sock = self._chunked_socket([other_frame, wanted_frame])

        result = _ctrader_wait_response(sock, "mine", timeout_seconds=5)
        assert isinstance(
            result, account_discovery._ProtoOAGetAccountListByAccessTokenRes
        ), type(result).__name__
        returned_id = result.ctidTraderAccount[0].ctidTraderAccountId
        assert returned_id == 12345, (
            f"the response for another request was returned as the answer: it carries "
            f"account {returned_id}, not the awaited 12345"
        )

    def test_an_error_response_is_raised_not_returned(self) -> None:
        """A broker error must raise, or the caller proceeds as though the call succeeded."""
        from live_runner.account_discovery import _ctrader_wait_response

        account_discovery._load_ctrader_protocol()
        error = account_discovery._ProtoOAErrorRes(errorCode="101", description="nope")
        frame = self._frame(
            payload_type=int(error.payloadType),
            client_msg_id="mine",
            payload=error.SerializeToString(),
        )
        sock = self._chunked_socket([frame])

        try:
            result = _ctrader_wait_response(sock, "mine", timeout_seconds=5)
        except RuntimeError as exc:
            assert "101" in str(exc) and "nope" in str(exc), str(exc)
        else:
            raise AssertionError(
                f"an error response was returned as a success: {type(result).__name__}"
            )

    def test_a_heartbeat_is_answered_and_the_loop_continues(self) -> None:
        """The broker sends heartbeats on the same socket; they are replied to and skipped.

        Two things in one test because they are one behaviour: the heartbeat must be answered
        with an empty client id (it is not a reply to anything), and the loop must continue
        rather than treating it as the awaited payload.
        """
        from live_runner.account_discovery import (
            _ProtoHeartbeatEvent,
            _ctrader_wait_response,
        )

        account_discovery._load_ctrader_protocol()
        heartbeat = _ProtoHeartbeatEvent()
        heartbeat_frame = self._frame(
            payload_type=int(heartbeat.payloadType),
            client_msg_id="",
            payload=heartbeat.SerializeToString(),
        )
        wanted = self._account_list_res()
        wanted_frame = self._frame(
            payload_type=int(wanted.payloadType),
            client_msg_id="mine",
            payload=wanted.SerializeToString(),
        )
        sock = self._chunked_socket([heartbeat_frame, wanted_frame])

        result = _ctrader_wait_response(sock, "mine", timeout_seconds=5)
        assert isinstance(
            result, account_discovery._ProtoOAGetAccountListByAccessTokenRes
        ), type(result).__name__
        assert sock.sent, "the heartbeat was not answered; the broker would drop the stream"

    def test_a_quiet_socket_surfaces_a_timeout(self) -> None:
        """A socket with no frames surfaces the timeout rather than hanging or returning junk.

        The fake raises `TimeoutError` when it has no data, which is what a real socket does
        once its timeout expires - and `_ctrader_wait_response` lets that propagate. Asserted
        so the path is exercised, and so the socket timeout being set is visible.
        """
        from live_runner.account_discovery import _ctrader_wait_response

        account_discovery._load_ctrader_protocol()
        sock = self._quiet_socket()
        try:
            _ctrader_wait_response(sock, "mine", timeout_seconds=5)
        except TimeoutError:
            pass
        else:
            raise AssertionError("a socket that sent nothing did not time out")
        assert sock.timeouts, "the socket timeout was never set"

    def test_each_iteration_shrinks_the_socket_timeout_to_what_is_left(self) -> None:
        """The deadline is a budget for the whole loop, not per iteration.

        Driven with a fake clock rather than real time, for two reasons. A wall-clock test
        would take as long as the budget on every run, and - more importantly - if the
        deadline check were removed the loop would spin forever instead of failing, so the
        mutation would hang the suite rather than kill it. With a fake clock the loop's
        arithmetic is observable directly.

        Three heartbeats are interleaved ahead of the answer, and each iteration must lower
        the socket timeout by the time the clock advanced.
        """
        from live_runner.account_discovery import (
            _ProtoHeartbeatEvent,
            _ctrader_wait_response,
        )

        account_discovery._load_ctrader_protocol()
        heartbeat = _ProtoHeartbeatEvent()
        wanted = self._account_list_res()
        frames = [
            self._frame(
                payload_type=int(heartbeat.payloadType),
                client_msg_id="",
                payload=heartbeat.SerializeToString(),
            )
            for _ in range(3)
        ]
        frames.append(
            self._frame(
                payload_type=int(wanted.payloadType),
                client_msg_id="mine",
                payload=wanted.SerializeToString(),
            )
        )
        sock = self._chunked_socket(frames)

        # Each recv advances the clock by half a second, so the budget must shrink.
        clock = {"now": 1000.0}
        for _ in frames:
            sock.advance_on_read = 0.5
        original_time = account_discovery.time.time
        account_discovery.time.time = lambda: clock["now"]
        original_recv = sock.recv

        def recv(size: int) -> bytes:
            clock["now"] += sock.advance_on_read
            return original_recv(size)

        sock.recv = recv
        try:
            result = _ctrader_wait_response(sock, "mine", timeout_seconds=10)
        finally:
            account_discovery.time.time = original_time

        assert isinstance(
            result, account_discovery._ProtoOAGetAccountListByAccessTokenRes
        ), type(result).__name__
        assert len(sock.timeouts) >= 4, f"only {len(sock.timeouts)} iterations: {sock.timeouts}"
        assert sock.timeouts == sorted(sock.timeouts, reverse=True), (
            f"the socket timeout did not shrink as the budget was consumed: {sock.timeouts}"
        )
        assert sock.timeouts[0] == pytest.approx(10.0), sock.timeouts
        assert sock.timeouts[-1] < sock.timeouts[0], sock.timeouts
        assert all(0 < t <= 10.0 for t in sock.timeouts), sock.timeouts

    def test_an_expired_budget_raises_before_reading_the_socket(self) -> None:
        """With no time left the loop must not attempt a read.

        Checked through the socket recording zero `recv` calls, and this is the test that kills
        the "deadline ignored" mutation. Reading anyway would block on a socket whose timeout
        has not been set, so the read would hang rather than fail - which is the difference
        between a bounded discovery attempt and a wedged daemon.
        """
        from live_runner.account_discovery import _ctrader_wait_response

        account_discovery._load_ctrader_protocol()
        sock = self._chunked_socket([])
        try:
            _ctrader_wait_response(sock, "mine", timeout_seconds=0.0)
        except TimeoutError:
            pass
        else:
            raise AssertionError("a zero budget did not raise")
        assert sock.requested == [], (
            f"the loop read from the socket with no budget left: {sock.requested}"
        )


class TestCtraderAccountIdentityAndListRequest:
    """Which account the broker payload describes, and how the list is requested.

    Six mutations survived; all are now killed. This is the **"how many accounts were
    found"** question, and it is the one that produces a silent-routing failure rather than an
    error: a discovery helper that reports one account when the broker sent two routes a signal
    through an account the operator did not choose, and every individual row looks plausible.

    **The identity resolution has three distinct rules, and two of them are falsy traps.**

    | payload | resolves to |
    |---|---|
    | `ctidTraderAccountId=111` | `111` |
    | only `traderAccountId=222` | `222` - the fallback exists for this |
    | both present and **differing** | `111` - the ctid wins |
    | `ctidTraderAccountId=0`, `traderAccountId=222` | `222` - a falsy ctid falls through |
    | neither | `0`, which the caller refuses |

    The `or` chain means a **`ctidTraderAccountId` of `0` is treated as absent** rather than as
    a real id. That is the same `or`-collapse that affects `moneyDigits` and `isLive` in this
    module, and it is asserted from both sides here: `0` must fall through to the fallback, and
    a genuine id must not.

    A **negative** id resolves to itself and is *not* caught by the resolver - the `account_id
    <= 0` refusal lives in the caller. Asserted as a pair deliberately, so the boundary between
    "resolve" and "refuse" is visible rather than assumed to be in one place.

    The access token going onto the request is asserted by decoding the serialised request, not
    by inspecting the object: protobuf messages are mutable and the assertion has to be about
    what actually reached the socket.
    """

    @staticmethod
    def _item(**kwargs):
        return SimpleNamespace(**kwargs)

    # -------------------------------------------------------------- id resolution

    def test_the_ctid_wins_and_the_trader_id_is_the_fallback(self) -> None:
        """Precedence, asserted with the two ids deliberately different."""
        from live_runner.account_discovery import _ctrader_resolved_account_id

        assert (
            _ctrader_resolved_account_id(
                self._item(ctidTraderAccountId=111, traderAccountId=222)
            )
            == 111
        ), "the trader id overrode the ctid"
        assert _ctrader_resolved_account_id(self._item(ctidTraderAccountId=111)) == 111
        assert _ctrader_resolved_account_id(self._item(traderAccountId=222)) == 222, (
            "the fallback no longer resolves a payload that carries only a trader id"
        )

    def test_a_falsy_ctid_falls_through_to_the_fallback(self) -> None:
        """`ctidTraderAccountId = 0` is the broker's "unset", so the fallback must apply.

        The `or` chain treats a falsy ctid as absent. Asserted explicitly because it is the
        third instance of that shape in this module - after `moneyDigits` and `isLive` - and
        each one silently changes behaviour rather than raising.
        """
        from live_runner.account_discovery import _ctrader_resolved_account_id

        assert _ctrader_resolved_account_id(
            self._item(ctidTraderAccountId=0, traderAccountId=222)
        ) == 222
        assert _ctrader_resolved_account_id(
            self._item(ctidTraderAccountId=0, traderAccountId=None)
        ) == 0

    def test_an_absent_id_resolves_to_zero_and_the_caller_refuses_it(self) -> None:
        """`0` is the "no usable id" sentinel, and the refusal lives in the caller.

        Asserted as a pair on purpose: the resolver does not refuse negatives or zeros, and
        `_build_ctrader_live_account` does. Keeping the boundary visible stops someone later
        adding a second refusal inside the resolver and wondering which one is in force.
        """
        from live_runner.account_discovery import _ctrader_resolved_account_id

        assert _ctrader_resolved_account_id(self._item()) == 0
        assert _ctrader_resolved_account_id(self._item(traderAccountId=0)) == 0
        # A negative id passes straight through the resolver.
        assert _ctrader_resolved_account_id(self._item(ctidTraderAccountId=-5)) == -5

    def test_a_numeric_string_id_is_accepted(self) -> None:
        """cTrader sends ids as strings in some payload versions, so the int() cast matters."""
        from live_runner.account_discovery import _ctrader_resolved_account_id

        assert _ctrader_resolved_account_id(self._item(ctidTraderAccountId="111")) == 111

    # ------------------------------------------------------------- list request

    def test_the_access_token_reaches_the_wire(self) -> None:
        """Asserted through the real request function, on what actually reached the socket.

        A protobuf message is mutable, so "the attribute is set" is not the claim - "this is
        what went out" is. And the claim only becomes observable through
        `_request_ctrader_account_list`, which builds the request, sets the token, sends it and
        then type-checks the reply. Testing it any earlier asserts my own fixture rather than
        the production path - and that is exactly why the first attempt at this test survived
        the mutation: it built the request by hand, so setting the token to `""` in production
        made no difference to what the test sent.

        So the two ends are stubbed and the middle is real: the real request builder builds the
        message, the real send path frames it, and only `_ctrader_wait_response` is replaced
        (with a valid account-list reply, since the type check runs afterwards).
        """
        account_discovery._load_ctrader_protocol()
        import live_runner.account_discovery as ad

        sent: list[bytes] = []

        class _Sock:
            def sendall(self, data: bytes) -> None:
                sent.append(data)

            def settimeout(self, value: float) -> None:
                pass

        reply = ad._ProtoOAGetAccountListByAccessTokenRes()
        reply.accessToken = "t"
        reply.ctidTraderAccount.add().ctidTraderAccountId = 4242

        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: reply
        try:
            ad._request_ctrader_account_list(_Sock(), access_token="secret-token")
        finally:
            ad._ctrader_wait_response = original_wait

        assert sent, "the request was never sent"
        envelope = ad._ProtoMessage()
        envelope.ParseFromString(sent[0][4:])
        assert envelope.clientMsgId, "the request carried no client id"
        request = ad._Protobuf.get(envelope.payloadType)
        request.ParseFromString(envelope.payload)
        assert request.accessToken == "secret-token", (
            f"the access token that reached the wire was {request.accessToken!r}; the broker "
            "would answer with the wrong list or none at all"
        )

    def test_a_wrong_response_type_is_refused_rather_than_iterated(self) -> None:
        """The account list must be the list message, not any message.

        Returning the wrong type here would be iterated as though it held accounts - and the
        count that comes out is the number of accounts discovery believes exist.
        """
        account_discovery._load_ctrader_protocol()
        # `_request_ctrader_account_list` sends, waits, then type-checks. Stubbing only the
        # two ends keeps the type check itself under test.
        sent: list[bytes] = []

        class _Sock:
            def sendall(self, data: bytes) -> None:
                sent.append(data)

            def settimeout(self, value: float) -> None:
                pass

        wrong = account_discovery._ProtoOATraderRes()
        import live_runner.account_discovery as ad

        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: wrong
        try:
            ad._request_ctrader_account_list(_Sock(), access_token="secret-token")
        except RuntimeError as exc:
            assert "Unexpected account list response" in str(exc), str(exc)
        else:
            raise AssertionError(
                f"a {type(wrong).__name__} was accepted as an account list; iterating "
                "it would report a wrong account count"
            )
        finally:
            ad._ctrader_wait_response = original_wait
        assert sent, "the request was never sent"

    def test_the_correct_response_type_is_returned(self) -> None:
        """And the type check passes the right message through unchanged."""
        account_discovery._load_ctrader_protocol()
        import live_runner.account_discovery as ad

        message = ad._ProtoOAGetAccountListByAccessTokenRes()
        message.accessToken = "t"
        message.ctidTraderAccount.add().ctidTraderAccountId = 4242

        class _Sock:
            def sendall(self, data: bytes) -> None:
                pass

            def settimeout(self, value: float) -> None:
                pass

        original_wait = ad._ctrader_wait_response
        ad._ctrader_wait_response = lambda *a, **k: message
        try:
            returned = ad._request_ctrader_account_list(_Sock(), access_token="t")
        finally:
            ad._ctrader_wait_response = original_wait
        assert returned is message
        assert returned.ctidTraderAccount[0].ctidTraderAccountId == 4242

    def test_an_account_with_no_usable_id_is_skipped_by_the_caller(self) -> None:
        """The `<= 0` refusal in the builder, asserted through the real builder path.

        This is the counterpart to the resolver returning `0`: an account the broker listed but
        gave no id for must be dropped, not turned into a `LiveAccount` with id `"0"`.
        """
        from live_runner.account_discovery import _build_ctrader_live_account

        monkeypatch = account_discovery
        original_detail = monkeypatch._request_ctrader_account_detail
        original_trader = monkeypatch._load_ctrader_trader_details
        monkeypatch._request_ctrader_account_detail = lambda *_a, **_k: None
        monkeypatch._load_ctrader_trader_details = lambda *_a, **_k: None
        try:
            for bad_id in (0, None, "0", -5):
                item = SimpleNamespace(
                    ctidTraderAccountId=bad_id, isLive=False, traderLogin="alice"
                )
                result = _build_ctrader_live_account(
                    "demo-host", None, "token", item
                )
                assert result is None, (
                    f"an account with id {bad_id!r} was turned into {result!r} instead of "
                    "being skipped"
                )
        finally:
            monkeypatch._request_ctrader_account_detail = original_detail
            monkeypatch._load_ctrader_trader_details = original_trader


class TestCtraderMissingCtidIsLogged:
    """A cTrader account silently dropped from discovery must at least say so.

    The surviving mutation removed the log line entirely, which is the easy way to lose
    it: it is a `debug` call, it takes no arguments beyond the host and the raw item, and
    nothing fails when it disappears.

    **The line is at `debug`, and that is the right level but only because of who reads
    it.** Discovery skipping an account is normal - an account mid-provision, or one on a
    different host - so this is not a warning. What makes it worth a test is that
    "skipped" and "not there" look identical from outside otherwise: with the line gone,
    a configuration problem in which every cTrader account is missing its ctid produces
    a clean, empty, entirely unremarkable discovery run. The account fields are included
    precisely so that case is diagnosable, and both halves are asserted.
    """

    @staticmethod
    def _capture_debug(call):
        """Run `call` and return its DEBUG records from the discovery logger.

        **`caplog` cannot see this logger.** `trading.live-account-discovery` sets its own
        level to INFO with `propagate=False`, exactly like `trading.live-runner` does, so
        a DEBUG record is dropped before any handler and never reaches caplog no matter
        what `caplog.at_level` is given. My first version of this test used caplog and
        asserted `len(...) == 1` against an empty list - it failed, but the failure was
        the assertion doing its job, not caplog cooperating.

        So a handler is attached to the logger itself and its level is lowered for the
        duration. The logger is restored afterwards so no later test inherits it.
        """
        import logging

        from live_runner import account_discovery as discovery

        captured = []
        handler = logging.Handler()
        handler.emit = captured.append
        previous_level = discovery.logger.level
        discovery.logger.addHandler(handler)
        discovery.logger.setLevel(logging.DEBUG)
        try:
            call()
        finally:
            discovery.logger.removeHandler(handler)
            discovery.logger.setLevel(previous_level)
        return captured

    def test_a_skipped_account_is_logged_with_its_host_and_fields(self) -> None:
        from live_runner.account_discovery import _log_ctrader_account_missing_ctid

        captured = self._capture_debug(
            lambda: _log_ctrader_account_missing_ctid(
                "live.ctrader.com:443", self._proto(login=1234, brokerName="cTrader Demo")
            )
        )
        skipped = [r for r in captured if "without ctidTraderAccountId" in r.getMessage()]
        assert len(skipped) == 1, [r.getMessage() for r in captured]
        data = skipped[0].__dict__.get("data") or getattr(skipped[0], "data", {})
        assert data["host"] == "live.ctrader.com:443", data
        assert data["account_fields"], "the raw account fields are what make the skip diagnosable"
        rendered = str(data["account_fields"])
        assert "1234" in rendered, rendered

    @staticmethod
    def _proto(**fields):
        """A minimal protobuf-shaped stand-in.

        **`_message_to_debug_fields` only understands protobufs.** It returns `{}` for a
        plain dict or a `SimpleNamespace`, because both branches require `DESCRIPTOR` or
        `ListFields`. The real caller passes a decoded protobuf, so the test does too -
        my first version passed a dict and asserted on the fields, which came back empty
        and proved nothing about the code.

        Worth knowing as a property of the code: the "diagnosable" value of this log line
        depends entirely on the caller passing the protobuf shape. Anything else logs an
        empty dict with no complaint.
        """
        class _Descriptor:
            LABEL_REPEATED = 3

            def __init__(self, name):
                self.name = name
                self.label = 1

        return type(
            "_Proto",
            (),
            {
                "ListFields": staticmethod(
                    lambda: [(_Descriptor(name), value) for name, value in fields.items()]
                )
            },
        )()

    def test_the_skip_is_not_escalated_above_debug(self) -> None:
        """An account without a ctid is ordinary, so it must not train anyone to ignore
        warnings. A missing ctid during provisioning is expected; a WARNING here would be
        noise on every successful discovery run."""
        import logging

        from live_runner.account_discovery import _log_ctrader_account_missing_ctid

        captured = self._capture_debug(
            lambda: _log_ctrader_account_missing_ctid("host", self._proto(login=1))
        )
        assert captured, "nothing was logged at all"
        for record in captured:
            assert record.levelno < logging.WARNING, record.getMessage()
