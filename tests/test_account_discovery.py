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
