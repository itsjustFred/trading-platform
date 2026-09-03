from __future__ import annotations

import os
from pathlib import Path

import pytest

from live_runner.credential_env import (
    accounts_env_mtime,
    load_accounts_env_values,
    parse_bool_env,
)


def _write_env(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_bool_env_missing_value_returns_default() -> None:
    assert parse_bool_env("SOME_FLAG", None, default=True) is True
    assert parse_bool_env("SOME_FLAG", None, default=False) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" y ", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("off", False),
    ],
)
def test_parse_bool_env_recognized_tokens(raw: str, expected: bool) -> None:
    assert parse_bool_env("SOME_FLAG", raw, default=True) is expected


@pytest.mark.parametrize("raw", ["ture", "eanbled", "", "  ", "2", "wahr"])
def test_parse_bool_env_typo_raises(raw: str) -> None:
    with pytest.raises(ValueError, match="SOME_FLAG"):
        parse_bool_env("SOME_FLAG", raw, default=True)


def test_parse_bool_env_error_lists_valid_values() -> None:
    with pytest.raises(ValueError, match="false") as exc_info:
        parse_bool_env("SOME_FLAG", "wahr", default=True)
    assert "true" in str(exc_info.value)


def test_load_accounts_env_values_reads_file(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / ".accounts.env", "ALPACA_API_KEY=file-key\nCTRADER_DEMO=false\n"
    )

    values = load_accounts_env_values(env_file, environ={})

    assert values == {"ALPACA_API_KEY": "file-key", "CTRADER_DEMO": "false"}


def test_load_accounts_env_values_file_wins_over_environ(tmp_path: Path) -> None:
    """File-first precedence: .accounts.env beats the process environment."""
    env_file = _write_env(
        tmp_path / ".accounts.env",
        "ALPACA_API_KEY=file-key\nCTRADER_DEMO=false\n",
    )

    values = load_accounts_env_values(
        env_file,
        environ={"ALPACA_API_KEY": "os-key", "CTRADER_DEMO": "true"},
    )

    assert values["ALPACA_API_KEY"] == "file-key"
    assert values["CTRADER_DEMO"] == "false"


def test_load_accounts_env_values_environ_fills_missing_keys(tmp_path: Path) -> None:
    """Keys the file does not define are filled from the environment."""
    env_file = _write_env(tmp_path / ".accounts.env", "ALPACA_API_KEY=file-key\n")

    values = load_accounts_env_values(
        env_file,
        environ={"ALPACA_SECRET_KEY": "os-secret", "OTHER": "x"},
    )

    assert values["ALPACA_API_KEY"] == "file-key"
    assert values["ALPACA_SECRET_KEY"] == "os-secret"
    assert values["OTHER"] == "x"


def test_load_accounts_env_values_missing_file_uses_environ_only(tmp_path: Path) -> None:
    values = load_accounts_env_values(
        tmp_path / "does-not-exist.env",
        environ={"ALPACA_API_KEY": "os-key"},
    )

    assert values == {"ALPACA_API_KEY": "os-key"}


