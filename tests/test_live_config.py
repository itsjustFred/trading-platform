from __future__ import annotations

import re

from pathlib import Path
from unittest.mock import patch

import pytest

from live_runner.config_parser import parse_live_deployments_config


def test_parse_live_deployments_yaml_valid(tmp_path: Path):
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text(
        """
accounts:
  - id: alpaca-paper
    broker: alpaca

deployments:
  - strategy_name: my_strategy
    account_id: alpaca-paper
    symbols: [AAPL, MSFT]
    timeframe: 1h
""".strip()
        + "\n",
        encoding="utf-8",
    )

    accounts, deployments = parse_live_deployments_config(config_path)

    assert len(accounts) == 1
    assert accounts[0].id == "alpaca-paper"
    assert accounts[0].broker == "alpaca"

    assert len(deployments) == 1
    assert deployments[0].strategy_name == "my_strategy"
    assert deployments[0].account_id == "alpaca-paper"
    assert deployments[0].symbols == ["AAPL", "MSFT"]
    assert deployments[0].timeframe == "1h"


def test_a_missing_config_file_is_a_startup_error(tmp_path: Path):
    """A config path that does not exist must raise, not parse as an empty platform.

    `_validated_config_file` raises `FileNotFoundError` before any reading happens.
    Removing that check leaves every other test green, because they all write a real
    file first - so the case of a *typo in the configured path* is untested.

    This is the shape of the failure that matters at start-up: the daemon is pointed
    at `live_deployments.production.yaml`, the name is wrong, and without the check
    `yaml.safe_load` is never reached but the caller gets whatever a missing read
    produces instead of a clear "not found". An operator seeing an empty account list
    has no way to tell a typo from a genuinely empty config.

    A directory is included too: `is_file()` is false for it, so it must not be read
    either.
    """
    missing = tmp_path / "live_deployments.production.yaml"
    assert not missing.exists()

    with pytest.raises(FileNotFoundError, match="not found"):
        parse_live_deployments_config(missing)

    # A directory is not a config file either.
    as_directory = tmp_path / "config-dir"
    as_directory.mkdir()
    with pytest.raises(FileNotFoundError, match="not found"):
        parse_live_deployments_config(as_directory)

    # And the message names the path that was asked for, so the typo is visible.
    with pytest.raises(FileNotFoundError) as excinfo:
        parse_live_deployments_config(missing)
    assert "live_deployments.production.yaml" in str(excinfo.value), (
        "the error does not name the missing path: " + str(excinfo.value)
    )


