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


# ─── connectivity: one event loop, and the process must be able to exit ──────


_TWO_CTRADER_ACCOUNTS = """
    accounts:
      - id: "10105152"
        broker: ctrader
      - id: "10105153"
        broker: ctrader

    deployments: []
    """

_CTRADER_ENV = {
    "CTRADER_APP_ID": "app",
    "CTRADER_APP_SECRET": "secret",
    "CTRADER_ACCESS_TOKEN": "token",
    "CTRADER_REFRESH_TOKEN": "refresh",
    "CTRADER_DEMO": "true",
}


def test_live_preflight_checks_every_ctrader_account_on_one_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """The Twisted reactor binds to the first loop it is installed on.

    On the cTrader demo, a second cTrader account checked on a fresh
    ``asyncio.run`` loop timed out on every connect attempt. All accounts must
    share one loop, and that loop must be closed afterwards.
    """
    import asyncio

    config_path = _write_config(tmp_path / "live_deployments.yaml", _TWO_CTRADER_ACCOUNTS)
    loops: dict[str, asyncio.AbstractEventLoop] = {}

    class _LoopRecordingCTraderAdapter:
        def __init__(self, **kwargs):
            self.account_id = str(kwargs["account_id"])

        async def connect(self):
            loops[self.account_id] = asyncio.get_running_loop()

        async def get_balance(self):
            return 100.0, 100.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _LoopRecordingCTraderAdapter)

    result = run_live_preflight(config_path, env=_CTRADER_ENV, check_connectivity=True)

    assert result.ok is True
    assert [account.connected for account in result.accounts] == [True, True]
    assert set(loops) == {"10105152", "10105153"}
    assert loops["10105152"] is loops["10105153"]
    assert loops["10105152"].is_closed()


def test_live_preflight_releases_twisted_threads_after_connectivity(
    tmp_path: Path, monkeypatch
) -> None:
    """Without this the CLI printed its result and then hung in interpreter shutdown."""
    config_path = _write_config(tmp_path / "live_deployments.yaml", _TWO_CTRADER_ACCOUNTS)
    released: list[bool] = []

    class _FakeCTraderAdapter:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            return None

        async def get_balance(self):
            return 100.0, 100.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _FakeCTraderAdapter)
    monkeypatch.setattr(
        preflight, "_release_twisted_reactor_threads", lambda: released.append(True)
    )

    run_live_preflight(config_path, env=_CTRADER_ENV, check_connectivity=True)
    assert released == [True]

    released.clear()
    run_live_preflight(config_path, env=_CTRADER_ENV, check_connectivity=False)
    assert released == []


def test_release_twisted_reactor_threads_stops_the_reactor_pool(monkeypatch) -> None:
    import sys
    import types

    from live_runner import preflight

    stopped: list[bool] = []

    class _FakeReactor:
        threadpool = object()

        def _stopThreadPool(self):
            stopped.append(True)

    fake_internet = types.ModuleType("twisted.internet")
    fake_internet.reactor = _FakeReactor()
    fake_twisted = types.ModuleType("twisted")
    fake_twisted.internet = fake_internet
    monkeypatch.setitem(sys.modules, "twisted", fake_twisted)
    monkeypatch.setitem(sys.modules, "twisted.internet", fake_internet)
    monkeypatch.setitem(sys.modules, "twisted.internet.reactor", fake_internet.reactor)

    preflight._release_twisted_reactor_threads()
    assert stopped == [True]

    fake_internet.reactor.threadpool = None
    preflight._release_twisted_reactor_threads()
    assert stopped == [True]


def test_release_twisted_reactor_threads_is_a_noop_without_a_reactor(monkeypatch) -> None:
    import sys

    from live_runner import preflight

    monkeypatch.delitem(sys.modules, "twisted.internet.reactor", raising=False)
    preflight._release_twisted_reactor_threads()


def test_connectivity_loop_shuts_down_pending_async_generators(tmp_path: Path, monkeypatch) -> None:
    """An adapter that leaks a suspended async generator must have it closed."""
    config_path = _write_config(tmp_path / "live_deployments.yaml", _TWO_CTRADER_ACCOUNTS)
    closed: list[bool] = []

    async def _leaky():
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    class _LeakyAdapter:
        def __init__(self, **kwargs):
            self._gen = None

        async def connect(self):
            self._gen = _leaky()
            await self._gen.__anext__()  # left suspended, never exhausted

        async def get_balance(self):
            return 100.0, 100.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _LeakyAdapter)
    result = run_live_preflight(config_path, env=_CTRADER_ENV, check_connectivity=True)
    assert result.ok is True
    assert closed == [True, True]


