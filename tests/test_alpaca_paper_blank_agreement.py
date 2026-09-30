"""A blanked `ALPACA_PAPER` must be refused, not silently turned into the default.

The mirror of the `CTRADER_DEMO` bug, in the other broker's adapter and found by
the same method: list the env-var names read in two or more places, then classify
each read by whether it preserves a present-but-empty value or collapses it into
"absent".

`live_runner/credential_env.resolve_env_value` exists to keep those two apart -
its docstring says a key defined in the accounts file wins "even when its value is
empty (present-but-empty stays empty)" - and it returns `""` for both an absent key
and a blanked one. The constructor then wrote `resolve_env_value(...) or None`,
which turns the blank into `None`, hands the strict parser nothing, and applies
`default=True`.

The divergence is visible *inside this file*. `_default_alpaca_credentials`, twenty
lines up, gets it right:

    raw_paper = values.get("ALPACA_PAPER")
    paper = _parse_bool_env(
        "ALPACA_PAPER",
        str(raw_paper).strip() if raw_paper is not None else None,
        default=True,
    )

It tests for `None` rather than falsiness, so a blanked value reaches the parser
and is refused. The constructor does not. `account_discovery` calls the correct one
in two places and the adapter's constructor is called in others, so the same blanked
file yields a refusal on one path and a silent paper account on another.

As with cTrader, the current consequence points the safe way - a blank becomes
paper, not live - which is exactly why it survived. `default=True` is the only thing
between this and a blanked variable selecting a live account, and that is not a
margin anyone should be relying on.
"""

from __future__ import annotations

import pytest

from live_runner.alpaca_adapter import AlpacaLiveAdapter, _default_alpaca_credentials


def _adapter(env_values, paper=None):
    return AlpacaLiveAdapter(
        api_key="key", secret_key="secret", paper=paper, env_values=env_values
    )


def test_a_blanked_alpaca_paper_value_is_refused_by_the_constructor(monkeypatch) -> None:
    """Blank means someone blanked it; it is not the same as never having set it."""
    monkeypatch.delenv("ALPACA_PAPER", raising=False)

    with pytest.raises(ValueError) as excinfo:
        _adapter({"ALPACA_PAPER": ""})
    assert "ALPACA_PAPER" in str(excinfo.value), (
        f"the refusal did not name the variable: {excinfo.value}"
    )


def test_the_two_readers_of_alpaca_paper_agree(monkeypatch) -> None:
    """The divergence was inside one file, which makes it a bug rather than a style question.

    `_default_alpaca_credentials` already preserved the blanked/absent distinction.
    Pinning both means a future "simplification" of either one to `or None` fails
    here instead of splitting the platform's opinion on paper again.

    Outcomes are compared rather than values, because a refusal is a legitimate
    answer from either reader and "one raises where the other answers" is exactly
    the divergence worth catching.
    """
    monkeypatch.delenv("ALPACA_PAPER", raising=False)

    def outcome(fn):
        try:
            return ("ok", fn())
        except ValueError:
            return ("refused", None)

    for value in ("", "  ", "true", "false", "0", "1", "yes", "off", "TRUE", " off "):
        env_values = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": value}
        via_constructor = outcome(lambda: _adapter({"ALPACA_PAPER": value}).paper)
        via_resolver = outcome(lambda: _default_alpaca_credentials(env_values)[2])
        assert via_constructor == via_resolver, (
            f"ALPACA_PAPER={value!r}: constructor says {via_constructor} and "
            f"_default_alpaca_credentials says {via_resolver}"
        )
        if value.strip() == "":
            assert via_constructor[0] == "refused", (
                f"ALPACA_PAPER={value!r} is a blanked value and must be refused, not "
                f"answered with a default: {via_constructor}"
            )


def test_an_absent_alpaca_paper_still_defaults_to_paper(monkeypatch) -> None:
    """Only a *present* blank is an error; the common case must keep working."""
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    assert _adapter({}).paper is True


def test_an_explicit_paper_argument_still_wins(monkeypatch) -> None:
    """A caller that passed `paper=` has decided, blanks included."""
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    assert _adapter({"ALPACA_PAPER": ""}, paper=False).paper is False


def test_a_valid_paper_value_is_honoured_in_both_directions(monkeypatch) -> None:
    """The refusal must not have swallowed the ordinary values."""
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    for value, expected in (("true", True), ("1", True), ("yes", True),
                            ("false", False), ("0", False), ("off", False)):
        assert _adapter({"ALPACA_PAPER": value}).paper is expected, f"{value!r}"
