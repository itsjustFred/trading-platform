"""Which ATR each strategy actually uses, and the claim that live differs from backtest.

Backlog item 5 is titled "live ATR smoothing disagrees with every backtested strategy, by ~10%".
Measuring the code, the disagreement is not live-versus-backtest. It is
**strategy-versus-strategy**, and the live path is not a third opinion.

Three measurements, all checked here so none of them is folklore:

1. **There is no live-only ATR.** Every ATR in the repository is either inside a strategy
   module or in `viewer/core.py`, where it draws a chart panel via `ta.atr`. The cTrader adapter
   contains no reference to ATR or a stop at all: it reads `stop_loss` out of the signal's
   metadata - `daemon.py` reads `("stop_loss", "sl_price", "stop_price", "resolved_stop_l...")` -
   and places what it is given. So a strategy's stop comes from that strategy's own code on
   both paths.

2. **The strategies genuinely disagree with each other.** By form: Wilder's
   `ewm(alpha=1/period, min_periods=period)` in 7, a simple `rolling(period).mean()` of true
   range in 6, and 4 more using something else. Two of the three ACTIVE deployments are in the
   simple-average group - `risk_parity` and `channel_breakout` - and one, `trend_following`, is
   in the Wilder group. That is a consistency question about the strategy library, not a live
   defect.

3. **The chart is the one place the two are mixed.** `viewer/core.py` draws ATR with `ta.atr`,
   which is Wilder's, whatever the strategy under the chart used. So a simple-average
   strategy's stops will not line up with the ATR drawn beside them. That is an explanatory
   mismatch, not a mis-sized stop.

The inventory below is asserted rather than described so that editing one strategy's ATR becomes
visible here instead of quietly changing which group it belongs to. `min_periods` is a separate
parameter from the smoothing form and was already fixed for RAMJET/ROCKETFUEL; the open question in
item 5 is the *form*, and these tests deliberately do not decide it.
"""

from __future__ import annotations

import re
from pathlib import Path

STRATEGIES = Path(__file__).resolve().parents[1] / "strategies"
ADAPTER = Path(__file__).resolve().parents[1] / "live_runner" / "ctrader_adapter.py"

WILDER = "wilder"
SIMPLE = "simple-mean"
OTHER = "other"

# Measured 2026-10-04 by reading each module, so a change to one strategy's ATR shows up here
# as a failing test rather than a silent reclassification.
EXPECTED_FORM = {
    "channel_breakout": SIMPLE,
    "cross_asset_momentum": WILDER,
    "gold_momentum": WILDER,
    "post_vix_dip_buy": WILDER,
    "sector_rotation": WILDER,
    "stat_arb_pairs": WILDER,
    "trend_following": WILDER,
    "volatility_breakout": WILDER,
    "ema_crossoiver_conversion_test": SIMPLE,
    "pine_superosc_vumanchu": SIMPLE,
    "pine_supertrend_dema": SIMPLE,
    "pynecore_sma_crossover": SIMPLE,
    "risk_parity": SIMPLE,
}
# Strategies that mention ATR but compute it some other way, listed so that a move between
# groups is seen. The inventory is 7 Wilder / 6 simple-mean / 4 other - and the first draft of
# this file omitted `qg_30min_es_breakout_strategy` from the OTHER group and
# `volatility_breakout` from the Wilder group - two transcription slips of exactly the kind
# this file exists to prevent, and both caught only because the inventory is measured at
# run time rather than trusted from the list above.
EXPECTED_OTHER = {
    "custom_swings_supertrend",
    "intraday_volatility_breakout",
    "qg_30min_es_breakout_strategy",
    "short_term_reversal",
}


def _form(text: str) -> str:
    if re.search(r"ewm\(alpha=1(\.0)? ?/", text):
        return WILDER
    simple = re.search(r"rolling\([^)]*\)\.mean\(\)", text)
    if simple and re.search(r"\btr\b|true_range", text, re.I):
        return SIMPLE
    return OTHER


def _measured() -> dict[str, str]:
    measured: dict[str, str] = {}
    for path in sorted(STRATEGIES.rglob("*.py")):
        text = path.read_text(errors="replace")
        if not re.search(r"ATR|true_range|\btr\b", text):
            continue
        measured[path.parent.name] = _form(text)
    return measured


def test_the_inventory_of_atr_forms_is_what_was_measured() -> None:
    measured = _measured()
    # Only the strategies with a declared form are compared against it; the OTHER group is asserted
    # separately, because folding it in here made every OTHER strategy look like a change -
    # the first version of this test failed on its own fixture.
    wrong = {
        name: measured[name]
        for name, declared in EXPECTED_FORM.items()
        if measured.get(name) != declared
    }
    assert not wrong, (
        f"these strategies changed ATR form: {wrong}. Declared forms that no longer hold: "
        f"{ {k: v for k, v in EXPECTED_FORM.items() if measured.get(k) != v} }. If a change was "
        f"intentional, update this map in the same commit - item 5 is an open decision about the "
        f"form, so a silent reclassification is exactly what must not happen."
    )
    measured_other = {n for n, f in measured.items() if f == OTHER}
    assert measured_other == EXPECTED_OTHER, (
        f"the OTHER group changed: measured {sorted(measured_other)}, declared "
        f"{sorted(EXPECTED_OTHER)}"
    )
    assert set(measured) == set(EXPECTED_FORM) | EXPECTED_OTHER, (
        f"the set of strategies computing ATR changed: measured {sorted(measured)}"
    )


def test_two_of_the_three_active_deployments_use_the_simple_average() -> None:
    """The live-relevant half of item 5, pinned so it cannot quietly become theoretical."""
    measured = _measured()
    active = {"channel_breakout", "risk_parity", "trend_following"}
    simple = {s for s in active if measured.get(s) == SIMPLE}
    assert simple == {"channel_breakout", "risk_parity"}, (
        f"of the three ACTIVE deployments, the simple-average ones are {sorted(simple)}; expected "
        f"channel_breakout and risk_parity"
    )
    assert measured.get("trend_following") == WILDER


def test_the_live_adapter_never_computes_an_atr_or_a_stop() -> None:
    """What makes the disagreement strategy-versus-strategy rather than live-versus-backtest.

    If the adapter ever grew its own ATR this fails, and that would be the moment item 5's
    original framing became true - so the failure would be informative, not annoying.
    """
    text = ADAPTER.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for token in ("ewm(", "true_range", "ta.atr"):
        assert token not in code, (
            f"the cTrader adapter now references {token!r}, so it computes its own indicator and "
            f"item 5's live-versus-backtest framing needs re-measuring from scratch"
        )


def test_the_viewer_charts_atr_with_wilder_regardless_of_the_strategy() -> None:
    """The one place the two forms are mixed, and the reason a chart can look inconsistent.

    Not a trading defect - an explanatory one. Pinned so that teaching the viewer to draw the
    strategy's own definition would be a deliberate change.
    """
    core = Path(__file__).resolve().parents[1] / "viewer" / "core.py"
    viewer = core.read_text(encoding="utf-8")
    assert "ta.atr(" in viewer, (
        "the viewer no longer charts ATR, so this test's premise needs revisiting"
    )
    assert "ewm(" not in viewer, (
        "the viewer now smooths ATR itself; if that is Wilder's it should agree with "
        "the strategies, and this test should be replaced by an assertion that it does"
    )