def test_connectivity_loop_warns_when_thread_release_fails(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    config_path = _write_config(tmp_path / "live_deployments.yaml", _TWO_CTRADER_ACCOUNTS)

    class _Adapter:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            return None

        async def get_balance(self):
            return 100.0, 100.0

        async def get_positions(self):
            return []

        async def disconnect(self):
            return None

    from live_runner import preflight

    def _boom():
        raise RuntimeError("release exploded")

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _Adapter)
    monkeypatch.setattr(preflight, "_release_twisted_reactor_threads", _boom)
    monkeypatch.setattr(preflight.logger, "propagate", True)
    with caplog.at_level("WARNING"):
        result = run_live_preflight(config_path, env=_CTRADER_ENV, check_connectivity=True)
    assert result.ok is True
    assert any("release exploded" in (r.getMessage() + str(r.exc_info)) for r in caplog.records)


def test_release_twisted_reactor_threads_warns_when_handler_missing(monkeypatch, caplog) -> None:
    import sys
    import types

    from live_runner import preflight

    class _Reactor:
        threadpool = object()

    fake_internet = types.ModuleType("twisted.internet")
    fake_internet.reactor = _Reactor()
    fake_twisted = types.ModuleType("twisted")
    fake_twisted.internet = fake_internet
    monkeypatch.setitem(sys.modules, "twisted", fake_twisted)
    monkeypatch.setitem(sys.modules, "twisted.internet", fake_internet)
    monkeypatch.setitem(sys.modules, "twisted.internet.reactor", fake_internet.reactor)
    monkeypatch.setattr(preflight.logger, "propagate", True)
    with caplog.at_level("WARNING"):
        preflight._release_twisted_reactor_threads()
    assert any("thread pool" in r.getMessage() for r in caplog.records)


def test_connectivity_error_text_redacts_credential_values(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: "10105152"
            broker: ctrader

        deployments: []
        """,
    )
    secret_token = "super-secret-access-token-123"

    class _Adapter:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            raise RuntimeError(f"auth rejected for token {secret_token}")

        async def disconnect(self):
            return None

    from live_runner import preflight

    monkeypatch.setattr(preflight, "CTraderLiveAdapter", _Adapter)
    env = {**_CTRADER_ENV, "CTRADER_ACCESS_TOKEN": secret_token}
    result = run_live_preflight(config_path, env=env, check_connectivity=True)
    errors = " ".join(result.accounts[0].errors)
    assert "cTrader connectivity check failed" in errors
    assert secret_token not in errors


# ─── ${VAR} expansion honours --accounts-env (#184 finding 6) ───────────────────


_ENV_REF_CONFIG = """
    accounts:
      - id: alpaca-paper
        broker: alpaca

    deployments:
      - strategy_name: discord_follow
        account_id: alpaca-paper
        symbols: [AAPL]
        timeframe: 1h
        signal_source: discord
        signal_config:
          channel_id: "${PREFLIGHT_TEST_CHANNEL_ID}"
    """


def test_live_preflight_expands_config_refs_from_accounts_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PREFLIGHT_TEST_CHANNEL_ID", raising=False)
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("PREFLIGHT_TEST_CHANNEL_ID=123456\n", encoding="utf-8")

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert not any("PREFLIGHT_TEST_CHANNEL_ID" in err for err in result.deployment_errors)
    assert not any("unexpanded" in err.lower() for err in result.deployment_errors)


def test_live_preflight_accounts_env_file_wins_over_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same file-first precedence as credentials (resolve_env_value).
    monkeypatch.setenv("PREFLIGHT_TEST_CHANNEL_ID", "from-process")
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("PREFLIGHT_TEST_CHANNEL_ID=from-file\n", encoding="utf-8")
    seen: dict[str, object] = {}

    import live_runner.preflight as preflight

    real = preflight.parse_live_deployments_config

    def spy(path, *, env_values=None):
        accounts, deployments = real(path, env_values=env_values)
        seen["channel"] = deployments[0].signal_config["channel_id"]
        return accounts, deployments

    monkeypatch.setattr(preflight, "parse_live_deployments_config", spy)
    run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert seen["channel"] == "from-file"


def test_live_preflight_still_expands_from_process_env_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PREFLIGHT_TEST_CHANNEL_ID", "777")
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)

    result = run_live_preflight(
        config_path, accounts_env_path=tmp_path / "missing.accounts.env"
    )

    assert not any("PREFLIGHT_TEST_CHANNEL_ID" in err for err in result.deployment_errors)


def test_live_preflight_unset_config_ref_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PREFLIGHT_TEST_CHANNEL_ID", raising=False)
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)

    result = run_live_preflight(
        config_path, accounts_env_path=tmp_path / "missing.accounts.env"
    )

    assert result.ok is False
    assert any("PREFLIGHT_TEST_CHANNEL_ID" in err for err in result.deployment_errors)


def test_live_preflight_explicit_env_mapping_is_not_merged_with_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``env=`` mapping is used as-is: a hermetic caller passing
    ``env={}`` to prove an unset reference fails must not have that reference
    silently expanded from whatever the runner's process environment happens to
    define - the preflight is the gate before live orders are enabled."""
    monkeypatch.setenv("PREFLIGHT_TEST_CHANNEL_ID", "from-process")
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)

    result = run_live_preflight(config_path, env={})

    assert result.ok is False
    assert any("PREFLIGHT_TEST_CHANNEL_ID" in err for err in result.deployment_errors)


