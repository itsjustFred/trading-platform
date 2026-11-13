"""Regenerate the reference-parity fixtures under ``tests/parity/fixtures/``.

The trading-platform native engine is differentially tested against three
strategies whose *original* implementation (``backtesting.py``, AGPL-3.0) and
full trade list this script produces. The original library is the reference; it
is never vendored into the repository, only its (numeric) output is committed,
so ``pytest tests/parity`` runs in CI without the reference dependency.

Usage (from the repository root, with a venv that has ``backtesting`` installed):

    python scripts/reference_parity/generate_reference_parity_fixtures.py
    python -m scripts.reference_parity.generate_reference_parity_fixtures.py

What it does
------------
1. builds the deterministic fixture OHLCV (seeded synthetic daily bars),
2. runs each reference strategy on it (``FractionalBacktest`` so sizes are
   fractional shares) and records the trade list,
3. **refuses to write** unless the fixture keeps the comparison clean:
   no stop/target is ever gapped through, the reference's "one position at a
   time" gate never binds (so the platform's stateless port is equivalent), and
   no trade is still open at the end of the data.

Those three invariants are what separates a *semantic* difference (documented in
``docs/reports/parity-reference-libraries.md``) from an *engine bug*; a fixture
that broke one of them would silently turn a real defect into an accepted
difference, so the generator fails loudly instead.

The data is synthetic by design: the bundled sample data that ships with the
reference library (``backtesting.test.GOOG``, Yahoo Finance quotes) is not
redistributable with confidence, so only the numeric trade lists are committed.
A local GOOG run is used for the report's cross-check against the library's
published quickstart statistics.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "parity" / "fixtures"
DATA_FIXTURE = FIXTURE_DIR / "ref_parity_daily.csv"

#: Fixed deterministic inputs for the fixture data.
SEED = 20240617
#: Total bars generated before the fixture is trimmed to a point where every
#: strategy holds no open position (see ``flat_trim_point``).
BARS = 2400
START = "2019-01-02"
START_PRICE = 88.0

#: (slug, reference strategy, backtesting.py commission)
RUNS = (
    ("smacross_all_in", "SmaCross", "QUICKSTART"),
    ("bracket_long", "BracketLong", "ZERO"),
    ("bracket_short", "BracketShort", "ZERO"),
)


def make_ohlcv(bars: int = BARS, seed: int = SEED) -> pd.DataFrame:
    """Deterministic daily OHLCV: alternating regime drift, overnight gap, wicks.

    Open = previous close times an overnight gap, high/low straddle the body.
    Every generated bar keeps ``low <= open, close <= high`` so the series is a
    legal OHLC frame on every engine.
    """
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(START, periods=bars, tz="UTC")
    drift = np.array([0.0009 if (i // 90) % 2 == 0 else -0.0007 for i in range(bars)])
    returns = drift + rng.normal(0.0, 0.0095, bars)
    gaps = rng.normal(0.0, 0.0020, bars)
    wick_up = np.abs(rng.normal(0.0, 0.0035, bars))
    wick_dn = np.abs(rng.normal(0.0, 0.0035, bars))

    closes = np.empty(bars)
    price = START_PRICE
    for i in range(bars):
        price = max(5.0, price * math.exp(returns[i]))
        closes[i] = round(float(price), 4)

    opens = np.empty(bars)
    previous = closes[0]
    for i in range(bars):
        opens[i] = round(float(previous * (1.0 + gaps[i])), 4)
        previous = closes[i]

    body_high = np.maximum(opens, closes)
    body_low = np.minimum(opens, closes)
    frame = pd.DataFrame(
        {
            "timestamp": index,
            "open": opens,
            "high": np.round(body_high + wick_up * body_high + 0.01, 4),
            "low": np.round(np.maximum(body_low - wick_dn * body_low - 0.01, 0.5), 4),
            "close": closes,
            "volume": np.round(rng.uniform(500_000, 1_500_000, bars), 0).astype(int),
        }
    )
    assert (frame["low"] <= frame["open"]).all()
    assert (frame["open"] <= frame["high"]).all()
    assert (frame["low"] <= frame["close"]).all()
    assert (frame["close"] <= frame["high"]).all()
    return frame


# ───────────────────────────── invariant checks ──────────────────────────────


def _bar_at(frame: pd.DataFrame, timestamp: pd.Timestamp) -> pd.Series:
    match = frame[frame["timestamp"] == pd.Timestamp(timestamp)]
    assert len(match) == 1, f"timestamp {timestamp} not unique in fixture"
    return match.iloc[0]


def protective_gap_throughs(frame: pd.DataFrame, trades: pd.DataFrame) -> list[str]:
    """Report exits whose reference fill beat or lost to the level by a gap.

    backtesting.py fills a contingent stop as ``min(open, level)`` and a
    contingent target as ``max(open, level)``; the platform fills both at the
    level itself (docs/cross-engine-backtest-contract.md section 1). Whenever
    the exit bar *opens through* the level the two frameworks disagree by more
    than a convention, so such trades are not usable for a parity fixture.
    """
    problems = []
    for _, trade in trades.iterrows():
        if trade["exit_kind"] == "end_of_data":
            continue
        bar = _bar_at(frame, trade["exit_time"])
        open_price = float(bar["open"])
        level = float(trade["stop_level"] if trade["exit_kind"] == "stop_loss" else trade["take_profit_level"])
        if trade["side"] == "LONG":
            through = open_price < level if trade["exit_kind"] == "stop_loss" else open_price > level
        else:
            through = open_price > level if trade["exit_kind"] == "stop_loss" else open_price < level
        if through:
            problems.append(
                f"trade {int(trade['trade'])} ({trade['side']} {trade['exit_kind']}): "
                f"exit bar {trade['exit_time']} opens {open_price:.4f} through level {level:.4f}"
            )
    return problems


def trades_open_at_end(trades: pd.DataFrame) -> list[str]:
    return [
        f"trade {int(row['trade'])} closed by the forced end-of-data close at {row['exit_time']}"
        for _, row in trades.iterrows()
        if row["exit_kind"] == "end_of_data"
    ]


def _ungated(strategy):
    """Subclass the strategy with its one-position-at-a-time gate disabled."""
    return type(f"Ungated{strategy.__name__}", (strategy,), {"gate": False})


def flat_trim_point(frame: pd.DataFrame, runs) -> int:
    """Largest bar count at which *every* strategy holds no open position.

    The reference forces a still-open trade closed on the last bar (at that
    bar's *open*), while the platform closes it at the last bar's *close* — a
    documented convention difference. The fixture is trimmed to a bar count
    where every strategy is flat, so the recorded trade lists isolate the
    semantics the comparison is about instead of that difference.

    ``runs`` is ``[(strategy, commission), ...]``. A bar count ``N`` is unusable
    when some trade *spans* it (``entry_bar < N <= exit_bar``): the reference
    run would leave that trade open and force it closed. The answer is the
    largest bar count that no trade of any strategy spans.
    """
    from scripts.reference_parity.reference_strategies import run_reference

    spanned = set()
    for strategy, commission in runs:
        trades, _ = run_reference(strategy, frame, commission=commission)
        spanned.update(
            range(int(row["entry_bar"]) + 1, int(row["exit_bar"]) + 1)
            for _, row in trades.iterrows()
        )
    for bar_count in range(len(frame), 0, -1):
        if bar_count not in spanned:
            return bar_count
    return 0


def gate_bound(frame: pd.DataFrame, strategy, commission: float) -> str:
    """Verify the reference's position gate never changes the outcome on this fixture.

    The platform port is *stateless*: it cannot know whether a position is open,
    so it signals on every qualifying bar. If the reference's gate ever suppressed
    an entry the two would diverge for a reason that is not an engine defect.
    Running the same strategy with the gate disabled must produce an identical
    trade list; the check below proves that on this fixture.
    """
    from scripts.reference_parity.reference_strategies import run_reference

    gated, _ = run_reference(strategy, frame, commission=commission)
    ungated, _ = run_reference(_ungated(strategy), frame, commission=commission)
    if not gated.equals(ungated):
        return (
            f"{strategy.__name__}: the one-position-at-a-time gate changed the trade list "
            f"({len(gated)} gated vs {len(ungated)} ungated trades)"
        )
    return ""


# ────────────────────────────────── driver ───────────────────────────────────


def _commission_for(name: str) -> float:
    return {"QUICKSTART": 0.002, "ZERO": 0.0}[name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="run the reference strategies and the invariant checks, write nothing",
    )
    parser.add_argument(
        "--print-trades",
        action="store_true",
        help="print every recorded trade list (for the report table)",
    )
    args = parser.parse_args(argv)

    from scripts.reference_parity.reference_strategies import (
        BracketLong,
        BracketShort,
        SmaCross,
        run_reference,
    )

    strategies = {"SmaCross": SmaCross, "BracketLong": BracketLong, "BracketShort": BracketShort}
    full = make_ohlcv()

    # Trim the fixture to the longest bar count at which *every* strategy is
    # flat, so the forced end-of-data close (reference: last bar's open,
    # platform: last bar's close) never enters the comparison.
    runs = [
        (strategies[name], _commission_for(comm)) for _, name, comm in RUNS
    ]
    bars = flat_trim_point(full, runs)
    frame = full.iloc[:bars].reset_index(drop=True)
    print(f"fixture data: {len(full)} bars generated, trimmed to {bars} bars "
          f"(last strategy-flat bar)")

    failures: list[str] = []
    summary = []
    for slug, strategy_name, commission_name in RUNS:
        strategy = strategies[strategy_name]
        commission = _commission_for(commission_name)
        trades, final_equity = run_reference(strategy, frame, commission=commission)
        problems = protective_gap_throughs(frame, trades) + trades_open_at_end(trades)
        problems.append(gate_bound(frame, strategy, commission))
        problems = [p for p in problems if p]
        summary.append((slug, strategy_name, commission, len(trades), final_equity, problems))
        if not args.no_write and not problems:
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            target = FIXTURE_DIR / f"ref_parity_{slug}_trades.csv"
            trades.to_csv(target, index=False)
        if args.print_trades:
            print(f"\n== {slug} ({strategy_name}, commission={commission}) "
                  f"trades={len(trades)} final_equity={final_equity:.6f}")
            print(trades.to_string(index=False))

    if not args.no_write and not any(p for *_, problems in summary for p in problems):
        DATA_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(DATA_FIXTURE, index=False)
        print(f"wrote {DATA_FIXTURE.relative_to(REPO_ROOT)} ({len(frame)} bars)")
    else:
        print(f"data fixture unchanged: {DATA_FIXTURE.relative_to(REPO_ROOT)}")

    print(f"\n{'strategy':18s} {'ref':22s} {'comm':>6s} {'trades':>6s} {'final equity':>14s}")
    for slug, strategy_name, commission, count, equity, problems in summary:
        print(f"{slug:18s} {strategy_name:22s} {commission:6.3f} {count:6d} {equity:14.6f}")
        for problem in problems:
            print(f"  INVARIANT VIOLATION: {problem}")
            failures.append(f"{slug}: {problem}")

    if failures:
        print("\nrefusing to write fixtures: the comparison is not clean", file=sys.stderr)
        return 1
    if args.no_write:
        print("\n--no-write: nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())