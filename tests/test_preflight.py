"""Tests for live_runner.preflight."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from live_runner.preflight import (
    LivePreflightResult,
    _validate_deployments,
    run_live_preflight,
)
from shared.models import LiveAccountConfig, LiveDeploymentConfig


class TestPreflightValidator:
    """Tests for _validate_deployments function."""

    def test_valid_deployment_passes(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ]
        errors, warnings = _validate_deployments(accounts, deployments)
        assert errors == []
        assert warnings == []

    def test_deployment_missing_account_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="unknown_acc",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("unknown account_id" in e for e in errors)

    def test_deployment_no_symbols_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=[],
                timeframe="1h",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("no symbols configured" in e for e in errors)

    def test_deployment_no_timeframe_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("no timeframe configured" in e for e in errors)

    def test_discord_channel_warning(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="1h",
                signal_source="discord",
                signal_config={},
            )
        ]
        _, warnings = _validate_deployments(accounts, deployments)
        assert any("channel_id" in w for w in warnings)


class TestRunLivePreflight:
    """Integration-style tests for run_live_preflight."""

    def test_missing_config_raises_error(self, tmp_path: Path):
        config = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            run_live_preflight(config, check_connectivity=False)

    def test_empty_config_returns_no_accounts(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")
        result = run_live_preflight(config, check_connectivity=False)
        assert result.ok is True
        assert result.accounts == []

    def test_account_without_credentials_fails(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "alpaca"
deployments: []
""")
        result = run_live_preflight(config, check_connectivity=False)
        assert result.ok is False
        assert len(result.accounts) == 1
        assert result.accounts[0].ok is False
        assert any("Missing Alpaca credentials" in e for e in result.accounts[0].errors)

    def test_alpaca_paper_mode_ok(self, tmp_path: Path, monkeypatch):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "alpaca"
deployments: []
""")
        accounts_env = tmp_path / ".accounts.env"
        accounts_env.write_text("""
ALPACA_API_KEY=test_key
ALPACA_SECRET_KEY=test_secret
ALPACA_PAPER=true
""")
        result = run_live_preflight(config, accounts_env_path=accounts_env, check_connectivity=False)
        assert result.ok is True
        assert result.accounts[0].ok is True
        assert result.accounts[0].paper is True

    def test_ctrader_missing_credentials_fails(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "ctrader"
    ctid: "12345"
deployments: []
""")
        # Pass empty env to ensure no credentials are found
        result = run_live_preflight(config, check_connectivity=False, env={})
        assert result.ok is False
        assert result.accounts[0].ok is False
        assert any("Missing cTrader credentials" in e for e in result.accounts[0].errors)


class TestPreflightCLI:
    """Tests for the CLI entry point."""

    def test_main_returns_zero_on_ok(self, tmp_path: Path, monkeypatch):
        from live_runner import preflight as preflight_module

        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")

        with patch.object(preflight_module, "run_live_preflight") as mock_run:
            mock_run.return_value = LivePreflightResult(
                ok=True,
                config_path=str(config),
                accounts_env_path="",
                accounts=[],
            )
            exit_code = preflight_module.main(["--config", str(config)])
            assert exit_code == 0

    def test_main_returns_one_on_fail(self, tmp_path: Path, monkeypatch):
        from live_runner import preflight as preflight_module

        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")

        with patch.object(preflight_module, "run_live_preflight") as mock_run:
            mock_run.return_value = LivePreflightResult(
                ok=False,
                config_path=str(config),
                accounts_env_path="",
                accounts=[],
                deployment_errors=["some error"],
            )
            exit_code = preflight_module.main(["--config", str(config)])
            assert exit_code == 1