def test_live_preflight_empty_file_value_wins_over_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-empty file key shadows the process environment during
    ${VAR} expansion, exactly like ``resolve_env_value`` does for credentials:
    a key an operator blanked to disable/invalidate it must not silently fall
    back to a stale process-env value."""
    monkeypatch.setenv("PREFLIGHT_TEST_CHANNEL_ID", "from-process")
    config_path = _write_config(tmp_path / "live_deployments.yaml", _ENV_REF_CONFIG)
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("PREFLIGHT_TEST_CHANNEL_ID=\n", encoding="utf-8")
    seen: dict[str, object] = {}

    import live_runner.preflight as preflight

    real = preflight.parse_live_deployments_config

    def spy(path, *, env_values=None):
        accounts, deployments = real(path, env_values=env_values)
        seen["channel"] = deployments[0].signal_config["channel_id"]
        return accounts, deployments

    monkeypatch.setattr(preflight, "parse_live_deployments_config", spy)
    run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert seen["channel"] == ""


def test_live_preflight_unreadable_accounts_env_reports_failure_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-unreadable accounts env file is a config error, not a crash.

    Every other accounts-env read in live_runner goes through
    ``load_accounts_env_values``, which warns and continues with the process
    environment; preflight must not raise an unhandled OSError traceback just
    because the YAML parse now reads the same file.
    """
    import live_runner.credential_env as credential_env

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
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("ALPACA_API_KEY=file-key\n", encoding="utf-8")

    def _deny(_path: object) -> dict[str, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(credential_env, "dotenv_values", _deny)

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert any("Missing Alpaca credentials" in e for e in result.accounts[0].errors)


def test_live_preflight_unreadable_accounts_env_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accounts env file is read once per preflight, so its read warning is logged once."""
    import live_runner.credential_env as credential_env

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
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("ALPACA_API_KEY=file-key\n", encoding="utf-8")
    warnings: list[str] = []

    class _Recorder:
        def warning(self, message: str, **_kwargs: object) -> None:
            warnings.append(message)

    def _deny(_path: object) -> dict[str, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(credential_env, "dotenv_values", _deny)
    monkeypatch.setattr(credential_env, "logger", _Recorder())

    run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert warnings == ["Could not read accounts env file; continuing with process env only"]


_SECRET_REF_VALUE = "file-secret-value-0123456789"


def _secret_ref_accounts_env(tmp_path: Path) -> Path:
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        f"PREFLIGHT_TEST_SECRET_REF={_SECRET_REF_VALUE}\n", encoding="utf-8"
    )
    return accounts_env


def test_live_preflight_config_error_never_echoes_expanded_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validation failure must not print an expanded value into the result.

    ${VAR} refs expand before validation, so an offending input can carry a
    credential into ``deployment_errors``, which ``main()`` prints as JSON. The
    cTrader routing check is a path that really echoes the resolved value (it
    names it in its message), so the operator must get the reference instead.
    """
    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader
            ctid: "${PREFLIGHT_TEST_SECRET_REF}"

        deployments:
          - strategy_name: fx_breakout
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )
    accounts_env = _secret_ref_accounts_env(tmp_path)

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert not any(_SECRET_REF_VALUE in err for err in result.deployment_errors)
    # The operator can still tell which reference failed and why.
    assert any("PREFLIGHT_TEST_SECRET_REF" in err for err in result.deployment_errors)
    assert any("numeric" in err for err in result.deployment_errors)


