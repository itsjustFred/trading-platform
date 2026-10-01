from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from live_runner.preflight import _parse_bool_env, run_live_preflight


def _write_config(path: Path, content: str) -> Path:
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return path


# ─── strict boolean parsing (CTRADER_DEMO must never implicitly select live) ──


def test_parse_bool_env_missing_defaults_to_demo() -> None:
    # Missing CTRADER_DEMO must stay True (demo), never implicitly live.
    assert _parse_bool_env("CTRADER_DEMO", None, default=True) is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("True", True),
        ("  TRUE ", True),
        ("1", True),
        ("yes", True),
        ("y", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("n", False),
        ("off", False),
    ],
)
def test_parse_bool_env_recognized_tokens(value: str, expected: bool) -> None:
    assert _parse_bool_env("CTRADER_DEMO", value, default=True) is expected


@pytest.mark.parametrize("value", ["tru", "flase", "", "   ", "maybe", "2"])
def test_parse_bool_env_garbage_raises_naming_var_and_valid_values(value: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        _parse_bool_env("CTRADER_DEMO", value, default=True)
    message = str(excinfo.value)
    assert "CTRADER_DEMO" in message
    assert "true" in message
    assert "false" in message


def test_live_preflight_passes_for_demo_and_paper_accounts(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: alpaca-paper
            broker: alpaca
            broker_login: PA3EFBL4P7BU
          - id: "10105152"
            broker: ctrader

        deployments:
          - strategy_name: equity_momentum
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: 1h
          - strategy_name: fx_breakout
            account_id: "10105152"
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    result = run_live_preflight(
        config_path,
        env={
            "ALPACA_API_KEY_ACC1": "alpaca-key",
            "ALPACA_SECRET_KEY_ACC1": "alpaca-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3EFBL4P7BU",
            "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_REFRESH_TOKEN": "refresh",
            "CTRADER_DEMO": "true",
        },
    )

    assert result.ok is True
    assert [account.ok for account in result.accounts] == [True, True]
    assert result.deployment_errors == []


def test_live_preflight_warns_when_ctrader_resolves_to_live_mode(tmp_path: Path) -> None:
    """A cTrader account configured for live must be a warning, not silence.

    `CTRADER_DEMO` is false, so the credentials resolve to live. The warning is the
    only signal that the operator is about to arm real money - and removing it leaves
    every collected test green, because the one cTrader preflight test sets
    `CTRADER_DEMO: "true"` and asserts only `result.ok`.

    Preflight still passes (`ok is True`), which is what makes the warning the only
    thing standing between a demo deployment and a live one. Asserted on the warning
    text rather than `ok`, so a test cannot pass by failing the account instead.
    """
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-live
            broker: ctrader
            ctid: "12345"

        deployments:
          - strategy_name: equity_momentum
            account_id: ctrader-live
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    result = run_live_preflight(
        config_path,
        env={
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_REFRESH_TOKEN": "refresh",
            "CTRADER_DEMO": "false",
        },
        check_connectivity=False,
    )

    account = result.accounts[0]
    assert account.ok is True, (
        "the account should still pass; the warning is the signal, not a failure: "
        + repr(account.errors)
    )
    assert any("live mode" in warning for warning in account.warnings), (
        "a cTrader account resolving to live mode produced no warning, so the operator "
        "has no signal that this deployment trades real money: " + repr(account.warnings)
    )

    # And demo mode must stay quiet, so the warning cannot become unconditional noise.
    demo_config = _write_config(
        tmp_path / "demo.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader
            ctid: "12345"

        deployments:
          - strategy_name: equity_momentum
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )
    demo = run_live_preflight(
        demo_config,
        env={
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_REFRESH_TOKEN": "refresh",
            "CTRADER_DEMO": "true",
        },
        check_connectivity=False,
    )
    assert not any("live mode" in warning for warning in demo.accounts[0].warnings), (
        "a demo account was warned about live mode: " + repr(demo.accounts[0].warnings)
    )


def test_live_preflight_warns_when_the_ctrader_refresh_token_is_missing(
    tmp_path: Path,
) -> None:
    """No refresh token means token refresh cannot be verified - say so.

    Without `CTRADER_REFRESH_TOKEN` the adapter cannot rotate its access token, so the
    account will stop working once the current token expires, and it will do so with a
    daemon log rather than a preflight answer. The warning is the only chance to see it
    at start-up; removing it leaves every test green, because every cTrader fixture
    supplies a refresh token.
    """
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader
            ctid: "12345"

        deployments:
          - strategy_name: equity_momentum
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    base_env = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_DEMO": "true",
    }

    without = run_live_preflight(
        config_path, env=base_env, check_connectivity=False
    )
    assert any(
        "REFRESH_TOKEN" in warning for warning in without.accounts[0].warnings
    ), (
        "a missing refresh token produced no warning, so the account will fail later "
        "when its token expires: " + repr(without.accounts[0].warnings)
    )

    with_token = run_live_preflight(
        config_path,
        env={**base_env, "CTRADER_REFRESH_TOKEN": "refresh"},
        check_connectivity=False,
    )
    assert not any(
        "REFRESH_TOKEN" in warning for warning in with_token.accounts[0].warnings
    ), (
        "the refresh-token warning appeared despite the token being present: "
        + repr(with_token.accounts[0].warnings)
    )


def test_the_ctrader_connectivity_check_is_never_attempted_for_an_invalid_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An account that cannot be routed must not also open a broker connection.

    `_preflight_ctrader_account` runs the connectivity check only when
    `check_connectivity and credentials is not None and not errors`. A wrong cTID
    stops before any network call, which is worth pinning for a concrete reason: a
    connect attempt with an identifier the broker will reject is an auth-failure
    against the app, and repeated preflights are how an app gets rate-limited. A
    non-numeric cTID is a config mistake a human fixes in seconds.

    The adapter is replaced with a stub that records construction, so the assertion is
    about whether a connection was *attempted*, not about what the broker answered.

    Note on reachability. Both invalid-identifier shapes (non-numeric, and zero or
    negative) are rejected by `parse_live_deployments_config` before any account
    exists, so this test exercises the early return rather than the `not errors`
    clause. That clause turns out to be an **equivalent mutant**: the only thing that
    appends to `errors` before the check is `_ctrader_preflight_credentials`, and it
    returns `None` in exactly the same breath, so `credentials is not None` already
    excludes every case `not errors` would. Recorded rather than given a contrived
    test - the behaviour is right, and the redundancy is harmless.
    """
    attempted: list[object] = []

    class _ExplodingAdapter:
        def __init__(self, **kwargs) -> None:
            attempted.append(kwargs)

        def connect(self, *_args, **_kwargs):
            raise AssertionError("connect() must not be called for an invalid account")

    monkeypatch.setattr("live_runner.preflight.CTraderLiveAdapter", _ExplodingAdapter)

    env = {
        "CTRADER_APP_ID": "app",
        "CTRADER_APP_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_REFRESH_TOKEN": "refresh",
        "CTRADER_DEMO": "true",
    }

    for ctid, reason in (("not-a-number", "numeric"), ("0", "non-positive")):
        attempted.clear()
        result = run_live_preflight(
            _write_config(
                tmp_path / f"invalid-{ctid}.yaml",
                f"""
                accounts:
                  - id: ctrader-broken
                    broker: ctrader
                    ctid: "{ctid}"

                deployments: []
                """,
            ),
            env=env,
            check_connectivity=True,
        )

        assert result.ok is False
        assert attempted == [], (
            f"a broker connection was attempted for cTID {ctid!r}: " + repr(attempted)
        )
        assert result.accounts == [], (
            f"cTID {ctid!r} produced an account, so parsing accepted it: "
            + repr(result.accounts)
        )
        assert any(reason in error for error in result.deployment_errors), (
            f"cTID {ctid!r} was rejected without saying why: "
            + repr(result.deployment_errors)
        )


def test_live_preflight_reports_missing_credentials(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: alpaca-paper
            broker: alpaca

        deployments:
          - strategy_name: equity_momentum
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: 1h
        """,
    )

    result = run_live_preflight(config_path, env={})

    assert result.ok is False
    assert result.accounts[0].ok is False
    assert "Missing Alpaca credentials" in result.accounts[0].errors[0]