def test_parse_live_deployments_warns_for_missing_account(tmp_path: Path):
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text(
        """
accounts:
  - id: ctrader-live
    broker: ctrader
    ctid: "12345"

deployments:
  - strategy_name: orphan_strategy
    account_id: unknown-account
    symbols: [EURUSD]
    timeframe: 15m
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with patch("live_runner.config_parser.logger.warning") as warning_mock:
        _, deployments = parse_live_deployments_config(config_path)

    assert len(deployments) == 1
    warning_mock.assert_called_once()
    _, kwargs = warning_mock.call_args
    assert kwargs["extra"]["data"]["account_id"] == "unknown-account"


class TestEnvExpansionAndUnexpandedGate:
    r"""The `${VAR}` expansion and the gate that refuses an unexpanded reference.

    The `${...}` form is already well covered by `tests/live_runner/test_discord_ingestion_gates.py`
    - all six mutations there were killed on the first attempt. This class covers the two things
    that gate does not reach: **an explicitly blank value**, and **the bare `$VAR` form**.

    **A blank value is a statement, not an absence.** `_expand_env_value` uses
    `env.get(key, match.group(0))`, so a variable set to `""` expands to nothing while an
    *unset* variable stays literal. That distinction is the whole point, and it is the exact
    hazard this repo has already been bitten by twice - a blanked credential silently selecting
    a default, once per broker, with the default being demo mode.

    Writing it as `env.get(key) or match.group(0)` looks equivalent and is not: it turns an
    explicit blank back into the literal text `${VAR}`. The failure would then be *loud* -
    the unexpanded gate reports it - but **the cause is inverted**. The operator set the
    variable on purpose and is told it was never set. Loud-and-wrong is still wrong when the
    message sends someone to the wrong place.

    **The bare `$VAR` form is a real gap, recorded rather than fixed.** `_expand_env_value`
    expands both `${VAR}` and `$VAR`, and leaves either literal when unset. But
    `assert_no_unexpanded_env` only matches `\$\{[A-Za-z_][A-Za-z0-9_]*\}`, so an unexpanded
    **bare** reference passes the gate silently.

    That is precisely the failure the gate was written for: its docstring says a literal
    `"${DISCORD_BOT_X_CHANNEL_ID}"` "is a startup error, never a silent mute". The braced form
    gets that guarantee and the bare form does not, and both are spellings the expander accepts.

    The likely reason for the narrow pattern is a legitimate one - a config may legitimately
    contain `cost $5`, and a blanket `\$[A-Za-z_]` pattern would not touch that but *would*
    touch a literal `$USD` in a comment. So the trade-off is real, and closing it is a
    decision rather than a fix. Tracked as `needs-decision`.
    """

    ENV = {"SET": "value", "BLANKED": "", "NUMERIC": "7"}

    @pytest.mark.parametrize(
        "template,expected",
        [
            ("${SET}", "value"),
            ("$SET", "value"),
            ("prefix ${SET} suffix", "prefix value suffix"),
            ("prefix $SET suffix", "prefix value suffix"),
            ("${NUMERIC}", "7"),
            ("${SET}${SET}", "valuevalue"),
            ("$SET-$NUMERIC", "value-7"),
        ],
    )
    def test_both_reference_forms_expand_when_the_variable_is_set(
        self, template: str, expected: str
    ) -> None:
        """`${VAR}` and `$VAR` are both accepted spellings, and both are expanded.

        Asserted pairwise because they are separate alternatives in one regex, and dropping
        either changes what a config file means.
        """
        from live_runner.config_parser import _expand_env_value

        assert _expand_env_value(template, self.ENV) == expected

    def test_a_variable_set_to_empty_expands_to_nothing(self) -> None:
        """**The blanked-value contract, which is the one to protect.**

        The variable *is* set; its value is empty. Expanding to the literal `${VAR}` would say
        the opposite to the operator, and the unexpanded gate would then blame them for not
        setting something they set deliberately.
        """
        from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env

        assert _expand_env_value("${BLANKED}", self.ENV) == ""
        assert _expand_env_value("$BLANKED", self.ENV) == ""
        assert _expand_env_value("x${BLANKED}y", self.ENV) == "xy"
        # And because it expanded, there is nothing left for the gate to complain about.
        assert_no_unexpanded_env({"channel": _expand_env_value("${BLANKED}", self.ENV)})

    def test_an_unset_variable_stays_literal_so_the_gate_can_see_it(self) -> None:
        """The other half of the contract: unset is *not* the same as blank.

        Leaving the reference in place is what lets `assert_no_unexpanded_env` catch it. An
        expander that substituted `""` for an unset variable would make the gate dead code.
        """
        from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env

        assert _expand_env_value("${UNSET}", self.ENV) == "${UNSET}"
        with pytest.raises(ValueError, match="Unexpanded environment reference"):
            assert_no_unexpanded_env({"id": _expand_env_value("${UNSET}", self.ENV)})

    def test_a_bare_reference_that_IS_set_expands_today(self) -> None:
        """Pins the half of the trade-off that is not free.

        Item 4's remaining decision is whether a bare `$NAME` should keep expanding.
        Today it does, and that is the only reason narrowing expansion would be a
        behaviour change at all. Measured blast radius of narrowing is zero: none of the
        7 live deployments contains a `$` anywhere, all 4 env references in
        `live_deployments.example.yaml` are braced, and none of the 27 active assignments
        in the real `.accounts.env` contains a `$` - which matters because the guard runs
        *after* expansion (`config_parser.py:119` then `120`), so an env value is exactly
        how a `$` could appear in an already-expanded payload.

        So nothing today depends on this, and a change here would be deliberate rather than
        a rescue.
        """
        from live_runner.config_parser import _expand_env_value

        env = {"SET_VAR": "value"}
        assert _expand_env_value("$SET_VAR", env) == "value", (
            "a bare reference that IS set no longer expands, so the cost side of item 4 "
            "has changed - re-measure which configs use the bare form before narrowing it"
        )

    def test_requiring_braces_would_reject_a_literal_dollar_amount(self) -> None:
        """The measured cost of the fix, stated as a fact rather than a worry.

        Both surviving options for closing the bare-`$VAR` gap work by rejecting a bare
        `$NAME`, and that rejection is indiscriminate: it cannot tell an unresolved
        reference from a currency amount in free text. So whichever way item 4 goes, this
        is what an operator hits:

            costs $USD per month   -> rejected

        Pinned with a widened pattern defined *here*, so the cost is re-evaluated if the
        pattern is ever changed rather than being rediscovered when a config fails to
        load. This asserts nothing about production code - it documents the trade-off.
        """
        import re

        widened = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
        # Caught: a `$` immediately followed by an identifier-shaped name.
        for literal, why in (
            ("costs $USD per month", "a currency code"),
            ("pnl in $GBP", "another currency code"),
            ("benchmark $SPX", "an index ticker"),
        ):
            assert widened.findall(literal), (
                f"{literal!r} ({why}) would no longer be matched by a widened guard, so the "
                "measured cost of requiring braces has changed - update item 4"
            )
        # NOT caught, which narrows the cost considerably and is worth knowing: the pattern
        # needs a name after the `$`, so a plain amount is unaffected. Asserted because the
        # natural assumption is the opposite, and this test was written assuming that.
        for safe, why in (
            ("$5 notional", "a numeric amount"),
            ("target 100$ per trade", "a trailing currency symbol"),
            ("cost is 5 dollars", "a spelled-out amount"),
        ):
            assert not widened.findall(safe), (
                f"{safe!r} ({why}) would now be rejected by a widened guard, so its cost is "
                "broader than documented - re-measure before requiring braces"
            )
        # And the case the guard exists for is still caught, braced or bare.
        for reference in ("${DISCORD_BOT_X_CHANNEL_ID}", "$DISCORD_BOT_X_CHANNEL_ID"):
            assert widened.findall(reference), (
                f"{reference!r} would not be caught by a widened guard, which would defeat "
                "the entire point of the change"
            )

    def test_a_bare_unset_reference_passes_the_gate_KNOWN_GAP(self) -> None:
        """**A real gap, pinned as-is.** The bare form escapes the gate entirely.

        `_UNEXPANDED_ENV_PATTERN` matches only the braced form, so an unexpanded `$VAR` sails
        through `assert_no_unexpanded_env` - and it is one of the two spellings the expander
        accepts. The gate's own docstring promises the opposite: *"is a startup error, never a
        silent mute"*.

        Asserted as-is so the gap stays visible. If this test starts failing, the gap was
        closed and the backlog entry should go with it.
        """
        from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env

        left = _expand_env_value("$UNSET", self.ENV)
        assert left == "$UNSET", f"the bare form now expands differently: {left!r}"
        assert_no_unexpanded_env({"channel": left})  # no raise: this is the gap

    def test_the_gate_walks_nested_structures_and_names_what_it_found(self) -> None:
        """Nested dicts, lists and strings are all reached, and the message names the keys.

        Asserted together because a gate that only checks the top level is the natural way to
        write one - and #81's own payload is nested.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        payload = {
            "a": {"b": ["${DEEP}", {"c": "${ALSO_DEEP}"}]},
            "plain": "no references here",
        }
        with pytest.raises(ValueError) as exc:
            assert_no_unexpanded_env(payload, context="live deployment config")
        message = str(exc.value)
        assert "${DEEP}" in message and "${ALSO_DEEP}" in message, message
        assert "live deployment config" in message, message

    def test_a_clean_payload_passes_and_non_strings_are_ignored(self) -> None:
        """The pass case, so the gate is not simply always-firing.

        Non-string leaves are skipped rather than crashing: a config may legitimately contain
        numbers, booleans and nulls.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        assert_no_unexpanded_env(
            {"n": 7, "f": 1.5, "b": True, "none": None, "s": "all set", "l": [1, "two"]}
        )
        assert_no_unexpanded_env({})
        assert_no_unexpanded_env([])

    def test_prices_and_malformed_references_are_not_mistaken_for_variables(self) -> None:
        """What the narrow pattern buys, stated so the trade-off is visible.

        `cost $5` and `$100` are legitimate config text. The current pattern cannot match them
        because `$` must be followed by a letter or underscore, so currency survives - and
        that is almost certainly why the pattern is as narrow as it is.

        The malformed forms `${}`, `${1BAD}` and `${9}` are also not matched. Those are
        ambiguous - a config could legitimately contain them as text - so the gate ignores
        them, and the cost is that a genuinely misspelled `${1BAD}` also goes unnoticed.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        for text in ("cost $5", "$100 per month", "a${}b", "${1BAD}", "50% discount"):
            assert_no_unexpanded_env({"text": text})

        # Note what this test is *not*: `${SET}` in a raw template is refused, correctly.
        # The gate runs on the **already-expanded** payload, so any `${NAME}` still present
        # is unexpanded by definition - which is why an expanded template is not something
        # it ever sees. Asserted here because my first version of this test got it backwards
        # and expected a set variable to pass.
        with pytest.raises(ValueError, match=re.escape("${SET}")):
            assert_no_unexpanded_env({"text": "${SET}"})