def test_live_preflight_config_error_rewrites_file_values_to_their_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same holds for a value echoed by a hand-raised config error:
    report the reference the operator has to fix, not its expanded value."""
    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader
            broker_login: "${PREFLIGHT_TEST_SECRET_REF}"

        deployments:
          - strategy_name: fx_breakout
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )
    accounts_env = _secret_ref_accounts_env(tmp_path)

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert not any(_SECRET_REF_VALUE in err for err in result.deployment_errors)
    assert any("PREFLIGHT_TEST_SECRET_REF" in err for err in result.deployment_errors)


def test_live_preflight_config_error_rewrites_process_env_values_to_their_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ${VAR} that resolves from the process environment is redacted too.

    The expansion view falls back to os.environ for keys the accounts file does
    not define, so a secret can reach a config error without ever being in the
    file. The message and the warning log must still name the reference only.
    """
    import live_runner.preflight as preflight

    monkeypatch.setenv("PROC_ONLY_SECRET", "supersecretvalue")
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: x
            broker: ctrader
            broker_login: "${PROC_ONLY_SECRET}"

        deployments:
          - strategy_name: fx_breakout
            account_id: x
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("", encoding="utf-8")
    logged: list[str] = []

    class _Recorder:
        def warning(self, message: str, **kwargs: object) -> None:
            logged.append(f"{message} {kwargs}")

    monkeypatch.setattr(preflight, "logger", _Recorder())

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert result.deployment_errors
    assert not any("supersecretvalue" in err for err in result.deployment_errors)
    assert any("${PROC_ONLY_SECRET}" in err for err in result.deployment_errors)
    assert logged, "the config error must be logged as a warning"
    assert not any("supersecretvalue" in line for line in logged)
    assert any("${PROC_ONLY_SECRET}" in line for line in logged)


def test_live_preflight_credentials_are_not_filled_from_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials resolve from the accounts env file only, process env or not.

    Deliberate, and different from both the ${VAR} expansion view above and the
    daemon (which resolves credentials through the env-filled
    ``load_accounts_env_values``): in preflight a key the file does not define
    is "not configured", so a credential that exists only in the process
    environment fails the preflight closed instead of borrowing it. Aligning
    preflight credentials with the daemon is a separate decision; this pins
    today's contract so it cannot drift silently.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "process-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "process-secret")
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

    result = run_live_preflight(
        config_path, accounts_env_path=tmp_path / "missing.accounts.env"
    )

    assert result.ok is False
    assert any("Missing Alpaca credentials" in e for e in result.accounts[0].errors)


def test_config_error_redaction_replaces_longest_value_first() -> None:
    """A shorter value that prefixes a longer one must not cut it short.

    Replacing ``abc`` first would leave ``${SHORT}def`` - the secret's tail
    still in the message. The longer value has to be redacted as a whole.
    """
    from live_runner.preflight import _config_error_message

    message = _config_error_message(
        ValueError("resolved to 'abcdef' here"),
        redact={"SHORT": "abc", "LONG": "abcdef"},
    )

    assert message == "resolved to '${LONG}' here"


def test_config_error_redaction_threshold_is_exactly_three_characters() -> None:
    """Values of three characters are redacted; shorter ones are treated as non-secret."""
    from live_runner.preflight import _config_error_message

    message = _config_error_message(
        ValueError("resolved to 'Zq9' and 'Zq'"),
        redact={"THREE": "Zq9", "TWO": "Zq"},
    )

    assert message == "resolved to '${THREE}' and 'Zq'"