def test_live_preflight_rejects_non_numeric_ctrader_account_id(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader

        deployments:
          - strategy_name: fx_breakout
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    result = run_live_preflight(
        config_path,
        env={
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
        },
    )

    # B3: unroutable accounts fail the preflight as a reported config
    # error, not an unhandled crash.
    assert result.ok is False
    assert any(
        "no numeric broker identifier" in error for error in result.deployment_errors
    )


def test_live_preflight_reports_unknown_deployment_account(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: alpaca-paper
            broker: alpaca

        deployments:
          - strategy_name: orphan_strategy
            account_id: missing-account
            symbols: [AAPL]
            timeframe: 1h
        """,
    )

    result = run_live_preflight(
        config_path,
        env={
            "ALPACA_API_KEY": "alpaca-key",
            "ALPACA_SECRET_KEY": "alpaca-secret",
            "ALPACA_PAPER": "true",
        },
    )

    assert result.ok is False
    assert result.deployment_errors == [
        "orphan_strategy (missing-account): references unknown account_id."
    ]



def test_live_preflight_connectivity_reads_alpaca_balance_and_positions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: alpaca-paper
            broker: alpaca

        deployments:
          - strategy_name: equity_momentum
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: 1h
        """,
    )

    class _FakeAlpacaAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def connect(self):
            return None

        async def get_balance(self):
            return 1234.5, 2345.6

        async def get_positions(self):
            return [{"symbol": "AAPL"}, {"symbol": "MSFT"}]

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "AlpacaLiveAdapter", _FakeAlpacaAdapter)

    result = run_live_preflight(
        config_path,
        env={
            "ALPACA_API_KEY": "alpaca-key",
            "ALPACA_SECRET_KEY": "alpaca-secret",
            "ALPACA_PAPER": "true",
        },
        check_connectivity=True,
    )

    assert result.ok is True
    assert result.accounts[0].connected is True
    assert result.accounts[0].balance == 1234.5
    assert result.accounts[0].equity == 2345.6
    assert result.accounts[0].positions_count == 2


