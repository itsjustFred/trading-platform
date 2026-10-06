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


def test_the_two_definitions_diverge_enough_to_change_a_stop() -> None:
    """Quantified, because item 5 is a trading judgement and needs a number, not a shrug.

    The chart draws Wilder's whatever the strategy used. If the two agree to within a rounding
    artefact, the mismatch is cosmetic and item 5 can stay closed. They do not: on a synthetic but
    realistic series, after warmup, the median gap is around 7% and the tail reaches ~40%.

    That is the size of the disagreement item 5 turns on - the same order as the "~10%" in the item's
    original title. It is measured here against synthetic data rather than production, because no
    production equity curve carries a stop distance this can be checked against, so this pins the
    arithmetic and the direction rather than a production figure.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    n = 600
    close = pd.Series(100 + np.cumsum(rng.normal(0, 0.35, n)))
    high = (close + rng.uniform(0.05, 0.5, n)).clip(upper=close)
    low = (close - rng.uniform(0.05, 0.5, n)).clip(lower=close)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)

    for period in (14, 20):
        simple = tr.rolling(period).mean()
        wilder = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        tail = slice(period + 150, None)
        gap = ((wilder[tail] - simple[tail]) / simple[tail] * 100).abs()
        assert gap.median() > 3.0, (
            f"period {period}: the two definitions now differ by only {gap.median():.2f}% at the "
            f"median, so the chart/strategy mismatch is cosmetic and item 5 can be closed"
        )
        assert gap.max() > 20.0, (
            f"period {period}: the tail gap is only {gap.max():.2f}%, so a stop placed on one "
            f"definition would rarely be touched by the other"
        )


def test_the_gap_carries_a_consistent_sign_so_it_is_not_noise() -> None:
    """Pins the sign of the gap across seeds, which is the honest form of the direction claim.

    An earlier draft of this test asserted Wilder's ATR sits below the simple mean more than 90% of
    the tail on a rising-volatility series. It measured **69%**, and re-running three seeds gave 69%,
    72% and 72% - so the sign is real but the overwhelming-frequency claim was an artefact of how I
    built the series, not a property of the two smoothings. Asserting the 90% figure would have been
    inventing a threshold to fit, so it is replaced by the claim that survives: the *median* gap is
    negative, consistently, across seeds.

    The magnitude here (~1.5%) is much smaller than the 7% median in the test above, because this
    series ramps gently and its true range is dominated by the close's own drift. That difference is
    the point worth recording: how far apart the two definitions sit depends on the series, so item
    5's cost is not a fixed percentage and cannot be quoted without naming the data.
    """
    import numpy as np
    import pandas as pd

    median_gaps = []
    for seed in (3, 7, 11):
        rng = np.random.default_rng(seed)
        n = 600
        vol = np.linspace(0.1, 1.2, n)
        close = pd.Series(100 + np.cumsum(rng.normal(0, 0.35, n)))
        high = close + vol * rng.uniform(0.5, 1.5, n)
        low = close - vol * rng.uniform(0.5, 1.5, n)
        tr = pd.concat(
            [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
        ).max(axis=1)
        period = 14
        simple = tr.rolling(period).mean()
        wilder = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        tail = slice(period + 300, None)
        median_gaps.append(float(((wilder[tail] - simple[tail]) / simple[tail] * 100).median()))

    assert all(g < 0 for g in median_gaps), (
        f"the median gap is no longer consistently negative across seeds ({median_gaps}), so the "
        f"two definitions no longer have a directional relationship and item 5's cost has to be "
        f"re-derived from scratch"
    )


def test_the_median_gap_is_modest_but_the_tail_is_where_a_stop_breaks() -> None:
    """The shape of the disagreement, which is what a stop actually feels.

    A first draft of this test claimed the gap scales with the series' volatility, on the reasoning
    that a more volatile series should separate the two smoothings more. Measured, it does not: a 4x
    more volatile series gave **1.84%** against a quiet one's **1.85%** - identical. Scale is not the
    driver, and asserting it would have been a plausible-sounding claim with no measurement behind it.

    What is true is the *distribution*: the median gap stays in the low single digits to ~7% while the
    95th percentile and the maximum reach roughly 24% and 41%. So the typical bar is only mildly
    misplaced, and the occasional one is badly misplaced - which is the case that matters, because a
    stop is a hard level rather than an average. This is the honest shape of item 5's cost, and it is
    why the earlier per-figure claims are replaced rather than restated.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    n = 600
    close = pd.Series(100 + np.cumsum(rng.normal(0, 0.35, n)))
    high = (close + rng.uniform(0.05, 0.5, n)).clip(upper=close)
    low = (close - rng.uniform(0.05, 0.5, n)).clip(lower=close)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    period = 14
    simple = tr.rolling(period).mean()
    wilder = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    tail = slice(period + 150, None)
    gap = ((wilder[tail] - simple[tail]) / simple[tail] * 100).abs()

    assert gap.median() < 10.0, (
        f"the median gap is now {gap.median():.1f}%, so a typical stop is only slightly "
        f"misplaced and item 5's cost is dominated by a few outliers rather than every bar"
    )
    assert float(np.percentile(gap, 95)) > 15.0, (
        f"the 95th percentile is only {np.percentile(gap, 95):.1f}%, so the tail has closed up and "
        f"a stop drawn from one definition would now be close to the other throughout"
    )
    assert gap.max() > 25.0, (
        f"the worst-case gap is only {gap.max():.1f}%, so the mismatch no longer produces a badly "
        f"misplaced stop and the chart/strategy difference is cosmetic"
    )


def test_the_gap_does_not_scale_with_volatility() -> None:
    """Pins the negative result, because it is the one a reader would most naturally assume.

    Scaling a series 4x changed the median gap by 0.01 percentage points. Anyone reasoning
    "more volatile means the two definitions diverge more" would be wrong, and the divergence that
    does exist comes from the shape of the tail rather than the level of volatility.
    """
    import numpy as np
    import pandas as pd

    def gap_for(scale: float) -> float:
        rng = np.random.default_rng(5)
        n = 600
        close = pd.Series(100 + np.cumsum(rng.normal(0, 0.35 * scale, n)))
        high = close + rng.uniform(0.05, 0.5, n) * scale
        low = close - rng.uniform(0.05, 0.5, n) * scale
        tr = pd.concat(
            [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
        ).max(axis=1)
        period = 14
        simple = tr.rolling(period).mean()
        wilder = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        tail = slice(period + 150, None)
        return float(((wilder[tail] - simple[tail]) / simple[tail] * 100).abs().median())

    quiet, wild = gap_for(1.0), gap_for(4.0)
    assert abs(wild - quiet) < 1.0, (
        f"a 4x volatility change moved the median gap from {quiet:.2f}% to {wild:.2f}%, so the gap "
        f"does scale with volatility after all and the tail explanation above is incomplete"
    )