def test_redaction_values_cover_referenced_keys_and_the_accounts_file_only() -> None:
    """Redaction covers the file values and the expansion values the config
    references - never every process-env key, which would mangle messages."""
    from live_runner.preflight import _redaction_values

    expansion_env = {
        "FILE_KEY": "filevalue1",
        "REFERENCED_KEY": "refvalue1",
        "UNREFERENCED_KEY": "otherval1",
    }

    values = _redaction_values(
        env_values={"FILE_KEY": "filevalue1"},
        expansion_env=expansion_env,
        referenced={"REFERENCED_KEY"},
    )

    assert values == {"FILE_KEY": "filevalue1", "REFERENCED_KEY": "refvalue1"}


def test_redaction_values_without_a_read_config_redact_every_expansion_value() -> None:
    """When the config text is unknown (it could not be read), every expansion
    value counts as referenced: the message cannot be checked any other way."""
    from live_runner.preflight import _redaction_values

    values = _redaction_values(
        env_values={"FILE_KEY": "filevalue1"},
        expansion_env={"FILE_KEY": "filevalue1", "PROC_KEY": "procvalue1"},
        referenced=None,
    )

    assert values == {"FILE_KEY": "filevalue1", "PROC_KEY": "procvalue1"}


def test_config_error_message_non_value_error_is_redacted_and_typed() -> None:
    """A TypeError/KeyError from parsing is not a ValueError, but its text must
    be redacted like one and must still name its type."""
    from live_runner.preflight import _config_error_message

    type_error = _config_error_message(
        TypeError("login 'abcdef' rejected"), redact={"SHORT": "abcdef"}
    )
    key_error = _config_error_message(KeyError("abcdef"), redact={"SHORT": "abcdef"})

    assert type_error == "TypeError: login '${SHORT}' rejected"
    assert key_error == "KeyError: '${SHORT}'"


def test_config_error_message_yaml_error_reports_problem_and_position_only() -> None:
    """A YAML syntax error carries a source snippet; that snippet can hold a
    literal secret, so only the problem and its position are reported."""
    import yaml

    from live_runner.preflight import _config_error_message

    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load("accounts: [ literalsecret-abc\n  x: y ]\n")

    message = _config_error_message(caught.value, redact={})

    assert message.startswith("ParserError: expected ',' or ']'")
    assert "line 2, column 4" in message
    assert "literalsecret-abc" not in message
    assert "accounts: [" not in message


def test_config_error_message_validation_error_without_title_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``title`` attribute is not relied on: without it the error type names the message."""
    import live_runner.preflight as preflight

    class _TitlelessValidationError(ValueError):
        def errors(self) -> list[dict[str, object]]:
            return [{"loc": ("broker",), "msg": "bad broker"}]

    monkeypatch.setattr(preflight, "ValidationError", _TitlelessValidationError)

    message = preflight._config_error_message(_TitlelessValidationError("x"), redact={})

    assert message == "_TitlelessValidationError (1 validation error): broker: bad broker"


def test_live_preflight_validation_error_message_names_model_field_and_count(
    tmp_path: Path,
) -> None:
    """A real pydantic failure is reported as ``Title (N validation error): loc: msg``."""
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: bad
            broker: kraken

        deployments:
          - strategy_name: fx_breakout
            account_id: bad
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )

    result = run_live_preflight(
        config_path, accounts_env_path=tmp_path / "missing.accounts.env"
    )

    assert result.ok is False
    assert len(result.deployment_errors) == 1
    assert result.deployment_errors[0].startswith(
        "LiveAccountConfig (1 validation error): broker: "
    )


_NON_VALUE_ERROR_CONFIG = """
accounts:
  - id: ctrader-demo
    broker: ctrader
    broker_login: "${PREFLIGHT_TEST_SECRET_REF}"

deployments:
  - strategy_name: fx_breakout
    account_id: ctrader-demo
    symbols: [EURUSD]
    timeframe: 15m