def test_live_preflight_connectivity_reports_ctrader_error(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: "10105152"
            broker: ctrader

        deployments:
          - strategy_name: fx_breakout
            account_id: "10105152"
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    class _FakeCTraderAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def connect(self):
            raise RuntimeError("auth failed")

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _FakeCTraderAdapter)

    result = run_live_preflight(
        config_path,
        env={
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_REFRESH_TOKEN": "refresh",
        },
        check_connectivity=True,
    )

    assert result.ok is False
    assert result.accounts[0].connected is False
    assert "cTrader connectivity check failed" in result.accounts[0].errors[0]


def test_live_preflight_threads_accounts_env_path_to_ctrader_adapter(
    tmp_path: Path, monkeypatch
) -> None:
    """Preflight must construct the cTrader adapter with the validated accounts file.

    The adapter's token refresh must read & write exactly that file; the default
    .accounts.env must be left untouched.
    """
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: "10105152"
            broker: ctrader

        deployments:
          - strategy_name: fx_breakout
            account_id: "10105152"
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    accounts_env = tmp_path / "custom.accounts.env"
    accounts_env.write_text(
        "CTRADER_ACCESS_TOKEN=old-access\nCTRADER_REFRESH_TOKEN=old-refresh\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class _FakeCTraderAdapter:
        def __init__(self, **kwargs):
            captured["accounts_env_path"] = kwargs.get("accounts_env_path")
            self._path = Path(kwargs["accounts_env_path"])

        async def connect(self):
            # Simulate a token refresh writing to exactly the threaded file.
            self._path.write_text(
                "CTRADER_ACCESS_TOKEN=new-access\nCTRADER_REFRESH_TOKEN=new-refresh\n",
                encoding="utf-8",
            )

        async def get_balance(self):
            return 100.0, 100.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _FakeCTraderAdapter)

    # Ensure the default accounts file would be detectable if wrongly written.
    default_path = Path(preflight.DEFAULT_ACCOUNTS_ENV)
    default_before = default_path.read_text(encoding="utf-8") if default_path.exists() else None

    result = run_live_preflight(
        config_path,
        accounts_env_path=accounts_env,
        env={
            "CTRADER_APP_ID": "app",
            "CTRADER_APP_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "old-access",
            "CTRADER_REFRESH_TOKEN": "old-refresh",
            "CTRADER_DEMO": "true",
        },
        check_connectivity=True,
    )

    assert result.ok is True
    # Adapter received exactly the validated/threaded path.
    assert Path(str(captured["accounts_env_path"])) == accounts_env
    # The threaded file was refreshed in place.
    assert "new-access" in accounts_env.read_text(encoding="utf-8")
    # The default .accounts.env must be untouched.
    default_after = default_path.read_text(encoding="utf-8") if default_path.exists() else None
    assert default_after == default_before


