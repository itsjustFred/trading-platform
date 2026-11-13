"""Regenerate the reference-parity fixtures under ``tests/parity/fixtures/``.

The trading-platform native engine is differentially tested against three
strategies whose *original* implementation (``backtesting.py``, AGPL-3.0) and
full trade list this script produces. The original library is the reference; it
is never vendored into the repository, only its (numeric) output is committed,
so ``pytest tests/parity`` runs in CI without the reference dependency.

Usage (from the repository root, with a venv that has ``backtesting`` installed):

    python -m scripts.reference_parity.generate_reference_parity_fixtures.py
    python scripts/reference_parity/generate_reference_parity_fixtures.py   # same thing
    python -m scripts.reference_parity.generate_reference_parity_fixtures.py --no-write

``--no-write`` runs every reference strategy and every invariant check and writes
nothing: use it to re-verify the committed fixtures (or a new seed) without
touching them. The four recorded runs are the three quickstart/bracket strategies
plus a **zero-commission** SmaCross, which is the case where the platform and the
library size identically, so the exit-fill convention is the only remaining
difference and the fixture pins it exactly.

What it does
------------
1. builds the deterministic fixture OHLCV (seeded synthetic daily bars),
2. runs each reference strategy on it (``FractionalBacktest`` so sizes are
   fractional shares) and records the trade list,
3. **refuses to write** unless the fixture keeps the comparison clean: the
   reference's "one position at a time" gate never binds (so the platform's
   stateless port is equivalent) and no trade is still open at the end of the
   data (the forced close differs: reference last open, platform last close).
   Stops/targets that the exit bar opens through are NOT refused any more: both
   frameworks fill them at the open (platform rule #208 A), they are recorded in
   the manifest (``gap_through_exits``) and compared exactly.

Those invariants separate a *semantic* difference (documented in
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
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "parity" / "fixtures"
DATA_FIXTURE = FIXTURE_DIR / "ref_parity_daily.csv"
SUMMARY_FIXTURE = FIXTURE_DIR / "ref_parity_summary.json"

# A direct ``python scripts/reference_parity/generate_reference_parity_fixtures.py``
# puts *this* directory on sys.path, not the repository root, so the absolute
# ``scripts.reference_parity.reference_strategies`` imports below would fail. This
# module imports nothing from ``shared`` (see tests/test_script_import_origin.py:
# the guard exists to stop ``shared`` being resolved from a different checkout),
# so the root may be prepended safely. The canonical invocation is still
# ``python -m`` from the root.
if __package__ in (None, ""):  # pragma: no cover - exercised only by a direct run
    sys.path.insert(0, str(REPO_ROOT))

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
    # zero-commission twin of the quickstart run: with no commission to gross
    # up, ``size=1`` and the platform's notional cap produce the *same* share
    # count, so the suite can assert exact quantity equality and pin the
    # exit-fill convention as the only difference (see the report).
    ("smacross_zero_fee", "SmaCross", "ZERO"),
    ("bracket_long", "BracketLong", "ZERO"),
    ("bracket_short", "BracketShort", "ZERO"),
)


def make_ohlcv(bars: int = BARS, seed: int = SEED) -> pd.DataFrame:
    """Deterministic daily OHLCV: alternating regime drift, overnight gap, wicks.

    Open = previous close times an overnight gap, high/low straddle the body.
    Overnight gaps are mostly small, with a fat tail of larger jumps so that some stops and
    targets are opened through.  Every generated bar keeps ``low <= open, close <= high`` so the series is a
    legal OHLC frame on every engine.
    """
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(START, periods=bars, tz="UTC")
    drift = np.array([0.0009 if (i // 90) % 2 == 0 else -0.0007 for i in range(bars)])
    returns = drift + rng.normal(0.0, 0.0095, bars)
    gaps = rng.normal(0.0, 0.0020, bars)
    # occasional news-sized overnight gaps (~8 % of bars, sigma 2.5 %): some of them open
    # beyond a stop or a target, which is the gap-fill rule the parity run must exercise.
    jump = rng.random(bars) < 0.08
    gaps = gaps + np.where(jump, rng.normal(0.0, 0.025, bars), 0.0)
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
    """Protective exits whose bar OPENED through the level (stop gapped down / target gapped up).

    backtesting.py fills a contingent stop as ``min(open, level)`` and a contingent target as
    ``max(open, level)`` (``Backtest._process_orders``), i.e. at the OPEN when the bar opens
    beyond the level and at the level on an intrabar touch.  The platform adopted the same rule
    on 2026-11-13 (#208 A), so these trades are now part of the exact comparison instead of being
    refused; they are listed so the manifest can prove the fixture exercises both fill rules.
    """
    through = []
    for _, trade in trades.iterrows():
        if trade["exit_kind"] == "end_of_data":
            continue
        stop_side = trade["exit_kind"] == "stop_loss"
        level_key = "stop_level" if stop_side else "take_profit_level"
        bar = _bar_at(frame, trade["exit_time"])
        open_price = float(bar["open"])
        level = float(trade[level_key])
        if trade["side"] == "LONG":
            opened_through = open_price < level if stop_side else open_price > level
        else:
            opened_through = open_price > level if stop_side else open_price < level
        if opened_through:
            through.append(f"{int(trade['trade'])}:{trade['exit_kind']}")
    return through


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


def _backtesting_version() -> str:
    import backtesting

    return str(backtesting.__version__)


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
        FRACTIONAL_UNIT,
        REFERENCE_CASH,
        SIZE_FRACTIONS,
        BracketLong,
        BracketShort,
        SmaCross,
        reference_size_fraction,
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
    #: What the committed fixture is, so the test suite can re-derive every recorded
    #: quantity from the trade list alone (see ``run_summary`` in the parity test).
    manifest: dict[str, object] = {
        "generator": "scripts/reference_parity/generate_reference_parity_fixtures.py",
        "library": f"backtesting.py {_backtesting_version()}",
        "license": "AGPL-3.0 (reference library; only its numeric output is committed)",
        "seed": SEED,
        "bars": len(frame),
        "bars_generated": len(full),
        "cash": REFERENCE_CASH,
        "fractional_unit": FRACTIONAL_UNIT,
        "size_fractions": dict(SIZE_FRACTIONS),
        "data_fixture": DATA_FIXTURE.name,
        "runs": {},
    }
    for slug, strategy_name, commission_name in RUNS:
        strategy = strategies[strategy_name]
        commission = _commission_for(commission_name)
        trades, final_equity = run_reference(strategy, frame, commission=commission)
        gap_exits = protective_gap_throughs(frame, trades)
        problems = trades_open_at_end(trades)
        problems.append(gate_bound(frame, strategy, commission))
        problems = [p for p in problems if p]
        summary.append((slug, strategy_name, commission, len(trades), final_equity, problems))
        manifest["runs"][slug] = {
            "library_strategy": strategy_name,
            "commission": commission,
            "size_fraction": reference_size_fraction(strategy),
            "trades": int(len(trades)),
            "final_equity": float(final_equity),
            "exit_kinds": {
                str(kind): int(count)
                for kind, count in trades["exit_kind"].value_counts().items()
            },
            "gap_through_exits": gap_exits,
            "invariant_violations": list(problems),
        }
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
        payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        SUMMARY_FIXTURE.write_text(payload, encoding="utf-8")
        print(f"wrote {SUMMARY_FIXTURE.relative_to(REPO_ROOT)} ({len(manifest['runs'])} runs)")
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
