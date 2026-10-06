"""An Alpaca execution price that is not a usable number must be visible before it is stored.

`_to_float` coerces an absent or unparseable price to ``0.0`` - the same value a genuine zero
produces - so a row carrying ``actual_price=0`` arrives with nothing on the wire and nothing in the
log to say whether the broker spoke at all. The cTrader resolver has always logged that case; both
Alpaca price paths were silent.

Item 10 asks what should happen to such a row - refuse it, flag it, or store the signal's intended
price - and that answer needs the broker's behaviour, which is not knowable from here. These tests
pin only the visibility, and pin the stored value as unchanged so nobody "fixes" the decision by
editing behaviour here.
"""

from __future__ import annotations

import logging
import math

import pytest

from live_runner.alpaca_adapter import AlpacaLiveAdapter


@pytest.fixture
def warn():
    return AlpacaLiveAdapter._warn_unreadable_price


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_a_readable_price_is_silent(warn, caplog: pytest.LogCaptureFixture) -> None:
    """The cry-wolf direction. A warning on every ordinary fill is one nobody reads."""
    with caplog.at_level(logging.WARNING, logger="live-runner"):
        warn(261.7, raw_price=261.7, account_id="a", broker_order_id="o", source="trade_update")
        warn(0.01, raw_price="0.01", account_id="a", broker_order_id="o", source="trade_update")

    assert _messages(caplog) == []


def test_an_absent_price_warns_and_says_absent(warn, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="live-runner"):
        warn(0.0, raw_price=None, account_id="a", broker_order_id="o", source="trade_update")

    warnings = _messages(caplog)
    assert len(warnings) == 1
    assert "absent" in warnings[0]
    assert "actual_price=0" in warnings[0]
    # Must say what this leads to, and that the decision is still open.
    assert "item 10" in warnings[0]


def test_an_unparseable_price_names_the_offending_value(
    warn, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="live-runner"):
        warn(0.0, raw_price="not-a-number", account_id="a", broker_order_id="o", source="order_reconcile")

    warnings = _messages(caplog)
    assert len(warnings) == 1
    assert "'not-a-number'" in warnings[0], "the log must carry the value that could not be read"
    assert "order_reconcile" in warnings[0], "the log must say which path produced it"


@pytest.mark.parametrize("resolved", [0.0, -1.5, float("nan"), float("inf")])
def test_every_unusable_price_warns(
    warn, resolved: float, caplog: pytest.LogCaptureFixture
) -> None:
    """0.0, negative, NaN and inf all reach the same unreadable branch.

    NaN is the nastiest: it is truthy, so an `if value:` guard would let it through, and one NaN row
    makes `avg(actual_price)` NaN and makes `WHERE actual_price > x` skip that row silently.
    """
    with caplog.at_level(logging.WARNING, logger="live-runner"):
        warn(resolved, raw_price=resolved, account_id="a", broker_order_id="o", source="trade_update")

    assert len(_messages(caplog)) == 1


def test_nan_is_caught_even_though_it_is_truthy(warn, caplog: pytest.LogCaptureFixture) -> None:
    """Pins why this helper exists rather than a plain `if not resolved:` check."""
    assert math.isnan(float("nan")) and bool(float("nan")) is True

    with caplog.at_level(logging.WARNING, logger="live-runner"):
        warn(float("nan"), raw_price=float("nan"), account_id="a", broker_order_id="o", source="s")

    assert len(_messages(caplog)) == 1


def test_both_alpaca_price_paths_are_wired(warn) -> None:
    """A helper nobody calls cannot prevent anything."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "live_runner" / "alpaca_adapter.py").read_text(
        encoding="utf-8"
    )
    calls = source.count("_warn_unreadable_price(")
    # One definition plus two call sites.
    assert calls == 3
    assert "actual_price=resolved_price" in source, (
        "the reconcile path must store the value it warned about, not a separate lookup"
    )
    assert 'source="trade_update"' in source
    assert 'source="order_reconcile"' in source


def test_the_stored_value_is_unchanged_by_the_warning(warn) -> None:
    """Item 10's decision is not made here. The helper must not alter what it inspects."""
    for raw, expected in ((None, 0.0), ("261.7", 261.7), (261.7, 261.7), ("bad", 0.0)):
        resolved = 0.0 if raw in (None, "bad") else float(raw)
        warn(resolved, raw_price=raw, account_id="a", broker_order_id="o", source="s")
        assert resolved == expected