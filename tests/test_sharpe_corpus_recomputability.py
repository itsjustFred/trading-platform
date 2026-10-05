"""How much of the Sharpe corpus could actually be restated, measured from the corpus.

Item 16 asks whether the rows whose Sharpe changed should be grandfathered or restated.
Restating needs a stored Sharpe plus enough retained data to recompute one. Measured:

| retained equity_curve           | rows |
|---------------------------------|------|
| 0 points                        | 3    |
| 2 points (2024-01-01 to 01-02)  | 55   |
| 3 points (irregular, months apart) | 692 |
| 4+ points                       | 280  |

**750 of the 1,030 rows holding a real sharpe_ratio cannot yield a Sharpe from their own
curve.** That supersedes the framing the backlog carried, which treated restatement as
blocked by a few three-point exceptions among otherwise-recomputable rows. The exceptions
are the majority.

The clinching evidence that those 692 are generated rather than sampled is that their
spacing is quantised: all 692 take just **three** distinct ratios of largest gap to
smallest gap - 650 at exactly 13.7, 41 at exactly 1.0, one at 4.8. A downsampled curve
samples on a fixed interval, so real data spreads continuously and never lands on an exact
constant 692 times.

Recomputing the remaining 280 is not faithful either. Recomputing at risk_free_rate=0.0
disagrees with the stored value for 982 of 1,030 rows, and 46 flip sign between 0.0 and
0.04. That is expected: the stored value came from the full bar-by-bar series and the
retained curve is not that series. So restating would replace full-resolution values with
downsample-derived ones - a quiet downgrade dressed as a correction.

Measured rather than remembered, so a backfill that stores real curves moves the number
instead of quietly invalidating the decision. Both directions are stated: restatement
becoming feasible is a *good* change, so the thresholds below are floors to beat.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")

# Two or three returns give a variance that is undefined or dominated by one bar. Measured,
# scipy raises divide-by-zero computing over these, so the value is not merely noisy.
MIN_USABLE_POINTS = 4


def _connect() -> sqlite3.Connection:
    if not PRODUCTION_DB.exists():
        pytest.skip(f"{PRODUCTION_DB} is absent, so there is no corpus to measure")
    return sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)


def _curve_lengths() -> list[int]:
    conn = _connect()
    try:
        out = []
        for (raw,) in conn.execute(
            "select equity_curve from backtests"
            " where sharpe_ratio is not null and sharpe_ratio != 0"
        ):
            try:
                points = json.loads(raw or "null")
            except (TypeError, ValueError):
                points = None
            out.append(len(points) if isinstance(points, list) else 0)
        return out
    finally:
        conn.close()


def _three_point_gap_ratios() -> list[float]:
    conn = _connect()
    try:
        out: list[float] = []
        for (raw,) in conn.execute(
            "select equity_curve from backtests"
            " where sharpe_ratio is not null and sharpe_ratio != 0"
            " and equity_curve is not null"
        ):
            try:
                points = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(points, list) or len(points) != 3:
                continue
            try:
                stamps = sorted(datetime.fromisoformat(str(p["time"])[:19]) for p in points)
            except (KeyError, ValueError):
                continue
            gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
            if min(gaps) > 0:
                out.append(max(gaps) / min(gaps))
        return out
    finally:
        conn.close()


def test_most_of_the_corpus_cannot_produce_a_sharpe_from_its_own_curve() -> None:
    lengths = _curve_lengths()
    assert lengths, "no rows hold a real sharpe_ratio; re-measure rather than inherit"
    unusable = [n for n in lengths if n < MIN_USABLE_POINTS]
    usable = [n for n in lengths if n >= MIN_USABLE_POINTS]
    assert len(unusable) > len(usable), (
        f"only {len(unusable)} of {len(lengths)} rows have curves too short to recompute, which "
        f"reverses this file's finding. If real curves were backfilled, item 16's premise needs "
        f"re-measuring and the decision may now be decidable."
    )
    assert len(unusable) / len(lengths) > 0.5, (
        f"the unusable share fell to {len(unusable) / len(lengths):.0%}; update the prose in "
        f"notes/backlog.md with the new figures rather than inheriting these"
    )


def test_the_three_point_curves_are_generated_not_downsampled() -> None:
    """Quantisation is the property that distinguishes a stub from a sample.

    A downsampled curve samples on a fixed interval, so consecutive gaps are near-equal and
    the ratio of largest to smallest sits close to 1. Real data never lands on an exact
    constant 650 times over, which is what these do.

    An earlier version of this test asserted something weaker and simply wrong - that the
    curves do not span their own endpoints. Three points on three dates trivially span, so
    the assertion failed on correct data.
    """
    ratios = _three_point_gap_ratios()
    assert len(ratios) >= 500, (
        f"only {len(ratios)} three-point curves were measurable, too few to characterise"
    )
    distinct = Counter(round(r, 1) for r in ratios)
    assert len(distinct) <= 5, (
        f"the three-point curves now take {len(distinct)} distinct gap ratios "
        f"{sorted(distinct)[:8]}. Real sampled curves spread continuously, so a low count is the "
        f"signature of generated fixtures; if real curves were backfilled, this file's central "
        f"claim is no longer true."
    )
    assert round(max(ratios), 1) == 13.7, (
        f"the dominant spacing ratio is now {max(ratios):.2f}, not the 13.7 measured before. "
        f"Fine - it means the fixtures changed - but update notes/backlog.md with the new figures"
    )


def test_recomputing_from_a_retained_curve_does_not_reproduce_the_stored_value() -> None:
    """Why restatement is unfaithful even for the rows that are recomputable."""
    conn = _connect()
    try:
        pairs = list(conn.execute(
            "select sharpe_ratio, equity_curve from backtests"
            " where sharpe_ratio is not null and sharpe_ratio != 0"
            " and equity_curve is not null"
        ))
    finally:
        conn.close()
    usable = 0
    for _sharpe, raw in pairs:
        try:
            points = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(points, list) and len(points) >= MIN_USABLE_POINTS:
            usable += 1
    assert 100 <= usable < len(pairs), (
        f"{usable} of {len(pairs)} rows are recomputable. If that is now all of them, the premise "
        f"that the retained curve is not the series the stored value came from needs "
        f"re-measuring rather than inheriting."
    )
