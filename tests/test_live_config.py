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

    def test_a_resolvable_only_gate_rejects_the_corrupting_cases_and_nothing_else(self) -> None:
        """A third option the backlog's binary framing never allowed.

        The entry weighed *accept bare `$VAR`* against *refuse bare `$VAR`*, and recorded that
        refusing "cannot tell an unresolved reference from a currency amount in free text". Measured
        against the real expander on 2026-10-11, that is true only of an **indiscriminate** gate - one
        that rejects every identifier-shaped `$`. Resolution changes the answer:

            costs $USD per month          USD is set    -> caught (currently expands to "dollars")
            leak $ALPACA_API_KEY_ACC1     set          -> caught (credential in prose)
            pnl in $GBP                   GBP not set  -> NOT caught, correctly a literal
            $5 notional                                 -> NOT caught
            target 100$ per trade                       -> NOT caught

        Against production this rejects **0 of 21** config values, so it is behaviour-neutral today
        while closing the silent-corruption path - which the indiscriminate version cannot claim
        without also rejecting `costs $USD per month`.

        The residual ambiguity, stated rather than hidden: a bare `$NAME` that resolves is still
        ambiguous between a deliberate reference and a currency code colliding with a variable name.
        `USD` is not in `.accounts.env` today, so `costs $USD` is harmless *now*; adding `USD=...`
        would make this gate reject a currently-harmless config. That is a narrower and more
        explicable failure than a credential silently landing in prose.

        This asserts the option exists and what it catches, not that production code does it - the
        decision remains the operator's.
        """
        import re

        from live_runner.config_parser import _expand_env_value

        env = {"USD": "dollars", "ALPACA_API_KEY_ACC1": "SECRET"}
        bare = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

        def caught(value: str) -> bool:
            return any(key in env for key in bare.findall(value))

        for corrupting, why in (
            ("costs $USD per month", "a set variable inside prose silently becomes its value"),
            ("leak $ALPACA_API_KEY_ACC1 here", "a credential lands in prose with no error"),
        ):
            assert caught(corrupting), f"{why} - the gate must catch {corrupting!r}"
            # And the corruption being prevented is real, not hypothetical.
            assert _expand_env_value(corrupting, env) != corrupting, (
                f"{corrupting!r} no longer expands, so the hazard it represents is gone and this "
                f"test's premise has changed"
            )

        for safe, why in (
            ("pnl in $GBP", "an unset ticker is text, not a reference"),
            ("$5 notional", "a numeric amount"),
            ("target 100$ per trade", "a trailing currency symbol"),
            ("100$", "a bare amount"),
            ("fee $ 5", "a spaced amount"),
        ):
            assert not caught(safe), (
                f"{why}: a resolvable-only gate must NOT reject {safe!r}, or it has become the "
                f"indiscriminate gate this option exists to avoid"
            )
            assert _expand_env_value(safe, env) == safe, (
                f"{safe!r} changed under expansion; re-measure before trusting the gate's premise"
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


class TestAnUnclosedReferenceIsATypo:
    r"""An unclosed `${` has no reading as text, which is what makes this decidable.

    `assert_no_unexpanded_env` matches `\$\{[A-Za-z_][A-Za-z0-9_]*\}`, which insists on the
    closing brace. So `${FOO` matched nothing, survived expansion, passed the guard, and delivered
    its literal to a Discord `channel_id` — measured end to end. That is the silent mute #81
    exists to prevent, reached by a route the existing pattern cannot see.

    **The scope is deliberately one case.** `${}`, `${1BAD}` and `${9}` are also malformed, but
    they were recorded as ambiguous — a config could plausibly contain them as text — and that
    decision is left exactly as it was. An unclosed brace has no such reading.

    **This is not the bare-form question.** Bare `$NAME` stays open (backlog item 4) because
    `$USD` and `$SPX` collide with real currency codes and tickers. Unclosed bracing has no such
    collision, which is why this could land without reopening it.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "${DISCORD_X",       # the form measured end to end
            "${FOO",
            "${FOO and more",    # unclosed, mid-sentence
            "prefix ${DISCORD_BOT_X_CHANNEL_ID",
            "${a.b}",            # brace present, name dotted - malformed name, same guard
        ],
    )
    def test_an_unclosed_reference_is_rejected(self, text) -> None:
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError, match="Malformed environment reference"):
            assert_no_unexpanded_env({"channel_id": text})

    def test_it_quotes_the_offending_token_rather_than_a_bare_brace(self) -> None:
        """`${DISCORD_X` and `${FOO` are different typos; both reporting as `${` helps nobody."""
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env({"channel_id": "${DISCORD_X"})
        assert "${DISCORD_X" in str(excinfo.value)

    def test_the_message_asks_for_the_brace_not_for_the_variable(self) -> None:
        """The operator's fix differs between the two failures, so the message must differ.

        A well-formed `${FOO}` means *set the variable*. An unclosed `${FOO` means *you missed a
        brace* — and nothing was expanded at all. Sending someone to the environment when the
        answer is a missing character in the file in front of them wastes the one signal they get.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as unclosed:
            assert_no_unexpanded_env({"channel_id": "${FOO"})
        with pytest.raises(ValueError) as unexpanded:
            assert_no_unexpanded_env({"channel_id": "${FOO}"})

        assert "closing brace is missing" in str(unclosed.value)
        assert "Malformed" not in str(unexpanded.value), (
            "a well-formed unset reference is being reported as unclosed; the two failures have "
            "different fixes and must not be conflated"
        )
        assert "set them via environment" in str(unexpanded.value)

    @pytest.mark.parametrize(
        "text",
        ["${}", "${1BAD}", "${9}", "${ VAR}", "${VAR-NAME}"],
    )
    def test_the_recorded_ambiguous_forms_are_still_left_alone(self, text) -> None:
        """The prior decision stands, and this pins it from the new pattern's side.

        A guard widened for one typo must not quietly take the neighbouring cases with it. If one
        of these ever starts being rejected, that is a decision to make on purpose.

        Note that all five are *closed*. `${a.b}` is not in this list even though a dot is not a
        legal identifier character, because its brace **is** present - it is caught by the same
        guard, for the other of the guard's two reasons. That distinction was previously stated
        backwards here: an earlier version of this comment said `${a.b}` "has no closing brace
        either", which was false, and it is why the error message once claimed a missing brace for
        a reference that had one.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        assert_no_unexpanded_env({"text": text})

    @pytest.mark.parametrize(
        "text", ["$USD", "$SPX", "$5", "100$", "costs $USD per month", "pnl in $GBP", "$", "SPY"]
    )
    def test_a_literal_dollar_amount_is_still_accepted(self, text) -> None:
        """Zero false positives is why this is safe to land.

        Measured on this system: 0 of the 7 live deployments contain a `$` in any column, all 4
        references in the operator-facing template are well-formed and braced, and 0 of the 27
        active `.accounts.env` values contain one.
        """
        from live_runner.config_parser import assert_no_unexpanded_env

        assert_no_unexpanded_env({"note": text})

    def test_the_bare_form_is_still_untouched(self) -> None:
        """Item 4's open one-bit question survives this untouched."""
        from live_runner.config_parser import assert_no_unexpanded_env

        assert_no_unexpanded_env({"channel_id": "$DISCORD_X"})

    def test_a_well_formed_reference_that_expanded_leaves_nothing_behind(self) -> None:
        """The guard runs *after* expansion, so a resolved reference must not trip it."""
        from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env

        assert_no_unexpanded_env(
            {"channel_id": _expand_env_value("${SET_VAR}", {"SET_VAR": "123"})}
        )

    def test_the_malformed_case_is_found_at_depth(self) -> None:
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError, match="Malformed environment reference"):
            assert_no_unexpanded_env({"deployments": [{"signal_config": {"ids": ["${A"]}}]})

    def test_several_unclosed_references_are_all_reported(self) -> None:
        """An operator fixing a config should see every one of them at once."""
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env({"a": "${ONE", "b": ["${TWO", "${THREE"]})
        message = str(excinfo.value)
        for name in ("${ONE", "${TWO", "${THREE"):
            assert name in message, f"{name} missing from {message!r}"


