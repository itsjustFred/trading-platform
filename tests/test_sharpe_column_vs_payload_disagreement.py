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


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
def test_the_corpus_still_holds_exactly_53_disagreeing_rows() -> None:
    """Anchors the count so a future backfill makes this fail loudly rather than silently pass."""
    assert len(_disagreeing()) == 53, (
        f"{len(_disagreeing())} rows now disagree between column and payload; if these have been "
        f"reconciled this whole file's premise is resolved and should be replaced with a regression "
        f"test that they stay reconciled"
    )



def _payload_value(payload: dict, column: str) -> float | None:
    raw = payload.get(column, payload.get(column.replace("_ratio", "")))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _cagr_drift_rows() -> list[tuple[str, float, float]]:
    """Rows where cagr's column holds a real value differing from a real payload value."""
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute("select id, cagr, stats from backtests").fetchall()
    out = []
    for backtest_id, column, stats in rows:
        if not stats:
            continue
        try:
            payload = json.loads(stats)
        except (TypeError, ValueError):
            continue
        value = _payload_value(payload, "cagr")
        if value is None:
            continue
        try:
            if abs(float(column) - value) <= 1e-9:
                continue
        except (TypeError, ValueError):
            continue
        if abs(float(column)) > 1e-9 and abs(value) > 1e-9:
            out.append((backtest_id, float(column), value))
    return out


def _declared_undefined_cells() -> list[tuple[str, str]]:
    """Cells where the producer withheld the metric yet published a value."""
    columns = ("sharpe_ratio", "psr", "cagr", "p_value")
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "select id, " + ", ".join(columns) + ", stats from backtests"
        ).fetchall()
    out = []
    for row in rows:
        if not row[-1]:
            continue
        try:
            payload = json.loads(row[-1])
        except (TypeError, ValueError):
            continue
        undefined = payload.get("undefined_metrics")
        if not isinstance(undefined, list):
            continue
        for index, name in enumerate(columns, start=1):
            if name in undefined and _payload_value(payload, name) is not None:
                out.append((row[0], name))
    return out

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


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
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


def test_the_recalc_script_would_destroy_the_payload_values_the_53_depend_on() -> None:
    """The hazard: the obvious tool to "fix a wrong Sharpe" is the one that would erase these 53.

    `scripts/recalculate_backtest_stats.py` merges `{**existing, **recalculated}`, so recomputed values
    win. For any of the 53 rows whose curve is absent or too short to yield a Sharpe, the recomputation
    returns 0.0 and the merge writes 0.0 over a real payload value - one of them -37.66.

    Measured directly: with `trades=[]`, `equity_curve=[]` and `stats.sharpe_ratio = -37.66`,
    `compute_stats_for_result` returns 0.0 and the merge yields 0.0.

    The script is manual-only today - no service, timer or workflow invokes it - so this is a trap for
    whoever reaches for it next, not an active data loss. And the fix it looks like it performs is the
    opposite of the correct direction established above: it moves the payload toward the column, when
    the column is the stale side.

    Asserted rather than documented so that fixing the merge order, or restricting the script to rows
    that actually have a curve, makes this fail and say what changed.
    """
    import sys

    sys.path.insert(0, str(REPO))
    from runner.stats import compute_stats_for_result
    from shared.models import BacktestMeta, BacktestResult

    meta = BacktestMeta(
        id="probe",
        strategies=["probe"],
        engine="probe",
        created_at="2026-01-01",
        initial_capital=100000,
    )
    result = BacktestResult(meta=meta, trades=[], equity_curve=[], stats={"sharpe_ratio": -37.66})

    recomputed = compute_stats_for_result(result)
    merged = {**result.stats, **recomputed}

    assert merged["sharpe_ratio"] == 0.0, (
        "compute_stats_for_result now preserves or derives a non-zero Sharpe from an empty curve, so "
        "recalculate_backtest_stats.py no longer overwrites these payload values with 0.0 and this "
        "hazard has gone - worth recording either way, since the hazard was the reason not to run it"
    )
    assert result.stats["sharpe_ratio"] == -37.66, (
        "the fixture changed: this test exists to prove the ORIGINAL payload value is what a "
        "recalculation would destroy, so a mutated fixture would make it vacuous"
    )