class _CountingAdapter:
    """Records how many times each broker call happened, in order."""

    def __init__(self, *, symbols_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.connected = False
        self._symbols_error = symbols_error

    async def connect(self) -> None:
        self.calls.append("connect")
        self.connected = True

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    async def get_balance(self):
        self.calls.append("get_balance")
        return 100000.0, 100000.0

    async def get_positions(self):
        self.calls.append("get_positions")
        return []

    async def tradeable_symbol_names(self):
        self.calls.append("tradeable_symbol_names")
        if self._symbols_error is not None:
            raise self._symbols_error
        return ["AAPL", "MSFT"]


def test_the_tradeable_symbol_read_reuses_the_connected_client() -> None:
    """One connect per account, not two, and on the same event loop.

    The symbol read used to run as a second `asyncio.run` after the connectivity
    check had already disconnected, so every account cost a second connect/disconnect
    round-trip and the same broker client was driven from two different event loops -
    which for a client holding sockets and timers is not merely wasteful.
    """
    from live_runner.preflight import _run_broker_connectivity_check

    adapter = _CountingAdapter()
    errors: list[str] = []
    connected, _balance, _equity, _positions, symbols = _run_broker_connectivity_check(
        adapter, broker_label="Alpaca", errors=errors
    )

    assert connected is True
    assert symbols == {"AAPL", "MSFT"}
    assert errors == []
    assert adapter.calls.count("connect") == 1, (
        f"the account was connected more than once: {adapter.calls}"
    )
    assert adapter.calls.count("disconnect") == 1
    assert adapter.calls.index("tradeable_symbol_names") < adapter.calls.index("disconnect"), (
        "the symbol read must happen on the open connection, not after it closes: "
        f"{adapter.calls}"
    )


def test_an_unreadable_symbol_list_warns_without_failing_connectivity() -> None:
    """Best effort, preserved: the account connected, so the preflight must not fail.

    A symbol list the broker will not hand over is "not checked" - None, plus a
    warning - rather than a failed preflight. Failing here would turn an
    informational read into a blocker on an account that demonstrably works.
    """
    from live_runner.preflight import _run_broker_connectivity_check

    adapter = _CountingAdapter(symbols_error=RuntimeError("symbols unavailable"))
    errors: list[str] = []
    warnings: list[str] = []
    connected, _balance, _equity, _positions, symbols = _run_broker_connectivity_check(
        adapter, broker_label="cTrader", errors=errors, warnings=warnings
    )

    assert connected is True, "connectivity was proven; it must not be undone by a symbols read"
    assert errors == [], f"an unreadable symbol list must not be an error: {errors}"
    assert symbols is None, "None means 'not checked', not an empty tradeable set"
    assert len(warnings) == 1 and "tradeable symbol list" in warnings[0]
    assert adapter.calls.count("connect") == 1


def test_a_broker_without_a_symbol_list_is_reported_not_silently_swallowed():
    """Only cTrader has a per-account tradeable list; Alpaca has no such method.

    The read was called unconditionally, so every Alpaca preflight raised
    `AttributeError` at that line - and because the Alpaca caller passes no
    `warnings` list, the error was discarded and the result stayed None with
    nothing recorded anywhere. A guaranteed failure that reports nothing is worse
    than not attempting the read: it made the docstring's "leaves the result as
    None" true by accident, and it would have hidden a real failure if Alpaca ever
    grew the method and it started erroring.

    So the result must be None *and* the reason recorded, whenever a caller is
    collecting warnings.
    """

    from live_runner import preflight

    class _NoSymbolListAdapter:
        """Everything the connectivity check needs, and nothing more."""

        def __init__(self):
            self.connected = False
            self.disconnected = False

        async def connect(self):
            self.connected = True

        async def get_balance(self):
            return 1000.0, 1000.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            self.disconnected = True

    adapter = _NoSymbolListAdapter()
    warnings: list[str] = []

    connected, balance, equity, positions, tradeable = (
        preflight._run_broker_connectivity_check(
            adapter, broker_label="Test", errors=[], warnings=warnings
        )
    )

    assert connected is True
    assert tradeable is None, "an unchecked list must stay None, never an empty set"
    assert warnings, (
        "an adapter with no symbol-list method was skipped in silence; the "
        "operator cannot tell 'not checked' from 'checked and empty'"
    )
    assert any("tradeable symbol list" in w for w in warnings), warnings
    assert adapter.connected and adapter.disconnected, (
        "the read must not disturb the single connect/disconnect round-trip"
    )


def test_a_symbol_list_failure_is_warned_and_leaves_the_result_unchecked():
    """The best-effort contract: a failing read warns, and claims nothing."""

    from live_runner import preflight

    class _FailingSymbolAdapter:
        async def connect(self):
            return None

        async def get_balance(self):
            return 1000.0, 1000.0

        async def get_positions(self):
            return []

        async def tradeable_symbol_names(self):
            raise RuntimeError("broker said no")

        async def disconnect(self):
            return None

    warnings: list[str] = []
    connected, _b, _e, _p, tradeable = (
        preflight._run_broker_connectivity_check(
            _FailingSymbolAdapter(),
            broker_label="Test",
            errors=[],
            warnings=warnings,
        )
    )

    assert connected is True, "a failed symbol read must not fail connectivity"
    assert tradeable is None, "a failed read must leave the list unchecked, not empty"
    assert any("broker said no" in w for w in warnings), warnings


# --------------------------------------------------------------------------
# The rest of the arming gate, found by mutation testing with `mutate.sh`
# --------------------------------------------------------------------------
#
# `_validate_deployments` has four guards that no test reaches. Dropping any of
# them leaves all 349 collected tests green, so a refactor could quietly delete
# the arming preflight's own refusals. The unknown-account guard beside them *is*
# pinned (`test_live_preflight_reports_unknown_deployment_account`), which is
# what makes the asymmetry odd: the loudest failure is covered and the quieter
# three are not.
#
# These are errors, not warnings, so each one is a deployment the preflight
# refuses to certify. That refusal is the product.


def _alpaca_config(tmp_path: Path, deployment_block: str) -> Path:
    return _write_config(
        tmp_path / "live_deployments.yaml",
        f"""
        accounts:
          - id: alpaca-paper
            broker: alpaca

        deployments:
        {deployment_block}
        """,
    )


_ALPACA_ENV = {
    "ALPACA_API_KEY": "alpaca-key",
    "ALPACA_SECRET_KEY": "alpaca-secret",
    "ALPACA_PAPER": "true",
}


def test_a_deployment_with_no_symbols_is_refused(tmp_path: Path) -> None:
    """Kills dropping the no-symbols error. An empty symbol list can never trade."""
    config_path = _alpaca_config(
        tmp_path,
        """
          - strategy_name: symbol_less
            account_id: alpaca-paper
            symbols: []
            timeframe: 1h
        """,
    )

    result = run_live_preflight(config_path, env=_ALPACA_ENV)

    assert result.ok is False
    assert result.deployment_errors == ["symbol_less (alpaca-paper): has no symbols configured."]


def test_a_deployment_with_no_timeframe_is_refused(tmp_path: Path) -> None:
    """Kills dropping the no-timeframe error. Backtests and grids both need one."""
    config_path = _alpaca_config(
        tmp_path,
        """
          - strategy_name: timeless
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: "   "
        """,
    )

    result = run_live_preflight(config_path, env=_ALPACA_ENV)

    assert result.ok is False
    assert result.deployment_errors == ["timeless (alpaca-paper): has no timeframe configured."]


def test_a_discord_deployment_with_no_channel_is_warned(tmp_path: Path) -> None:
    """Kills dropping the channel_id warning.

    A Discord deployment with no channel id listens to nothing and says so only
    here, so the warning is the whole of what makes that failure visible.
    """
    config_path = _alpaca_config(
        tmp_path,
        """
          - strategy_name: silent_discord
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: 1h
            signal_source: discord
            signal_config:
              bot: rocketfuel
        """,
    )

    result = run_live_preflight(config_path, env=_ALPACA_ENV)

    assert any("has no channel_id in signal_config" in w for w in result.deployment_warnings), (
        f"expected the channel_id warning, got {result.deployment_warnings}"
    )


def test_a_non_discord_deployment_is_not_warned_about_a_channel(tmp_path: Path) -> None:
    """The other half, so the warning cannot be satisfied by always firing."""
    config_path = _alpaca_config(
        tmp_path,
        """
          - strategy_name: not_discord
            account_id: alpaca-paper
            symbols: [AAPL]
            timeframe: 1h
        """,
    )

    result = run_live_preflight(config_path, env=_ALPACA_ENV)

    assert not any("channel_id" in w for w in result.deployment_warnings)


def _account_result(account_id: str, tradeable: set[str] | None):
    from live_runner.preflight import LivePreflightAccountResult

    return LivePreflightAccountResult(
        account_id=account_id,
        broker="alpaca",
        ok=True,
        paper=True,
        demo=None,
        connected=True,
        balance=1000.0,
        equity=1000.0,
        positions_count=0,
        errors=[],
        warnings=[],
        tradeable_symbols=tradeable,
    )


def _deployment(account_id: str = "alpaca-paper", symbols: list[str] | None = None):
    from live_runner.preflight import LiveDeploymentConfig

    return LiveDeploymentConfig(
        strategy_name="probe",
        account_id=account_id,
        symbols=symbols if symbols is not None else ["AAPL"],
        timeframe="1h",
    )


def _account(account_id: str = "alpaca-paper"):
    from live_runner.preflight import LiveAccountConfig

    return LiveAccountConfig(id=account_id, broker="alpaca")


def test_a_broker_answered_with_an_empty_symbol_list_still_warns() -> None:
    """Kills turning `is not None` into a truthiness check at the consumer.

    The producer side already insists an unchecked list stays `None`, "never an
    empty set" - so that the operator can tell "not checked" from "checked and
    empty". This is the other half: the consumer has to keep those apart too.

    With `if tradeable:` instead of `if tradeable is not None:`, a broker that
    genuinely answered with zero tradeable symbols becomes falsy, the check is
    skipped, and the deployment is certified with a warning that never appears.
    """
    from live_runner.preflight import _validate_deployments

    errors, warnings = _validate_deployments(
        [_account()],
        [_deployment(symbols=["AAPL", "MSFT"])],
        [_account_result("alpaca-paper", set())],
    )

    assert errors == []
    assert any("cannot trade AAPL, MSFT" in w for w in warnings), (
        f"a broker that answered with an empty list must still be reported: {warnings}"
    )


def test_an_unchecked_symbol_list_claims_nothing() -> None:
    """`None` means the broker never answered, so no claim is made in either direction."""
    from live_runner.preflight import _validate_deployments

    errors, warnings = _validate_deployments(
        [_account()],
        [_deployment(symbols=["AAPL"])],
        [_account_result("alpaca-paper", None)],
    )

    assert errors == []
    assert not any("cannot trade" in w for w in warnings), (
        f"an unchecked list must not produce a claim: {warnings}"
    )


def test_a_broker_answered_with_the_symbols_we_want_is_quiet() -> None:
    """The other half again: a checked, satisfied list must be silent."""
    from live_runner.preflight import _validate_deployments

    errors, warnings = _validate_deployments(
        [_account()],
        [_deployment(symbols=["aapl", " AAPL "])],
        [_account_result("alpaca-paper", {"AAPL", "MSFT"})],
    )

    assert errors == []
    assert not any("cannot trade" in w for w in warnings), warnings