class TestPreflightResultSerialization:
    """Tests that preflight results serialize to JSON correctly."""

    def test_result_to_jsonable(self):
        from live_runner.preflight import LivePreflightAccountResult, _result_to_jsonable

        account_result = LivePreflightAccountResult(
            account_id="acc1",
            broker="alpaca",
            ok=True,
            paper=True,
            connected=True,
            balance=10000.0,
            equity=10050.0,
            positions_count=2,
            errors=[],
            warnings=["test warning"],
        )
        result = LivePreflightResult(
            ok=True,
            config_path="/tmp/config.yaml",
            accounts_env_path="/tmp/.accounts.env",
            accounts=[account_result],
        )
        jsonable = _result_to_jsonable(result)
        assert jsonable["ok"] is True
        assert len(jsonable["accounts"]) == 1
        assert jsonable["accounts"][0]["broker"] == "alpaca"
        assert jsonable["accounts"][0]["warnings"] == ["test warning"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestCtraderAccountIdentifierAndEnvFile:
    """Two preflight gates that had no direct test. Four survivors.

    Preflight is the last gate before trading proceeds, so a gate that fails to fire is a gate
    that does not exist. Both of these decide whether an account definition is usable at all,
    and both report by *appending to an error list* rather than raising - which means a broken
    check does not fail loudly, it just quietly permits.

    **The identifier gate distinguishes two failures, and the messages are the contract:**

    | identifier | error |
    |---|---|
    | `0`, `-5` | *must be a positive numeric cTID trader account id* |
    | `abc`, `""`, `1.5` | *must be numeric via ctid, broker_login, or id* |

    They are separate messages because they are separate problems: a non-positive id is a
    plausible-looking value that is still unusable, while a non-numeric one is not an id at all.
    Asserted separately because a mutation to either branch leaves the other passing.

    **`1.5` is refused by the numeric branch even though it is a number.** `int("1.5")` raises,
    so an id carrying a decimal point is reported as non-numeric. That is a false positive
    rather than a safety hole - a cTID is an integer - but it is the kind of thing an operator
    hits at 3am, so it is pinned as a deliberate strictness rather than left to be discovered.

    **The env-file filter drops unparsable values, and dropping is the safer half.** `dotenv`
    returns `None` for a bare key written without `=`, so the `isinstance(value, str)` filter
    removes it. Removing that filter would not keep the value - `str(None).strip()` is the
    literal string `"None"`, so a credential lookup for `BARE` would return the four-character
    string `None` instead of nothing. **A wrong-looking value is worse than an absent one**,
    because an absent key falls through to the environment and a present one does not.

    The `.strip()` is load-bearing only for **quoted** values: `dotenv` already trims unquoted
    ones, but preserves the padding inside quotes. So the test quotes its values - an unquoted
    fixture would pass with the strip removed.
    """

    @staticmethod
    def _account(**kwargs):
        base = {"broker": "ctrader", "id": "acct-1"}
        base.update(kwargs)
        return LiveAccountConfig(**base)

    @pytest.mark.parametrize("bad_id", ["0", "-5", "-1"])
    def test_a_non_positive_cTrader_account_id_is_reported(self, bad_id: str) -> None:
        """A plausible-looking but unusable id is refused, and named as such.

        `0` and `-5` are exactly the values that pass an "is it a number" check while being
        unable to identify an account, which is why the positivity branch exists separately.
        """
        from live_runner.preflight import _validate_ctrader_account_identifier

        errors: list[str] = []
        _validate_ctrader_account_identifier(self._account(ctid=bad_id), errors)
        assert len(errors) == 1, f"expected one error for {bad_id!r}, got {errors}"
        assert "positive numeric" in errors[0], errors[0]

    @pytest.mark.parametrize("bad_id", ["abc", "", "1.5", "12 345", "0x10"])
    def test_a_non_numeric_cTrader_account_id_is_reported(self, bad_id: str) -> None:
        """A different message from the positivity branch, and the distinction is the point.

        An operator seeing "not positive" knows the id parsed; seeing "must be numeric" knows
        it did not. Conflating the two would send someone to look at the wrong thing.
        """
        from live_runner.preflight import _validate_ctrader_account_identifier

        errors: list[str] = []
        returned = _validate_ctrader_account_identifier(self._account(ctid=bad_id), errors)
        assert len(errors) == 1, f"expected one error for {bad_id!r}, got {errors}"
        assert "must be numeric" in errors[0], errors[0]
        assert "positive" not in errors[0], (
            f"the non-numeric case reported the positivity message: {errors[0]}"
        )
        # The identifier is still returned, so the caller can name the offending account.
        assert returned, "the identifier was not returned for the caller to report"

    def test_a_valid_cTrader_account_id_reports_nothing(self) -> None:
        """The pass case, so the guard is not simply always-firing."""
        from live_runner.preflight import _validate_ctrader_account_identifier

        for good in ("12345", "1"):
            errors: list[str] = []
            returned = _validate_ctrader_account_identifier(
                self._account(ctid=good), errors
            )
            assert errors == [], f"{good!r} was refused: {errors}"
            assert returned == good

    def test_the_identifier_falls_back_through_ctid_login_then_id(self) -> None:
        """The precedence the validator inherits from the routing helper.

        Pinned here because the validator's numeric check is what catches a bad fallback, so
        the fallback order and the validation have to agree about which value is checked.
        """
        from live_runner.preflight import _validate_ctrader_account_identifier

        errors: list[str] = []
        assert (
            _validate_ctrader_account_identifier(
                self._account(ctid="12345", broker_login="ignored"), errors
            )
            == "12345"
        )
        errors.clear()
        assert (
            _validate_ctrader_account_identifier(
                self._account(broker_login="777"), errors
            )
            == "777"
        )
        assert errors == []

    # ------------------------------------------------------------------ env file

    def _write_env(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_missing_env_file_is_empty_rather_than_an_error(self, tmp_path: Path) -> None:
        """Absent is normal: the file is optional, so its absence must not stop preflight.

        The alternative - raising - would make an unset optional file fatal, which is the
        opposite of the intent.
        """
        from live_runner.preflight import _load_env_file

        assert _load_env_file(tmp_path / "does-not-exist.env") == {}

    def test_quoted_env_values_are_stripped(self, tmp_path: Path) -> None:
        """Quoted, specifically: `dotenv` already trims unquoted values.

        This is the detail that makes the strip observable. A fixture written as `K=v  ` passes
        with the strip removed, because dotenv has already removed the padding - so the test
        has to use quotes, which dotenv preserves verbatim.
        """
        from live_runner.preflight import _load_env_file

        path = self._write_env(
            tmp_path,
            'QUOTED="  quoted  "\n'
            "SQUOTED='  squoted  '\n"
            "PLAIN=  plain  \n",
        )
        values = _load_env_file(path)
        assert values["QUOTED"] == "quoted", repr(values["QUOTED"])
        assert values["SQUOTED"] == "squoted", repr(values["SQUOTED"])
        assert values["PLAIN"] == "plain", repr(values["PLAIN"])

    def test_a_bare_key_with_no_equals_is_dropped_not_stringified(self, tmp_path: Path) -> None:
        """`BARE` yields `None` from dotenv, and dropping it is the safer outcome.

        The consequence of keeping it is the reason to pin this: `str(None).strip()` is the
        literal four-character string `"None"`, so a credential lookup for `BARE` would return
        `"None"` - a value that looks configured. An absent key falls through to the
        environment; a present-but-`"None"` key does not, and it would be sent to the broker.

        So: **absent is recoverable, wrong-looking is not.**
        """
        from live_runner.preflight import _load_env_file

        path = self._write_env(
            tmp_path, "BARE\nWITH_VALUE=1\nEMPTY=\n"
        )
        values = _load_env_file(path)
        assert "BARE" not in values, f"a bare key became {values.get('BARE')!r}"
        assert "None" not in values.values(), (
            f"a None value was stringified into the env: {values}"
        )
        assert values["WITH_VALUE"] == "1"
        # An explicitly empty value is kept: empty is a deliberate statement, not a parse
        # failure, and the blanked-credential policy depends on being able to see it.
        assert values["EMPTY"] == ""

    def test_keys_are_stripped_so_a_spaced_assignment_is_found(self, tmp_path: Path) -> None:
        """`SPACED_KEY = value` must land under `SPACED_KEY`, not under `" SPACED_KEY "`.

        An unstripped key would not match the environment variable the rest of preflight looks
        up, so the credentials would silently come from somewhere else.
        """
        from live_runner.preflight import _load_env_file

        path = self._write_env(tmp_path, "SPACED_KEY = value\n")
        values = _load_env_file(path)
        assert list(values) == ["SPACED_KEY"], list(values)
        assert values["SPACED_KEY"] == "value"

    def test_an_empty_env_file_is_empty(self, tmp_path: Path) -> None:
        from live_runner.preflight import _load_env_file

        assert _load_env_file(self._write_env(tmp_path, "")) == {}
        assert _load_env_file(self._write_env(tmp_path, "# only a comment\n")) == {}