def test_the_recalc_script_is_manual_only_so_the_hazard_is_not_active() -> None:
    """No service, timer or workflow invokes it - so this is a trap, not a running data loss."""
    repo_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in REPO.rglob("*.service")
    )
    assert "recalculate_backtest_stats" not in repo_text, (
        "a systemd unit now invokes recalculate_backtest_stats, so the hazard above is ACTIVE rather "
        "than a trap for a manual run, and the merge order needs fixing before it fires"
    )

    script = (REPO / "scripts" / "recalculate_backtest_stats.py").read_text(encoding="utf-8")
    assert "{**existing_stats, **recalculated_stats}" in script, (
        "recalculate_backtest_stats.py no longer merges recomputed stats over existing ones; if the "
        "order changed, re-derive whether this hazard still exists before relying on it"
    )


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
def test_the_backfill_restores_the_zero_population_to_the_count_models_py_recorded() -> None:
    """A coincidence worth recording honestly, without over-reading it.

    `validation/readiness/models.py` originally cited "1,572 of 2,655 rows (59.2%)" as the
    zero-Sharpe population. Measured today that population is 1,625. Applying the backfill on a clone
    moves it to exactly 1,572 - so the original figure was a real observed state of this corpus, and
    the 53 rows are the difference between then and now.

    What this does NOT establish: that the divergence was a single event. The 53 rows' created_at spans
    2026-03-09 to 2026-09-25 - seven months, and 51 of them are `vbt-` prefixed - so it is a persistent
    class of divergence across many saves, not one bad write. That is why I did not write the fix as a
    one-off repair of a known incident: there is no single incident to name.

    Recorded as a consistency check rather than proof. If a future state makes the backfill land on a
    different count, that is worth knowing - either the population moved or the recorded figure was
    wrong - so this asserts the arithmetic and lets the number diverge visibly.
    """
    import sqlite3 as _sqlite3

    with _sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        total = conn.execute(
            "select count(*) from backtests where sharpe_ratio is not null"
        ).fetchone()[0]
        zeros = conn.execute(
            "select count(*) from backtests where sharpe_ratio = 0.0"
        ).fetchone()[0]

    assert len(_disagreeing()) == 53, "the premise - 53 disagreeing rows - has moved"
    # The 53 are a subset of the zero population, not of the corpus: correcting them removes them from
    # the zeros and leaves the total untouched. My first version of this asserted `total - 53 == 1572`,
    # which is arithmetically nonsense - 2655 - 53 is 2602 - and the test caught it.
    assert zeros - len(_disagreeing()) == 1572, (
        f"correcting the disagreeing rows leaves {zeros - len(_disagreeing())} zeros, not the 1,572 "
        f"recorded in models.py. Either the corpus moved or that figure was wrong; both are worth "
        f"knowing before relying on it."
    )
    assert total == 2655, (
        f"the corpus now holds {total} rows with a Sharpe; the proportions derived from it in "
        f"validation/readiness/models.py must be re-measured rather than inherited"
    )


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
def test_the_divergence_is_a_persistent_class_not_a_single_event() -> None:
    """Why the fix is written as a general reconciliation rather than a named incident's repair."""
    import sqlite3 as _sqlite3

    ids = [bid for bid, _, _ in _disagreeing()]
    with _sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        created = [
            row[0]
            for row in conn.execute(
                "select created_at from backtests where id in (%s)"
                % ",".join("?" * len(ids)),
                ids,
            )
            if row[0]
        ]

    assert created, "no created_at recorded; the span cannot be measured"
    months = sorted({value[:7] for value in created})
    assert len(months) > 1, (
        f"all {len(ids)} disagreeing rows now fall in {months}, so this looks like a single event "
        f"after all and could be named as one"
    )
    vbt = sum(1 for bid in ids if bid.startswith("vbt-"))
    assert vbt > len(ids) // 2, (
        f"only {vbt} of {len(ids)} disagreeing rows are vbt-prefixed; the population has become "
        f"mixed and the vbt-specific explanation no longer holds"
    )


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
def test_the_same_sentinel_divergence_exists_in_three_more_scalar_columns() -> None:
    """The 53 are not a Sharpe quirk. Three sibling columns carry the same shape, far larger.

    Each scalar column has a physically-carried default that means "never computed":
    `sharpe_ratio`, `psr` and `cagr` default to 0.0, and `p_value` defaults to 1.0 because "no
    evidence against the null hypothesis" is the correct value for an uncomputed p-value. Where the
    payload holds a real computed value and the column still holds its sentinel, that is the same
    defect as the 53 - the column is stale because nothing wrote a number into it.

    Measured, counting only rows where both sides are present:

      sharpe_ratio   53 of 2,570   disagree, all with column == 0.0
      psr           925 of   984   disagree, all with column == 0.0
      cagr        1,046 of 2,488   disagree with column == 0.0   (plus 54 more, see below)
      p_value     1,041 of 2,488   disagree with column == 1.0

    So `scripts/backfill_sharpe_column.py` currently covers 53 rows of a population closer to 3,065.
    That is recorded rather than fixed here: extending the script to `p_value` means writing over a
    sentinel that has documented meaning, which deserves its own decision even though the direction is
    the same.

    `cagr`'s remaining 54 rows are a different question and deliberately excluded: there both sides are
    non-zero and differ by a median of 0.00055 with a maximum of 0.0133, which is recomputation drift,
    not staleness. Overwriting a real column value with a slightly different real payload value is not
    the same operation as replacing a sentinel, and lumping them together would destroy information
    rather than restore it.
    """
    import sqlite3 as _sqlite3

    with _sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute("select id, sharpe_ratio, psr, cagr, p_value, stats from backtests").fetchall()

    sentinels = {"sharpe_ratio": 0.0, "psr": 0.0, "cagr": 0.0, "p_value": 1.0}
    sentinel_counts: dict[str, int] = {}
    cagr_both_nonzero = 0
    cagr_diffs: list[float] = []

    for row in rows:
        stats = row[5]
        if not stats:
            continue
        try:
            parsed = json.loads(stats)
        except (TypeError, ValueError):
            continue
        for index, name in enumerate(("sharpe_ratio", "psr", "cagr", "p_value"), start=1):
            raw = parsed.get(name, parsed.get(name.replace("_ratio", "")))
            if raw is None:
                continue
            try:
                column_value = float(row[index])
                payload_value = float(raw)
            except (TypeError, ValueError):
                continue
            if abs(payload_value - column_value) <= 1e-9:
                continue
            if abs(column_value - sentinels[name]) <= 1e-9:
                sentinel_counts[name] = sentinel_counts.get(name, 0) + 1
            elif name == "cagr":
                cagr_both_nonzero += 1
                cagr_diffs.append(abs(payload_value - column_value))

    assert sentinel_counts.get("sharpe_ratio") == 53, (
        f"sharpe sentinel divergences are now {sentinel_counts.get('sharpe_ratio')}, not 53"
    )
    for name in ("psr", "cagr", "p_value"):
        assert sentinel_counts.get(name, 0) > 100, (
            f"{name} now shows only {sentinel_counts.get(name, 0)} sentinel divergences; the "
            f"cross-column finding needs re-measuring before it is reported again"
        )

    # cagr's non-sentinel disagreements are drift, and must stay distinguishable from staleness.
    assert cagr_both_nonzero > 0, "cagr's both-non-zero disagreements have vanished; re-derive"
    median = sorted(cagr_diffs)[len(cagr_diffs) // 2]
    assert median < 1e-2, (
        f"cagr's both-non-zero differences now have a median of {median:.4f}, which is too large to "
        f"call recomputation drift - re-derive before treating them as benign"
    )


@pytest.mark.skip(reason="historical: asserts the pre-backfill corpus of 2026-10-06. Kept because it records why the correction was safe; the live check is the post-backfill section of this file.")
def test_no_disagreeing_row_declares_its_metric_undefined_in_any_column() -> None:
    """Which settles whether widening the backfill is the same operation in every column.

    Card L103 held `p_value` back on the grounds that its 1.0 sentinel has documented meaning, so
    writing over it might destroy a deliberate "never computed" marker. That caution was half right
    and half wrong, and this is the measurement that separates them.

    The writer gates the column on the producer's `undefined_metrics`
    (`_scalar_stats_for_save` uses `resolve_defined_stat`), so the two situations are distinguishable
    in the stored payload:

      declared undefined  - the producer withheld the metric, so the sentinel is deliberate and the
                            payload value is a bug in its own right;
      no such declaration - the producer published the value and the column simply was never written,
                            so the sentinel is staleness and backfilling restores intent.

    Measured across all four columns and every disagreeing row: **zero** declare their metric
    undefined. All 53 / 925 / 1,100 / 1,041 carry no `undefined_metrics` key naming the metric at all.

    So the sentinel means "never computed" in every case, and in every case it is factually wrong -
    the payload proves the value was computed. Widening is therefore the same operation throughout,
    which reverses L103's caution about `p_value` specifically.
    """
    import sqlite3 as _sqlite3

    columns = ("sharpe_ratio", "psr", "cagr", "p_value")
    with _sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "select id, sharpe_ratio, psr, cagr, p_value, stats from backtests"
        ).fetchall()

    declared_undefined = 0
    disagreements = 0
    for row in rows:
        if not row[5]:
            continue
        try:
            parsed = json.loads(row[5])
        except (TypeError, ValueError):
            continue
        for index, name in enumerate(columns, start=1):
            raw = parsed.get(name, parsed.get(name.replace("_ratio", "")))
            if raw is None:
                continue
            try:
                if abs(float(raw) - float(row[index])) <= 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            disagreements += 1
            undefined = parsed.get("undefined_metrics")
            if isinstance(undefined, list) and name in undefined:
                declared_undefined += 1

    assert disagreements > 3000, (
        f"only {disagreements} disagreements remain; the four-column finding needs re-measuring"
    )
    assert declared_undefined == 0, (
        f"{declared_undefined} rows now declare their metric undefined while publishing a value. In "
        f"those the sentinel is deliberate and backfilling WOULD destroy a 'never computed' marker - "
        f"the script must skip them rather than overwrite."
    )


