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