class TestFlagResolutionIsFileFirst:
    """A runtime flag read from a helper that has no env mapping must still see `.accounts.env`.

    `resolve_env_value` takes an already-loaded mapping, which is right for credentials: the caller
    has it, because it owns the connection. It is wrong for a flag read from a helper that has no
    such mapping - and `CTRADER_HONOUR_ZERO_MONEY_DIGITS`, the switch implementing backlog item 6,
    was exactly that. Every other live flag, including `CTRADER_DEMO` read a few lines away in the
    same constructor, is file-first.

    So the operator who put the flag in `.accounts.env` beside `CTRADER_DEMO`, as the runbook tells
    them to, got the default with no error and no warning.
    """

    @staticmethod
    def _accounts_env(tmp_path, body: str):
        path = tmp_path / ".accounts.env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_the_default_path_is_the_repo_accounts_env_file(self, tmp_path, monkeypatch) -> None:
        """No-arg lookups must read `<repo>/.accounts.env`, not fall back to the process env.

        Every other test in this class passes `accounts_env=` explicitly, so the default path had
        **no coverage at all**. That is not academic: `_honour_zero_money_digits` in
        `account_discovery` and `ctrader_adapter` calls `resolve_flag_env` with no path argument,
        and a docstring beside both claimed they consulted "only the process environment". They did
        not - `accounts_env=None` means *use the repo's file* - so the claim was false, the backlog
        item it justified did not exist, and an audit "fixed" a bug by adding an argument whose
        default already did what the fix claimed to add. Pin the default so the next reader is not
        misled by prose again.
        """
        from live_runner import credential_env

        fake_repo = tmp_path / "repo"
        (fake_repo / "live_runner").mkdir(parents=True)
        (fake_repo / ".accounts.env").write_text("CTRADER_HONOUR_ZERO_MONEY_DIGITS=true\n", encoding="utf-8")
        monkeypatch.setattr(credential_env, "__file__", str(fake_repo / "live_runner" / "credential_env.py"))
        monkeypatch.delenv("CTRADER_HONOUR_ZERO_MONEY_DIGITS", raising=False)
        monkeypatch.setenv("CTRADER_HONOUR_ZERO_MONEY_DIGITS", "false")
        credential_env.reset_flag_env_cache()
        try:
            assert credential_env.resolve_flag_env("CTRADER_HONOUR_ZERO_MONEY_DIGITS") == "true", (
                "with no accounts_env argument the repo's .accounts.env must win over the process "
                "environment - that is what makes the flag file-first for both call sites that omit it"
            )
        finally:
            credential_env.reset_flag_env_cache()

    def test_a_value_in_the_file_is_found(self, tmp_path, monkeypatch) -> None:
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        monkeypatch.delenv("CTRADER_DEMO", raising=False)
        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "CTRADER_DEMO=\n")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == "", (
            "a key defined in the accounts file must be found there"
        )
        reset_flag_env_cache()

    def test_the_file_wins_over_the_process_environment(self, tmp_path, monkeypatch) -> None:
        """File-first is the documented precedence, and the whole reason this helper exists."""
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        monkeypatch.setenv("CTRADER_DEMO", "from-environment")
        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "CTRADER_DEMO=from-file\n")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == "from-file", (
            "the accounts file must win over the process environment, as resolve_env_value's own "
            "docstring specifies"
        )
        reset_flag_env_cache()

    def test_a_key_defined_nowhere_is_none_and_not_an_empty_string(self, tmp_path) -> None:
        """`None` versus `""` is the whole point for a boolean.

        `parse_bool_env(name, None, default=...)` treats absent as the default and
        present-but-blank as an error. Collapsing the two here would lose that distinction, and the
        flag would silently ignore a blanked value.
        """
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) is None, (
            "an undefined key must be None, not an empty string; the two mean different things to "
            "parse_bool_env"
        )
        reset_flag_env_cache()

    def test_a_present_but_blank_value_stays_blank(self, tmp_path) -> None:
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "CTRADER_DEMO=\n")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == ""
        reset_flag_env_cache()

    def test_a_missing_file_is_not_an_error(self, tmp_path) -> None:
        """No `.accounts.env` is the normal single-machine setup, not a misconfiguration."""
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        reset_flag_env_cache()
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=tmp_path / "absent.env") is None
        reset_flag_env_cache()

    def test_the_environment_is_still_consulted_when_the_file_is_silent(
        self, tmp_path, monkeypatch
    ) -> None:
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        monkeypatch.setenv("CTRADER_DEMO", "from-environment")
        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == "from-environment"
        reset_flag_env_cache()

    def test_editing_the_file_is_picked_up(self, tmp_path) -> None:
        """The mtime cache must not make a flag change invisible - that is the bug it could cause.

        An operator who edits `.accounts.env` to turn a flag on expects it to take effect, exactly
        as they would for a credential. If the cache were keyed on anything but the file's mtime,
        this would keep serving the old value for the life of the process.
        """
        from live_runner.credential_env import reset_flag_env_cache, resolve_flag_env

        reset_flag_env_cache()
        path = self._accounts_env(tmp_path, "CTRADER_DEMO=before\n")
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == "before"
        # Distinct mtime, as a real edit would produce.
        path.write_text("CTRADER_DEMO=after\n", encoding="utf-8")
        import os

        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert resolve_flag_env("CTRADER_DEMO", accounts_env=path) == "after", (
            "an edit to the accounts file did not change the resolved value; the mtime cache "
            "is not keyed correctly"
        )
        reset_flag_env_cache()

    def test_the_flag_helper_now_follows_the_file(self, tmp_path, monkeypatch) -> None:
        """End to end: the item 6 flag, read through the file an operator would actually use."""
        from live_runner.account_discovery import _honour_zero_money_digits
        from live_runner.credential_env import reset_flag_env_cache

        monkeypatch.delenv("CTRADER_HONOUR_ZERO_MONEY_DIGITS", raising=False)
        reset_flag_env_cache()
        assert _honour_zero_money_digits() is False, "the default must still be off"
        reset_flag_env_cache()