"""


@pytest.mark.parametrize("error_type", [TypeError, KeyError])
def test_live_preflight_non_value_error_during_parse_never_echoes_expanded_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    """A TypeError/KeyError raised while parsing an expanded config must be
    redacted like a ValueError: it is printed in deployment_errors and logged."""
    import live_runner.config_parser as config_parser
    import live_runner.preflight as preflight

    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
    config_path = _write_config(tmp_path / "live_deployments.yaml", _NON_VALUE_ERROR_CONFIG)
    accounts_env = _secret_ref_accounts_env(tmp_path)
    logged: list[str] = []

    class _Recorder:
        def warning(self, message: str, **kwargs: object) -> None:
            logged.append(f"{message} {kwargs}")

    def _reject(accounts: list[object], *_args: object, **_kwargs: object) -> None:
        raise error_type(f"login {accounts[0].broker_login} is not routable")  # type: ignore[attr-defined]

    monkeypatch.setattr(config_parser, "validate_live_account_routes", _reject)
    monkeypatch.setattr(preflight, "logger", _Recorder())

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert len(result.deployment_errors) == 1
    assert result.deployment_errors[0].startswith(f"{error_type.__name__}: ")
    assert not any(_SECRET_REF_VALUE in err for err in result.deployment_errors)
    assert "${PREFLIGHT_TEST_SECRET_REF}" in result.deployment_errors[0]
    assert logged and not any(_SECRET_REF_VALUE in line for line in logged)


def test_live_preflight_yaml_syntax_error_never_echoes_a_source_line(
    tmp_path: Path,
) -> None:
    """The runbook promises a parse error never shows an expanded value. A YAML
    syntax error is raised before expansion, but its snippet echoes the raw line
    the operator wrote, so the snippet is dropped."""
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text("accounts: [ literalsecret-abc\n  x: y ]\n", encoding="utf-8")

    result = run_live_preflight(config_path, accounts_env_path=tmp_path / "missing.env")

    assert result.ok is False
    assert len(result.deployment_errors) == 1
    assert result.deployment_errors[0].startswith("ParserError: ")
    assert "literalsecret-abc" not in result.deployment_errors[0]


def test_live_preflight_bare_dollar_reference_expands_and_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$NAME`` (no braces) is a reference too: it expands from the accounts
    file and its value is redacted from config errors like ``${NAME}``."""
    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
        accounts:
          - id: ctrader-demo
            broker: ctrader
            ctid: $PREFLIGHT_TEST_SECRET_REF

        deployments:
          - strategy_name: fx_breakout
            account_id: ctrader-demo
            symbols: [EURUSD]
            timeframe: 15m
        """,
    )
    accounts_env = _secret_ref_accounts_env(tmp_path)

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    assert result.ok is False
    assert not any(_SECRET_REF_VALUE in err for err in result.deployment_errors)
    # The reference was expanded (its value was found and rewritten to the name).
    assert any("${PREFLIGHT_TEST_SECRET_REF}" in err for err in result.deployment_errors)


def test_bare_dollar_reference_expands_to_its_value() -> None:
    from live_runner.config_parser import expand_env_mapping

    assert expand_env_mapping({"symbols": ["$SYM", "${SYM}x"]}, {"SYM": "AAPL"}) == {
        "symbols": ["AAPL", "AAPLx"]
    }


def test_live_preflight_crash_path_redacts_expanded_values_in_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhandled exception after a successful expansion reaches main()'s
    crash document; it must not print the expanded value."""
    import json

    import live_runner.preflight as preflight
    from shared import logging as shared_logging

    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
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
            signal_source: discord
            signal_config:
              channel_id: "${PREFLIGHT_TEST_SECRET_REF}"
        """,
    )
    accounts_env = _secret_ref_accounts_env(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"deployment check saw {_SECRET_REF_VALUE}")

    monkeypatch.setattr(preflight, "_validate_deployments", _boom)
    monkeypatch.setattr(shared_logging, "_loggers", {})
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(shared_logging, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger("preflight-crash-redact"))

    rc = preflight.main(
        ["--config", str(config_path), "--accounts-env", str(accounts_env)]
    )

    captured = capsys.readouterr()
    assert _SECRET_REF_VALUE not in captured.out
    document = json.loads(captured.out)
    assert rc == 1
    assert document["deployment_errors"] == [
        "RuntimeError: deployment check saw ${PREFLIGHT_TEST_SECRET_REF}"
    ]


def test_live_preflight_unreadable_config_crash_redacts_all_expansion_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a readable config the references are unknown, so the crash
    document redacts every expansion value rather than risk printing one."""
    import json

    import live_runner.preflight as preflight
    from shared import logging as shared_logging

    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
    accounts_env = _secret_ref_accounts_env(tmp_path)
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text("accounts: []\n", encoding="utf-8")

    def _unreadable(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"cannot read config while holding {_SECRET_REF_VALUE}")

    monkeypatch.setattr(preflight, "parse_live_deployments_config", _unreadable)
    monkeypatch.setattr(shared_logging, "_loggers", {})
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(shared_logging, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger("preflight-unreadable"))

    rc = preflight.main(["--config", str(config_path), "--accounts-env", str(accounts_env)])

    captured = capsys.readouterr()
    assert rc == 1
    assert _SECRET_REF_VALUE not in captured.out
    document = json.loads(captured.out)
    assert document["deployment_errors"] == [
        "OSError: cannot read config while holding ${PREFLIGHT_TEST_SECRET_REF}"
    ]