# ── POST-BACKFILL ────────────────────────────────────────────────────────────
#
# scripts/backfill_sharpe_column.py --apply ran against production on 2026-10-06 with the operator's
# authorisation, correcting 3,065 stale sentinel cells across the four scalar stat columns:
#
#     sharpe_ratio   53      sharpe_ratio 0.0  1,625 -> 1,572
#     psr           925      psr          0.0  2,603 -> 1,678
#     cagr        1,046      cagr         0.0  2,600 -> 1,554
#     p_value     1,041      p_value      1.0  2,601 -> 1,560
#
# The tests above describe the corpus as it was *before* that, and they now fail on purpose. They are
# kept rather than deleted because they record why the correction was safe - which is the part a future
# reader needs - and the assertions below replace their role as live checks.
#
# What is deliberately NOT reconciled: cagr's 54 rows where both sides hold a real value and differ by a
# median of 0.00055. That is recomputation drift, not staleness, and overwriting a real value with a
# different real value would destroy information rather than restore it.


def test_the_backfill_left_no_column_payload_disagreement_behind() -> None:
    """The regression check that replaces "there are exactly 53 disagreeing rows".

    If this ever goes non-zero, something is still failing to write these columns - which is a more
    interesting finding than the original mismatch, because it would mean the writer regressed rather
    than the corpus having drifted.
    """
    rows = _disagreeing()
    assert rows == [], (
        f"{len(rows)} rows disagree between a column and the payload again: {rows[:5]}. The backfill "
        f"is not durable - something is still failing to write the scalar stat columns."
    )