class TestTheRefusalSaysWhichFieldIsWrong:
    """Backlog item 4 deferred widening this guard on a stated ground that is decidable.

    The note said the widened guard should land only after the logging had been observed, because
    "the error message names the reference but not the field it came from". That part needed no
    judgement: an operator holding a seven-deployment YAML cannot act on a token alone, because the
    same variable can appear in several places and only one of them is wrong.

    So the field path is now reported, and the reason for deferring is gone rather than pending.
    """

    @staticmethod
    def _payload(reference: str):
        return {
            "deployments": [
                {
                    "strategy_name": "fx_breakout",
                    "signal_config": {"channel_id": reference, "wait_candle_close": True},
                },
                {"strategy_name": "us_equities", "signal_config": {"channel_id": "${A_REAL_ONE}"}},
            ],
        }

    def test_the_field_path_is_named_for_a_malformed_reference(self) -> None:
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env(self._payload("${DISCORD_FX"))
        message = str(excinfo.value)
        assert "deployments[0].signal_config.channel_id" in message, (
            f"the error does not say which field is wrong: {message}"
        )

    def test_the_field_path_is_named_for_an_unexpanded_reference(self) -> None:
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env(self._payload("${DISCORD_FX}"))
        message = str(excinfo.value)
        assert "deployments[0].signal_config.channel_id" in message, (
            f"the unexpanded-reference error does not say which field is wrong: {message}"
        )

    def test_the_index_distinguishes_two_occurrences_in_different_deployments(self) -> None:
        """The point of a path: two deployments can both be wrong, differently."""
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env({
                "deployments": [
                    {"signal_config": {"channel_id": "${FIRST"}},
                    {"signal_config": {"channel_id": "${SECOND"}},
                ],
            })
        message = str(excinfo.value)
        assert "deployments[0].signal_config.channel_id" in message
        assert "deployments[1].signal_config.channel_id" in message, (
            "both offending fields must be named, not just the first: " f"{message}"
        )

    def test_a_top_level_string_reports_the_root(self) -> None:
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError, match="<root>"):
            assert_no_unexpanded_env("${BROKEN")

    def test_a_repeated_reference_is_reported_once_per_path(self) -> None:
        """Same token, same field, twice - one problem, not two."""
        from live_runner.config_parser import assert_no_unexpanded_env

        with pytest.raises(ValueError) as excinfo:
            assert_no_unexpanded_env({"a": "${SAME", "b": "${SAME"})
        message = str(excinfo.value)
        assert message.count("'${SAME'") == 2, (
            f"expected one mention per field path, got: {message}"
        )