class TestUnexpandedConfigReferencesAreRecorded:
    """A reference that never resolved must not be silent.

    `_expand_env_value` left an unresolved `$VAR` in the value and said nothing. That is the
    failure mode the guard next door exists for: a channel id of `${DISCORD_BOT_X_CHANNEL_ID}`
    matches no channel, and the notification vanishes with nothing recorded anywhere.

    **This does not close the guard's gap, and deliberately so.** `assert_no_unexpanded_env`
    still only matches the braced form, because widening it would refuse a legitimate literal
    `$USD` in free text - and, measured, an unresolved `$UNSET` and a deliberate `$USD` both
    survive as the *same string*, so nothing downstream (including a report of which keys
    failed to resolve) can tell them apart. Choosing between "silent", "refuse" and "opt-in"
    per field is a design decision and stays with the human.

    What is free of that trade-off is exactly what landed here: the expansion result is
    unchanged, and the unresolved key is **recorded** instead of vanishing.
    """

    def _expand(self, value, env, caplog):
        import logging

        from live_runner.config_parser import _expand_env_value

        with caplog.at_level(logging.WARNING, logger="trading.live-runner"):
            caplog.clear()
            return _expand_env_value(value, env)

    @pytest.mark.parametrize(
        ("payload", "env", "expected"),
        [
            ("${SET}", {"SET": "value"}, "value"),
            ("$SET", {"SET": "value"}, "value"),
            # **Unchanged behaviour.** These are the exact results from before the change.
            ("${UNSET}", {}, "${UNSET}"),
            ("$UNSET", {}, "$UNSET"),
            ("costs $USD per month", {"SET": "x"}, "costs $USD per month"),
            ("price $5", {}, "price $5"),
            ("$$USD", {}, "$$USD"),
            # A variable that is set but empty is RESOLVED, and must not be called missing.
            ("$EMPTY", {"EMPTY": ""}, ""),
            ("${EMPTY}", {"EMPTY": ""}, ""),
        ],
    )
    def test_the_expansion_result_is_byte_identical_to_before(
        self, payload, env, expected, caplog
    ) -> None:
        """The fix must not change a single expanded value.

        A refactor that "also" tightened expansion would refuse a valid config, so the values
        are pinned rather than reasoned about.
        """
        assert self._expand(payload, env, caplog) == expected

    def test_an_unresolved_reference_is_now_logged(self, caplog) -> None:
        self._expand("${DISCORD_BOT_X_CHANNEL_ID}", {}, caplog)
        messages = [r.getMessage() for r in caplog.records]
        assert any("DISCORD_BOT_X_CHANNEL_ID" in m for m in messages), (
            f"an unresolved reference is still silent: {messages}"
        )
        assert any("not present in the environment" in m for m in messages), messages

    def test_the_bare_form_is_logged_too(self, caplog) -> None:
        """The bare spelling is the one the guard misses, so it is the one that must be loud."""
        self._expand("$DISCORD_BOT_X_CHANNEL_ID", {}, caplog)
        messages = [r.getMessage() for r in caplog.records]
        assert any("DISCORD_BOT_X_CHANNEL_ID" in m for m in messages), (
            f"the bare form is silent, which is the whole gap: {messages}"
        )

    def test_a_resolved_reference_is_not_logged(self, caplog) -> None:
        self._expand("${SET}", {"SET": "value"}, caplog)
        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_a_set_but_empty_reference_is_not_reported_missing(self, caplog) -> None:
        """`key in env`, not truthiness - an empty value is a value the operator chose."""
        assert self._expand("$EMPTY", {"EMPTY": ""}, caplog) == ""
        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_one_line_per_value_not_per_occurrence(self, caplog) -> None:
        """A config repeating a reference should not produce a log line per repetition."""
        self._expand("$UNSET and $UNSET and $UNSET", {}, caplog)
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1, messages
        assert messages[0].count("UNSET") == 1, f"the key is listed once: {messages}"

    def test_a_literal_dollar_name_in_free_text_is_still_accepted(self, caplog) -> None:
        """The reason this logs rather than refuses.

        A literal `$USD` in a note is indistinguishable from an unset `$USD` variable after
        expansion, so refusing either would break a valid config. It must expand unchanged -
        and being *told* there is a `$USD` in there is the useful part.
        """
        assert self._expand("costs $USD per month", {}, caplog) == "costs $USD per month"
        messages = [r.getMessage() for r in caplog.records]
        assert any("USD" in m for m in messages), (
            f"a literal $USD should still be pointed out, just not refused: {messages}"
        )

    def test_the_guard_gap_is_still_open_and_still_pinned(self) -> None:
        """The human's decision is untouched, and the tripwire still trips on a fix."""
        text = Path(__file__).read_text(encoding="utf-8")
        assert "test_a_bare_unset_reference_passes_the_gate_KNOWN_GAP" in text, (
            "the tripwire that fails when the gap is closed has been removed. Either the gap "
            "is closed - in which case the decision was taken and should be recorded - or the "
            "tripwire is gone and nobody will notice the gap closing."
        )
