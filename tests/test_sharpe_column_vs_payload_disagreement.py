"""Which of the two disagreeing Sharpe values the platform actually uses.

53 backtests carry `sharpe_ratio = 0.0` in the column and a non-zero `stats.sharpe_ratio` in the
payload. Until now the disposition question treated them as symmetric - as if correcting either one
were equally correct. It is not, and the asymmetry is decidable from the load path alone:

    BacktestResult(stats=backtest.stats or {})        shared/database.py:5625
    viewer -> _stats_payload_from_result -> raw["sharpe_ratio"]   viewer/core.py:1367, 1405

So the payload is what the viewer displays, what the API returns, and what a reader sees in the
backtest list. The column is read by nothing on the display path; it is written by
`_scalar_stats_for_save` and read back by the same function to carry a value forward across a re-save.

That makes the disposition one-sided: for these 53 rows the *column* is stale, not the payload.
Correcting the column from the payload is the safe direction, because it moves a value nothing
displays toward the value everything displays. The reverse - overwriting the payload with 0.0 to
agree with the column - would erase 53 real Sharpe values from the UI.

Readiness is unaffected either way: `validator.py` compares `expected_stats`/`actual_stats` from
`validation/stats/calculator.py`, which recomputes Sharpe from the curve rather than reading either
stored value.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")
REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not PRODUCTION_DB.exists(), reason="production db absent, so there is no corpus to inspect"
)


def _disagreeing() -> list[tuple[str, float, float]]:
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        out = []
        for bid, column, stats in conn.execute(
            "select id, sharpe_ratio, stats from backtests"
        ):
            if not stats:
                continue
            try:
                parsed = json.loads(stats)
            except (TypeError, ValueError):
                continue
            raw = parsed.get("sharpe_ratio", parsed.get("sharpe"))
            if raw is None:
                continue
            column_value = float(column)
            payload_value = float(raw)
            if abs(payload_value - column_value) > 1e-9:
                out.append((bid, column_value, payload_value))
        return out


def test_the_corpus_still_holds_exactly_53_disagreeing_rows() -> None:
    """Anchors the count so a future backfill makes this fail loudly rather than silently pass."""
    assert len(_disagreeing()) == 53, (
        f"{len(_disagreeing())} rows now disagree between column and payload; if these have been "
        f"reconciled this whole file's premise is resolved and should be replaced with a regression "
        f"test that they stay reconciled"
    )


def test_the_viewer_reads_the_payload_not_the_column() -> None:
    """The decisive claim, asserted against source rather than described in a docstring.

    A docstring asserting "the viewer reads the payload" is worth nothing if the load path changes.
    """
    viewer = (REPO / "viewer" / "core.py").read_text(encoding="utf-8")
    database = (REPO / "shared" / "database.py").read_text(encoding="utf-8")

    assert "stats=backtest.stats or {}" in database, (
        "BacktestResult is no longer constructed from the stats payload, so which value the viewer "
        "surfaces must be re-derived before trusting this file"
    )
    assert 'raw.get("sharpe_ratio", 0)' in viewer, (
        "the viewer no longer reads sharpe_ratio out of the canonical stats payload; if it now reads "
        "the column instead, the direction of the safe correction reverses and this file is wrong"
    )
    # The column must not appear on the display path at all.
    assert "BacktestRecord.sharpe_ratio" not in viewer, (
        "the viewer now reads the sharpe_ratio column directly, so both values are displayed and the "
        "one-sidedness argument no longer holds"
    )


def test_only_the_column_is_stale_so_the_correction_direction_is_one_sided() -> None:
    """For every disagreeing row the column is 0.0 and the payload is not.

    That is what makes correcting the column safe and correcting the payload destructive. If a row
    ever disagreed the other way - column carrying a real value that the payload contradicts - the
    payload would be the stale one and this argument would need inverting.
    """
    rows = _disagreeing()
    assert rows
    assert all(column == 0.0 for _, column, _ in rows), (
        "a row now disagrees with a non-zero column and a zero payload; the stale side has changed "
        "and the safe correction direction is no longer 'always from the payload'"
    )
    assert all(payload != 0.0 for _, _, payload in rows), (
        "a row now has 0.0 in both places, which is agreement rather than disagreement and means the "
        "counting logic needs re-deriving"
    )


def test_readiness_reads_neither_stored_value() -> None:
    """Why this disposition does not touch the Sharpe tolerance question at all.

    `validator.py` compares `expected_stats` against `actual_stats`, and both come from
    `validation/stats/calculator.py`, which recomputes Sharpe from the equity curve. So the 53 rows'
    disagreement cannot move a readiness verdict, and fixing them does not change item 14.
    """
    validator = (REPO / "validation" / "readiness" / "validator.py").read_text(encoding="utf-8")
    calculator = (REPO / "validation" / "stats" / "calculator.py").read_text(encoding="utf-8")

    assert "expected_sharpe = float(expected_stats.sharpe_ratio)" in validator
    assert "sharpe_ratio=sharpe_ratio" in calculator, (
        "the readiness calculator no longer computes sharpe_ratio, so the claim that readiness "
        "recomputes it from the curve - and therefore ignores both stored values - needs re-deriving"
    )
    # The calculator must derive it, not read a column.
    assert "BacktestRecord" not in calculator, (
        "the readiness calculator now reads the database directly; if it reads the stored column, "
        "the 53 rows DO affect readiness verdicts and item 14 is coupled to this disposition"
    )