def test_load_accounts_env_values_unreadable_file_warns_and_uses_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that exists but cannot be read is a config error: discovery decides
    which accounts exist from this result, so it must not silently look like
    "no accounts configured"."""
    import live_runner.credential_env as credential_env_module

    unreadable = tmp_path / ".accounts.env"
    unreadable.write_text("ALPACA_API_KEY=file-key\n", encoding="utf-8")

    def _raise_oserror(_path: object) -> dict[str, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(credential_env_module, "dotenv_values", _raise_oserror)
    warnings: list[str] = []

    def _capture_warning(message: str, extra: dict | None = None) -> None:
        warnings.append(message)

    monkeypatch.setattr(credential_env_module.logger, "warning", _capture_warning)

    values = load_accounts_env_values(unreadable, environ={"ALPACA_API_KEY": "os-key"})

    assert values == {"ALPACA_API_KEY": "os-key"}
    assert any("Could not read accounts env file" in message for message in warnings)


def test_load_accounts_env_values_strips_and_skips_blank(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".accounts.env", "A = spaced \nB=\n")

    values = load_accounts_env_values(env_file, environ={})

    assert values["A"] == "spaced"
    # A present-but-empty value stays present (and empty): it must not be
    # silently replaced by the environment, which would invert precedence.
    assert values["B"] == ""


def test_load_accounts_env_values_defaults_to_process_environ(tmp_path: Path, monkeypatch) -> None:
    env_file = _write_env(tmp_path / ".accounts.env", "ALPACA_API_KEY=file-key\n")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "os-secret")

    values = load_accounts_env_values(env_file)

    assert values["ALPACA_API_KEY"] == "file-key"
    assert values["ALPACA_SECRET_KEY"] == "os-secret"


def test_accounts_env_mtime_missing_file_returns_none(tmp_path: Path) -> None:
    assert accounts_env_mtime(tmp_path / "nope.env") is None


def test_accounts_env_mtime_changes_on_write(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".accounts.env", "A=1\n")
    before = accounts_env_mtime(env_file)
    assert before is not None

    import time

    time.sleep(0.01)
    _write_env(env_file, "A=2\n")

    assert accounts_env_mtime(env_file) != before


def test_service_discovery_prefers_file_over_environ(tmp_path: Path, monkeypatch) -> None:
    """B-9: discovery must resolve the file first, not let os.environ win."""
    import live_runner.service_discovery as service_discovery

    env_file = _write_env(
        tmp_path / ".accounts.env",
        "DISCORD_BOT_ALPHA_TOKEN=file-token\nDISCORD_BOT_ALPHA_CHANNEL_ID=file-channel\n",
    )
    monkeypatch.setattr(service_discovery, "DEFAULT_ACCOUNTS_ENV", env_file)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "DISCORD_BOT_ALPHA_TOKEN": "os-token",
            "DISCORD_BOT_ALPHA_CHANNEL_ID": "os-channel",
        },
    )

    discovered = service_discovery.discover_discord_bots_from_env()

    assert discovered == [
        {
            "name": "ALPHA",
            "token_env_key": "DISCORD_BOT_ALPHA_TOKEN",
            "channel_id": "file-channel",
        }
    ]


def test_daemon_load_accounts_env_values_is_file_first(tmp_path: Path, monkeypatch) -> None:
    """daemon._load_accounts_env_values delegates to the shared file-first resolver."""
    import live_runner.daemon as daemon

    env_file = _write_env(tmp_path / ".accounts.env", "CTRADER_DEMO=false\n")
    monkeypatch.setenv("CTRADER_DEMO", "true")

    assert daemon._load_accounts_env_values(env_file)["CTRADER_DEMO"] == "false"


def test_discovery_accounts_env_is_file_first(tmp_path: Path, monkeypatch) -> None:
    """account_discovery must not let os.environ overwrite .accounts.env values."""
    import live_runner.account_discovery as account_discovery

    env_file = _write_env(tmp_path / ".accounts.env", "CTRADER_APP_ID=file-app\n")
    monkeypatch.setenv("CTRADER_APP_ID", "os-app")
    monkeypatch.setattr(account_discovery, "DEFAULT_ACCOUNTS_ENV", env_file)

    captured: dict[str, object] = {}

    def _capture_load(accounts_env_path, **_kwargs):
        from live_runner.credential_env import load_accounts_env_values as _real_load

        values = _real_load(accounts_env_path, environ={})
        captured.update(values)
        return values

    monkeypatch.setattr(account_discovery, "load_accounts_env_values", _capture_load)

    async def _none_discovery(*_args, **_kwargs):
        return []

    monkeypatch.setattr(account_discovery, "discover_ctrader_accounts", _none_discovery)

    accounts = asyncio_run(account_discovery.discover_configured_accounts(accounts_env=env_file))

    assert accounts == []
    assert captured == {"CTRADER_APP_ID": "file-app"}
    # os.environ must not have been mutated by discovery.
    assert os.environ["CTRADER_APP_ID"] == "os-app"


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