def test_live_preflight_crash_traceback_redacts_expanded_values_everywhere(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-parse crash whose exception text holds an expanded value must not
    print that value to stderr, the log file, or stdout. The traceback itself
    stays: operators need it, with the value rewritten to its ``${VAR}`` reference."""
    import json

    import live_runner.preflight as preflight
    from shared import logging as shared_logging

    monkeypatch.delenv("PREFLIGHT_TEST_SECRET_REF", raising=False)
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
            signal_source: discord
            signal_config:
              channel_id: "${PREFLIGHT_TEST_SECRET_REF}"
        """,
    )
    accounts_env = _secret_ref_accounts_env(tmp_path)
    log_dir = tmp_path / "logs"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"deployment check saw {_SECRET_REF_VALUE}")

    monkeypatch.setattr(preflight, "_validate_deployments", _boom)
    monkeypatch.setattr(shared_logging, "_loggers", {})
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(shared_logging, "LOG_DIR", log_dir)
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger("preflight-crash-tb"))

    rc = preflight.main(["--config", str(config_path), "--accounts-env", str(accounts_env)])

    captured = capsys.readouterr()
    assert rc == 1
    assert _SECRET_REF_VALUE not in captured.out
    assert _SECRET_REF_VALUE not in captured.err
    # The traceback is kept, and its exception text is redacted.
    assert "Traceback" in captured.err
    assert "RuntimeError: deployment check saw ${PREFLIGHT_TEST_SECRET_REF}" in captured.err

    log_records = [
        json.loads(line)
        for line in (log_dir / "preflight-crash-tb.log").read_text(encoding="utf-8").splitlines()
    ]
    crash_records = [r for r in log_records if r["message"].startswith("Live preflight crashed")]
    assert len(crash_records) == 1
    assert crash_records[0]["level"] == "ERROR"
    assert "Traceback" in crash_records[0]["message"]
    expected_line = "RuntimeError: deployment check saw ${PREFLIGHT_TEST_SECRET_REF}"
    assert expected_line in crash_records[0]["message"]
    assert _SECRET_REF_VALUE not in json.dumps(log_records)

    document = json.loads(captured.out)
    assert document["deployment_errors"] == [
        "RuntimeError: deployment check saw ${PREFLIGHT_TEST_SECRET_REF}"
    ]


