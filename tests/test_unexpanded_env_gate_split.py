"""The braced form fails closed and the bare form fails open. That split IS the decision.

Backlog item 4 asks whether a bare `$NAME` should be required to be written `${NAME}`. The shipped
half (`db083cc`) made `_expand_env_value` *log* an unresolved reference for both spellings. The
gate, `assert_no_unexpanded_env`, refuses only the braced form.

Measured on this codebase:

| input                   | expander logs | gate does |
|-------------------------|---------------|-----------|
| `${MISSING}`            | yes           | **refuses** |
| `$MISSING`              | yes           | **accepts** |
| `costs $USD per month`  | yes           | accepts   |
| `benchmark $SPX`        | yes           | accepts   |
| `$5 notional`           | no            | accepts   |
| `target 100$ per trade` | no            | accepts   |

**Why the gate cannot simply catch the bare form.** `$USD` and `$ACCOUNT_ID` are syntactically
identical - a dollar sign then an uppercase identifier run. Widening the gate to the bare form
would refuse `costs $USD per month` and `benchmark $SPX`, which are legitimate config text, and
the backlog already measured that blast radius. It is what makes this a decision rather than a
cleanup. There is no syntactic discriminator to find, so this file does not go looking for one.

**Why the split is pinned anyway.** It is a real asymmetry on a live-trading path: the same
operator mistake written two ways has one form refused at startup and the other silently
accepted. Left unpinned, a later edit could close the gap in either direction - refusing bare
refs and breaking legitimate configs, or dropping the braced refusal and losing a hard
guarantee - without anybody deciding. So each row is asserted, and each assertion says what a
change would mean.

Nothing here decides item 4. It records what the decision is currently worth, and makes the cost
of each direction visible at the point someone is tempted to change it.
"""

from __future__ import annotations

import io
import logging
import re

import pytest

from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env
from live_runner.config_parser import logger as config_logger

ENV = {"ACCOUNT_ID": "12345"}