def test_a_reference_repeated_inside_one_value_is_reported_once() -> None:
    """De-duplication is only observable when the same token appears twice in one value.

    Two different fields each holding `${SAME` are two findings and are both reported either way, so
    a test written that way cannot tell `set(found)` from `found`. One *value* containing the same
    reference twice is the case that can: the operator still has one thing to fix, in one field, and
    the message should not read as though there were two problems.
    """
    from live_runner.config_parser import assert_no_unexpanded_env

    with pytest.raises(ValueError) as excinfo:
        assert_no_unexpanded_env({"channel_id": "${A_REF and again ${A_REF"})
    message = str(excinfo.value)
    assert message.count("'${A_REF'") == 1, (
        f"the same reference twice in one value was reported {message.count(chr(39) + '${A_REF' + chr(39))} "
        f"times: {message}"
    )
    assert "channel_id" in message


def test_the_bare_gate_is_wired_before_expansion_so_it_can_still_see_the_evidence(tmp_path: Path) -> None:
    """The option is now production code, and the ordering is what makes it work.

    The test above specifies the gate's behaviour against a locally-defined `caught()`. This
    asserts that `parse_live_deployments_config` actually applies it, and specifically that it
    applies it *before* `expand_env_mapping` rather than after.

    That ordering is the whole difference between a gate that works and one that silently
    passes on every input it exists to catch. `_expand_env_value` substitutes a bare `$NAME`
    whenever the name resolves, so by the time expansion has run there is no `$USD` left to
    find - a post-expansion check would see clean text and pass, while the config on disk had
    already said `costs dollars per month`.

    So both directions are pinned: the corrupting config must be refused, and the refusal must
    quote the original token rather than the expanded value. If someone moves the call below
    the expansion, the second assertion fails rather than the gate quietly going blind.
    """
    import textwrap

    from live_runner.config_parser import parse_live_deployments_config

    env = {"USD": "dollars", "ALPACA_API_KEY_ACC1": "SECRET"}
    tmp = tmp_path / "live.yaml"
    tmp.write_text(
        textwrap.dedent(
            """
            accounts: []
            deployments:
              - name: leaky
                strategy: trend_following
                note: costs $USD per month
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as caught_ctx:
        parse_live_deployments_config(tmp, env_values=env)
    message = str(caught_ctx.value)

    # Quotes the token as written in the file...
    assert "$USD" in message
    # ...and the field path, so an operator holding a multi-deployment YAML can find it.
    assert "note" in message
    # And never quotes the expanded value: seeing "dollars" here would mean the gate ran
    # after expansion and only got lucky.
    assert "dollars" not in message

def test_the_bare_gate_admits_the_literals_the_indiscriminate_gate_would_reject(tmp_path: Path) -> None:
    """A config of ordinary free text must still load.

    The gate refuses a bare `$NAME` only when that name resolves. Free text that merely looks
    like a reference - an unset ticker, a price, a trailing currency symbol - has to survive,
    or the gate has become the indiscriminate version the backlog rejected. Pinned against
    production code rather than a local helper, so a widened pattern is caught here.
    """
    import textwrap

    from live_runner.config_parser import parse_live_deployments_config

    tmp = tmp_path / "live.yaml"
    tmp.write_text(
        textwrap.dedent(
            """
            accounts: []
            deployments:
              - strategy_name: trend_following
                account_id: "1"
                symbols: ["XAUUSD"]
                timeframe: "1h"
                note: pnl in $GBP, notional $5, target 100$ per trade
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    _accounts, deployments = parse_live_deployments_config(
        tmp,
        env_values={"USD": "dollars"},
    )
    assert len(deployments) == 1