def test_live_preflight_crash_without_redaction_set_prints_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the accounts env file cannot be resolved, no redaction set exists, so a
    crash must print neither its traceback nor its message, only the type."""
    import json

    import live_runner.preflight as preflight
    from shared import logging as shared_logging

    config_path = _write_config(tmp_path / "live_deployments.yaml", "accounts: []\n")

    def _unresolvable(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"cannot resolve accounts env holding {_SECRET_REF_VALUE}")

    monkeypatch.setattr(preflight, "_accounts_env_views", _unresolvable)
    monkeypatch.setattr(shared_logging, "_loggers", {})
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(shared_logging, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger("preflight-no-redact"))

    rc = preflight.main(["--config", str(config_path), "--accounts-env", str(tmp_path / "x.env")])

    captured = capsys.readouterr()
    assert rc == 1
    assert _SECRET_REF_VALUE not in captured.out
    assert _SECRET_REF_VALUE not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.out)["deployment_errors"] == ["OSError"]


def test_live_preflight_ctrader_invalid_demo_value_is_redacted(tmp_path: Path) -> None:
    """CTRADER_DEMO is parsed as a boolean; a mistyped secret in it must not be
    echoed by the boolean error into the account's errors."""
    secret_demo_value = "sk-live-9f8e7d6c"
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        "CTRADER_APP_ID=app-id-1\n"
        "CTRADER_APP_SECRET=app-secret-1\n"
        "CTRADER_ACCESS_TOKEN=access-token-1\n"
        f"CTRADER_DEMO={secret_demo_value}\n",
        encoding="utf-8",
    )
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

    result = run_live_preflight(config_path, accounts_env_path=accounts_env)

    account_errors = result.accounts[0].errors
    assert result.ok is False
    assert len(account_errors) == 1
    assert "Invalid boolean value for CTRADER_DEMO" in account_errors[0]
    assert secret_demo_value not in account_errors[0]
    assert "'${CTRADER_DEMO}'" in account_errors[0]


def test_redact_values_matches_a_value_in_its_repr_escaped_form() -> None:
    """Exception text often quotes a value with ``!r``; a backslash or quote in
    the value is then escaped, so the raw value alone would not match."""
    from live_runner.preflight import _redact_values

    value = "tok\\en-'quoted'-secret"
    message = f"Invalid value: {value!r}. Raw: {value}"

    reference = "${TOKEN}"
    # repr picks the quote character; redaction keeps it and swaps only the value.
    quote = repr(value)[0]
    assert _redact_values(message, {"TOKEN": value}) == (
        f"Invalid value: {quote}{reference}{quote}. Raw: {reference}"
    )


# ─── stdout is exactly one JSON document (#184 finding 8) ───────────────────────


def test_preflight_cli_stdout_is_a_single_json_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import live_runner.preflight as preflight

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
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("", encoding="utf-8")

    # Build the console handler the way a fresh process does: bound to the stdout
    # in effect when the logger is created (here pytest's capture).
    from shared import logging as shared_logging

    monkeypatch.setattr(shared_logging, "_loggers", {})
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger("preflight-stdout-test"))

    rc = preflight.main(["--config", str(config_path), "--accounts-env", str(accounts_env)])

    captured = capsys.readouterr()
    document = json.loads(captured.out)  # raises if any log line is interleaved
    assert document["config_path"] == str(config_path)
    assert rc == 1  # no credentials for the alpaca account
    assert "Running live preflight" in captured.err


@pytest.mark.parametrize("case", ["missing_config", "malformed_yaml"])
def test_preflight_cli_emits_json_document_even_when_config_cannot_load(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    import json

    import live_runner.preflight as preflight
    from shared import logging as shared_logging

    if case == "missing_config":
        config_path = tmp_path / "missing_live_deployments.yaml"
    else:
        config_path = _write_config(tmp_path / "live_deployments.yaml", "accounts: [\n")
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("", encoding="utf-8")

    monkeypatch.setattr(shared_logging, "_loggers", {})
    # main() switches the module-global console stream to stderr; restore it after
    # the test so later loggers on this worker keep the shipped stdout default.
    monkeypatch.setattr(shared_logging, "_console_stream_name", "stdout")
    monkeypatch.setattr(shared_logging, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(preflight, "logger", shared_logging.get_logger(f"preflight-crash-{case}"))

    rc = preflight.main(["--config", str(config_path), "--accounts-env", str(accounts_env)])

    captured = capsys.readouterr()
    # The documented contract holds on failure too: stdout is one parseable document.
    document = json.loads(captured.out)
    assert rc == 1
    assert document["ok"] is False
    assert document["config_path"] == str(config_path)
    assert document["accounts"] == []
    assert len(document["deployment_errors"]) == 1
    expected_type = "FileNotFoundError" if case == "missing_config" else "ParserError"
    assert document["deployment_errors"][0].startswith(f"{expected_type}: ")
    # A missing config is an unhandled crash: its traceback goes to stderr, not
    # stdout. A YAML syntax error is a reported config error, logged as a warning
    # with no traceback.
    if case == "missing_config":
        assert "Traceback" in captured.err
    else:
        assert "Traceback" not in captured.err
        assert "Live deployment config invalid" in captured.err