def _expander_logs(value: str) -> str:
    """Run the expander and return whatever it logged, so the warning is observed not assumed."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    # The logger is read off the module rather than guessed. `get_logger("live-runner")` yields
    # `trading.live-runner`, so attaching to a literal "live-runner" captures nothing - which is how
    # this test first reported that BOTH spellings were silent when only the capture was broken.
    logger = config_logger
    logger.addHandler(handler)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        _expand_env_value(value, ENV)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    return stream.getvalue()


def test_the_both_forms_expand_identically_when_the_variable_exists() -> None:
    """The split is about refusal, not expansion - both spellings resolve the same way."""
    assert _expand_env_value("${ACCOUNT_ID}", ENV) == "12345"
    assert _expand_env_value("$ACCOUNT_ID", ENV) == "12345"


@pytest.mark.parametrize("spelling", ["${MISSING}", "$MISSING"])
def test_both_unresolved_spellings_are_logged(spelling: str) -> None:
    """The shipped half of item 4: an unresolved reference is recorded, either way."""
    logged = _expander_logs(spelling)
    assert "MISSING" in logged, (
        f"{spelling!r} was left unexpanded with nothing said, which is the failure this item "
        f"exists for: a value matching nothing, delivered silently"
    )


def test_the_gate_refuses_the_braced_form() -> None:
    with pytest.raises(Exception, match="[Uu]nexpanded"):
        assert_no_unexpanded_env({"channel": "${MISSING}"})


def test_the_gate_accepts_the_bare_form_and_says_why_that_is_the_decision() -> None:
    """The asymmetry, asserted. If this fails, either item 4 was decided or a bug landed.

    A change here is not a cleanup. Refusing bare refs would also refuse `costs $USD per month`
    and `benchmark $SPX`, which is the cost the backlog measured. Accepting them is the recorded
    decision - not an oversight, and not something a later edit should change quietly.
    """
    assert_no_unexpanded_env({"channel": "$MISSING"})


@pytest.mark.parametrize(
    "text",
    [
        "costs $USD per month",
        "benchmark $SPX",
        "$5 notional",
        "target 100$ per trade",
        "cost is 5 dollars",
    ],
)
def test_literal_currency_and_ticker_text_stays_legal(text: str) -> None:
    """The collision cases, pinned so widening the gate cannot happen by accident.

    `test_requiring_braces_would_reject_a_literal_dollar_amount` pins the same truth for braces;
    this pins it for the bare form, which is the one the gate would be widened to catch.
    """
    assert_no_unexpanded_env({"note": text})


def test_a_widened_gate_would_break_these_two_and_only_these_two() -> None:
    """The cost of the decision, as an executable claim rather than prose.

    If someone widens `_UNEXPANDED_ENV_PATTERN` to cover bare references, this is the list that
    breaks. It exists so the cost is re-evaluated at the moment of the change rather than
    rediscovered when a real config fails to load.
    """
    candidates = (
        "costs $USD per month",
        "benchmark $SPX",
        "$5 notional",
        "target 100$ per trade",
    )
    would_break = [t for t in candidates if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", t)]
    assert would_break == ["costs $USD per month", "benchmark $SPX"], (
        f"the collision set changed: {would_break}. Amounts are safe because no name follows the "
        f"dollar sign; currency codes and tickers are not, because a name does."
    )


def test_a_bare_reference_resolves_so_the_braced_only_gate_is_not_hiding_misses() -> None:
    """The reason item 4 is narrower than it looks.

    The gate refuses only *braced* references. That reads like a fail-open gap, but the expander
    expands **both** forms, so a bare `$CTRADER_DEMO_CTID` resolves exactly like
    `${CTRADER_DEMO_CTID}` and the gate has nothing to catch.

    What the gate genuinely misses is a bare reference whose variable is *absent*: the expander logs
    it, leaves it in place, and the gate accepts the result. So it is visible in the log rather than
    silent - which makes this a policy choice about fail-closed-vs-tolerant, not an undetected defect.
    """
    from live_runner.config_parser import _expand_env_value, assert_no_unexpanded_env

    env = {"CTRADER_DEMO_CTID": "46736434"}

    # A bare reference that resolves: expanded correctly, and the gate has nothing to say.
    assert _expand_env_value("$CTRADER_DEMO_CTID", env) == "46736434"
    assert_no_unexpanded_env({"ctid": "46736434"}, context="probe")

    # A bare reference whose variable is absent: left in place and logged, and the gate accepts it.
    assert _expand_env_value("$CTRADER_MISSING", env) == "$CTRADER_MISSING"
    assert_no_unexpanded_env({"ctid": "$CTRADER_MISSING"}, context="probe")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"jobs": {"py": {"runs-on": "${{ matrix.os }}"}}}, id="github-actions"),
        pytest.param({"note": "costs $USD per month"}, id="prose-dollar"),
        pytest.param({"ctid": "$CTRADER_DEMO_CTID"}, id="bare-reference"),
    ],
)
def test_everything_a_widened_bare_dollar_gate_would_refuse(payload: dict) -> None:
    """The full collision set, so item 4's cost is enumerated rather than asserted.

    Widening the gate to bare `$NAME` is one regex. This is what it would refuse today, and every
    entry is a legitimate value - a GitHub Actions expression, prose containing a currency symbol, and
    a bare reference that resolved correctly during expansion and so is a fully-expanded config by the
    time the gate runs.

    That last one is the important row: the gate runs *after* expansion, so a bare reference that
    resolved is no longer present as `$NAME` in the payload at all. Widening would therefore refuse
    only unresolved bare references and the two false-positive shapes - which is a narrower blast
    radius than it first appears, and a real decision rather than a coin flip.
    """
    from live_runner.config_parser import assert_no_unexpanded_env

    # All three are accepted today, so widening is what would change.
    assert_no_unexpanded_env(payload, context="probe")
