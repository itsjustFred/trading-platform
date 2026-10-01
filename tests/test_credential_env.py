from __future__ import annotations

import os
from pathlib import Path

import pytest

import live_runner.credential_env as credential_env

from live_runner.credential_env import (
    accounts_env_mtime,
    load_accounts_env_values,
    parse_bool_env,
    resolve_credential_value,
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


def test_a_missing_accounts_env_file_is_the_normal_single_machine_setup(tmp_path):
    """No `.accounts.env` must still yield the process environment.

    The single-machine env-only setup this branch exists for was untested: every
    fixture creates the file first. Getting it backwards means a total failure to
    start for anyone running credentials from the environment alone.

    The `FileNotFoundError` handler itself is an **equivalent mutant**, verified rather
    than assumed: `dotenv_values` returns an empty mapping for a missing path instead
    of raising, so that `except` clause is unreachable and narrowing it to
    `except ValueError` changes nothing observable. The behaviour is nonetheless the
    contract - a missing file must not stop the environment from being read - so it is
    pinned at the level where it is actually reachable, and the unreachable clause is
    recorded here rather than given a contrived test.

    Also checked: a directory in place of the file, which `dotenv_values` also reads
    as empty rather than raising.
    """
    missing = tmp_path / "no-such-file.env"
    assert not missing.exists()

    values = load_accounts_env_values(
        missing, environ={"CTRADER_APP_ID": "from-process-env"}
    )
    assert values == {"CTRADER_APP_ID": "from-process-env"}, (
        "a missing accounts file did not yield the process environment: " + repr(values)
    )

    as_directory = tmp_path / "accounts-dir.env"
    as_directory.mkdir()
    from_dir = load_accounts_env_values(
        as_directory, environ={"CTRADER_APP_ID": "from-process-env"}
    )
    assert from_dir.get("CTRADER_APP_ID") == "from-process-env", (
        "a directory in place of the accounts file lost the environment values: "
        + repr(from_dir)
    )

    # Both an empty file and an unreadable one behave the same way: the environment
    # is still what remains, which is what discovery decides from.
    unreadable = tmp_path / "unreadable.env"
    unreadable.write_text("CTRADER_APP_ID=file-value\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        result = load_accounts_env_values(
            unreadable, environ={"CTRADER_APP_SECRET": "env-secret"}
        )
        assert result.get("CTRADER_APP_SECRET") == "env-secret", (
            "an unreadable accounts file cost the process environment: " + repr(result)
        )
    finally:
        unreadable.chmod(0o600)


def test_a_non_string_file_value_is_ignored_rather_than_stringified(tmp_path):
    """Only `str` values from the file are kept, and a non-string is dropped.

    The loop guards `isinstance(value, str)`. Replacing it with `str(value or "")`
    leaves every test green, because a parsed `.accounts.env` yields strings or `None`
    - so the guard's purpose is only visible with a value `dotenv_values` cannot
    produce on its own.

    Which is exactly why it is worth pinning: `None` is the one real case, and
    `str(None or "")` would store an **empty string** for it, turning "this key is
    commented out" into "this key is defined and empty". Since a present-but-empty
    file value deliberately wins over the environment, that would shadow a working
    process-env credential with nothing.

    Simulated through a patched `dotenv_values`, because the divergence cannot be
    reached through the real parser.
    """
    monkeypatched = {"X_OK": "value", "X_NONE": None, "X_EMPTY": "", "X_NUM": 7}
    real = credential_env.dotenv_values

    def _fake(path):
        if str(path).endswith("fake.env"):
            return monkeypatched
        return real(path)

    original = credential_env.dotenv_values
    credential_env.dotenv_values = _fake
    try:
        values = credential_env.load_accounts_env_values(
            "ignored/fake.env", environ={"X_NUM": "from-env", "X_NONE": "env-none"}
        )
    finally:
        credential_env.dotenv_values = original

    assert values["X_OK"] == "value"
    assert values["X_EMPTY"] == "", "a genuinely empty file value must stay empty"
    # The None value is dropped, so the environment may fill it.
    assert values["X_NONE"] == "env-none", (
        "a None file value was stored as empty, shadowing the environment with "
        "nothing: " + repr(values.get("X_NONE"))
    )
    # A non-string is dropped too, rather than stringified into a credential.
    assert values.get("X_NUM") == "from-env", (
        "a non-string file value was stringified instead of dropped: "
        + repr(values.get("X_NUM"))
    )


class TestResolveEnvValue:
    """C1: file-first precedence with absent-vs-empty distinction."""

    def test_file_value_wins_over_process_env(self, monkeypatch) -> None:
        from live_runner.credential_env import resolve_env_value

        monkeypatch.setenv("C1_TEST_KEY", "stale-env")
        assert resolve_env_value({"C1_TEST_KEY": "fresh-file"}, "C1_TEST_KEY") == "fresh-file"

    def test_present_but_empty_stays_empty(self, monkeypatch) -> None:
        """The `or`-fallback hole: '' must not fall through to os.environ."""
        from live_runner.credential_env import resolve_env_value

        monkeypatch.setenv("C1_TEST_KEY", "stale-env")
        assert resolve_env_value({"C1_TEST_KEY": ""}, "C1_TEST_KEY") == ""

    def test_absent_key_falls_back_to_process_env(self, monkeypatch) -> None:
        from live_runner.credential_env import resolve_env_value

        monkeypatch.setenv("C1_TEST_KEY", "env-only")
        assert resolve_env_value({}, "C1_TEST_KEY") == "env-only"

    def test_missing_everywhere_returns_default(self, monkeypatch) -> None:
        from live_runner.credential_env import resolve_env_value

        monkeypatch.delenv("C1_TEST_KEY", raising=False)
        assert resolve_env_value({}, "C1_TEST_KEY") == ""
        assert resolve_env_value(None, "C1_TEST_KEY", default="d") == "d"


class TestAdapterEnvValues:
    def test_alpaca_adapter_prefers_env_values_map(self, monkeypatch) -> None:
        from live_runner import alpaca_adapter as module

        monkeypatch.setenv("ALPACA_API_KEY", "stale-env-key")
        adapter = module.AlpacaLiveAdapter(
            account_id="test",
            env_values={"ALPACA_API_KEY": "file-key", "ALPACA_SECRET_KEY": "file-secret"},
        )
        assert adapter.api_key == "file-key"
        assert adapter.secret_key == "file-secret"

    def test_alpaca_adapter_empty_file_value_not_shadowed(self, monkeypatch) -> None:
        from live_runner import alpaca_adapter as module

        monkeypatch.setenv("ALPACA_API_KEY", "stale-env-key")
        with pytest.raises(ValueError, match="Missing Alpaca credentials"):
            module.AlpacaLiveAdapter(
                account_id="test",
                env_values={"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": "s"},
            )


# --------------------------------------------------------------------------
# resolve_credential_value: the per-key resolver had no tests at all
# --------------------------------------------------------------------------
#
# Found by mutation testing `live_runner/credential_env.py` with `mutate.sh`.
# Two mutations survived 423 collected tests. They are not the same kind of thing:
#
#   - **swapping the two branches so the environment wins over the file.** A real
#     gap. Inverting it silently resolves a credential from the environment even
#     when the file supplies one - measured as the *wrong broker account*, not a
#     wrong value. Pinned below.
#   - **replacing `get(key) or ""` with `get(key, "")`.** An *equivalent mutant*
#     for any typed caller: the two differ only when the value is an explicit
#     `None`, which a `Mapping[str, str]` parsed from an env file cannot contain.
#     Under the mutation a `None` would become the literal credential `"None"`,
#     which is a genuine hazard but needs a caller that violates the annotation.
#
# The loader beside it, `load_accounts_env_values`, has eight tests including
# `test_load_accounts_env_values_file_wins_over_environ`. So there were two
# precedence decisions in this module and only one was pinned.
#
# Separately, and recorded in `notes/backlog.md` rather than fixed here: a
# *blanked* credential currently falls back to the environment, while the
# docstring says the environment "only fills keys the file does not define".
# That is a live divergence from the module's own documentation - the same
# collapse `parse_bool_env`'s docstring blames for two earlier live bugs - and
# changing credential resolution on the live path is the human's call. The last
# test here pins today's behaviour so it cannot change silently.


def test_resolve_credential_value_is_file_first_when_both_define_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills the env-first mutation: the file's value must win, not the environment's."""
    monkeypatch.setenv("CTRADER_APP_ID", "account-A-from-environment")

    resolved = resolve_credential_value({"CTRADER_APP_ID": "account-B-from-file"}, "CTRADER_APP_ID")

    assert resolved == "account-B-from-file"


def test_resolve_credential_value_falls_back_to_environment_when_the_file_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half, so the first cannot be satisfied by always preferring the file."""
    monkeypatch.setenv("CTRADER_APP_ID", "only-in-environment")

    assert resolve_credential_value({}, "CTRADER_APP_ID") == "only-in-environment"


def test_resolve_credential_value_uses_the_env_key_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-key env alias is honoured, and the file still wins over it."""
    monkeypatch.setenv("ACCOUNT_B_APP_ID", "aliased-environment-value")

    values = {"ACCOUNT_B_APP_ID": "from-file"}
    assert resolve_credential_value(values, "ACCOUNT_B_APP_ID", env_key="ACCOUNT_B_APP_ID") == "from-file"

    monkeypatch.delenv("ACCOUNT_B_APP_ID")
    monkeypatch.setenv("ALIASED_APP_ID", "aliased-environment-value")
    assert resolve_credential_value({}, "ACCOUNT_B_APP_ID", env_key="ALIASED_APP_ID") == (
        "aliased-environment-value"
    )


def test_resolve_credential_value_strips_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CTRADER_APP_ID", raising=False)
    assert resolve_credential_value({"CTRADER_APP_ID": "  padded-key  "}, "CTRADER_APP_ID") == "padded-key"


def test_an_empty_key_resolves_to_nothing() -> None:
    """An empty key must short-circuit, not become a lookup of `""`.

    No environment setup here on purpose: an empty-named variable cannot be set
    (`os.environ[""] = ...` raises `OSError: Invalid argument`), so there is no
    value for the empty key to accidentally pick up. The guard is the only thing
    being tested.
    """
    assert resolve_credential_value({"": "value"}, "") == ""
    assert resolve_credential_value({"": "value"}, "", env_key="ALSO_EMPTY") == ""


@pytest.mark.parametrize("blanked", ["", "   "])
def test_a_blanked_credential_currently_falls_back_to_the_environment(
    blanked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins today's behaviour, which contradicts the docstring. See the backlog.

    `or ""` makes a blanked value indistinguishable from an absent one, so the
    environment supplies the credential even though the file *defines* the key.
    This is the same collapse the module's `parse_bool_env` docstring blames for
    two live bugs - there on a boolean whose default was demo mode, here on a
    credential.

    Pinned rather than asserted away: if someone changes it, this test fails and
    points at the decision instead of the behaviour silently changing.
    """
    monkeypatch.setenv("CTRADER_APP_ID", "from-environment")

    assert resolve_credential_value({"CTRADER_APP_ID": blanked}, "CTRADER_APP_ID") == "from-environment"