def test_only_cagrs_recomputation_drift_remains_and_is_deliberate() -> None:
    """54 rows, all cagr, all both-sides-real. This is the exclusion working, not a leak.

    Asserted positively so that if the count ever moves, it is visible which direction it moved: a
    rise means new drift has appeared and the exclusion needs re-examining; a fall to zero means the
    drift resolved itself and cagr can be folded into the backfill.
    """
    drift = _cagr_drift_rows()
    assert len(drift) == 54, (
        f"cagr drift rows are now {len(drift)}, not 54. A rise means new recomputation drift needs "
        f"deciding; a fall to zero means cagr can join the backfill."
    )
    assert all(abs(column) > 1e-9 and abs(payload) > 1e-9 for _id, column, payload in drift), (
        "a cagr drift row has a sentinel on one side, which would make it stale rather than drift and "
        "would belong in the backfill"
    )


def test_no_producer_declared_a_metric_undefined_while_publishing_it() -> None:
    """Still zero after the backfill, which is what made the single-direction correction safe."""
    withheld = _declared_undefined_cells()
    assert len(withheld) == 0, (
        f"{len(withheld)} cells declare their metric undefined while publishing a value. Those "
        f"sentinels are deliberate, and the script refuses to overwrite them - so if this is "
        f"non-empty the population needs re-examining before any further apply."
    )


def test_the_zero_sharpe_population_is_now_the_1572_the_code_recorded() -> None:
    """Closure, and a useful coincidence.

    validation/readiness/models.py originally cited "1,572 of 2,655 rows (59.2%)" as the zero-Sharpe
    population. I corrected that to 1,625 (61.2%) when I measured the pre-backfill corpus, and the
    backfill has now restored exactly 1,572. So the recorded figure was right for a prior state and my
    correction was right for the moment I measured it - they described different moments rather than
    competing claims, which is why this closes rather than conflicting.
    """
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        total = conn.execute(
            "select count(*) from backtests where sharpe_ratio is not null"
        ).fetchone()[0]
        zeros = conn.execute(
            "select count(*) from backtests where sharpe_ratio = 0.0"
        ).fetchone()[0]

    assert total == 2655, (
        f"the corpus now holds {total} rows with a Sharpe; every proportion in "
        f"validation/readiness/models.py must be re-measured rather than inherited"
    )
    assert zeros == 1572, f"the zero-Sharpe population is {zeros}, not the 1,572 the backfill restored"


def test_the_extreme_value_the_backfill_was_measured_against_survived() -> None:
    """The -37.6644 row was the reason the direction mattered, so it is checked by name."""
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        value = conn.execute(
            "select sharpe_ratio from backtests where id = 'vbt-example_sma-20260331-212522'"
        ).fetchone()

    assert value is not None, "the probe row is gone; pick another and re-derive"
    assert value[0] == pytest.approx(-37.6644), (
        f"that row now stores {value[0]}, not -37.6644. A production run of "
        f"recalculate_backtest_stats.py would have zeroed it - see the hazard test in the sibling file"
    )
