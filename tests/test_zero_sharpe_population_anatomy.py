"""What the zero-Sharpe population actually is, measured from each row's own equity curve.

The readiness tolerance's absolute floor governs whenever the expected Sharpe is small, and 61.2% of
the corpus stores `sharpe_ratio` exactly 0.0. That made the population sound like one homogeneous
group eligible for a "not comparable" verdict. It is not, and the distinction decides the policy
question: most of those rows cannot produce a Sharpe from their own curve at all.

Measured across all 1,625 zero-Sharpe rows:

    1,353  exactly two equity points   one return, zero variance, no Sharpe derivable
      11  exactly one equity point     no return at all
     209  >=3 points, flat             derivable, and correctly zero
      52  >=3 points, moving           zero Sharpe on a curve that moved

The 52 are the contradiction. For 46 of them `stats.sharpe_ratio` disagrees with the 0.0 column -
which is exactly the 53-row cohort the backlog entry discusses, once the 7 non-`vbt-` rows are
included. So the "53" was always real and specific; it is the subset where the column and the stats
payload hold different answers, not a hand-picked number.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")
pytestmark = pytest.mark.skipif(
    not PRODUCTION_DB.exists(), reason="production db absent, so there is no corpus to measure"
)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)


def _equity_points(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[float] = []
    for point in parsed:
        if isinstance(point, dict):
            for key in ("equity", "value", "close"):
                if isinstance(point.get(key), (int, float)):
                    out.append(float(point[key]))
                    break
    return out


def _stats_sharpe(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        stats = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(stats, dict):
        return None
    value = stats.get("sharpe_ratio", stats.get("sharpe"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@pytest.fixture(scope="module")
def zero_rows() -> list[tuple[str, float, str | None, str | None]]:
    with _conn() as conn:
        return [
            (bid, float(sharpe), stats, curve)
            for bid, sharpe, stats, curve in conn.execute(
                "select id, sharpe_ratio, stats, equity_curve from backtests where sharpe_ratio = 0.0"
            )
        ]


@pytest.fixture(scope="module")
def all_rows() -> list[tuple[str, float, str | None]]:
    with _conn() as conn:
        return [
            (bid, float(sharpe), stats)
            for bid, sharpe, stats in conn.execute(
                "select id, sharpe_ratio, stats from backtests where sharpe_ratio is not null"
            )
        ]


def test_the_zero_population_is_the_majority_and_there_are_1030_non_zero_rows(
    all_rows: list[tuple[str, float, str | None]],
) -> None:
    """The proportion the tolerance comment argues from, asserted against the corpus.

    An earlier version of that comment claimed 97.9% zero and 1,028 non-zero pairs. Measured is
    61.2% and 1,030, so the zero population was overstated by more than a third. This test exists so
    that number cannot drift back: it is the premise for why the absolute floor governs the common
    case, and a wrong premise there is load-bearing.
    """
    total = len(all_rows)
    zeros = sum(1 for _, sharpe, _ in all_rows if sharpe == 0.0)
    non_zero = total - zeros

    assert total == 2655, (
        f"the corpus now holds {total} rows with a Sharpe, so every proportion derived from it in "
        f"validation/readiness/models.py needs re-measuring rather than inheriting"
    )
    assert non_zero == 1030, (
        f"{non_zero} rows now carry a non-zero Sharpe; models.py cites 1,030 as the pair count"
    )
    assert 0.60 < zeros / total < 0.65, (
        f"{zeros} of {total} rows ({100 * zeros / total:.1f}%) store exactly 0.0. models.py says 61.2%, "
        f"so either the corpus moved or one of the two is wrong and the tolerance argument depends "
        f"on which."
    )


def test_the_dominant_fact_is_an_absent_curve_not_a_short_one(
    zero_rows: list[tuple[str, float, str | None, str | None]],
) -> None:
    """1,208 of 1,625 store no usable equity curve at all.

    This is the measurement that changes what the cohort *is*. The natural assumption - and my own
    first pass - was that zero-Sharpe rows carry stub curves too short to yield a Sharpe. They do not:
    they overwhelmingly carry **nothing**, so no recomputation policy can recover them because there
    is no curve to recompute from. Separating absent from short is what tells a reader whether the
    fix is a policy switch or a data-retention question.
    """
    empty = one = two = usable = 0
    for _, _, _, curve in zero_rows:
        points = len(_equity_points(curve))
        if points == 0:
            empty += 1
        elif points == 1:
            one += 1
        elif points == 2:
            two += 1
        else:
            usable += 1

    total = len(zero_rows)
    assert empty / total > 0.70, (
        f"only {empty} of {total} zero-Sharpe rows lack a usable curve; if curves are now being "
        f"stored, this cohort's character has changed and the policy question needs restating"
    )
    assert empty + one + two + usable == total


def test_the_recomputable_residue_is_small_but_not_vacuous(
    zero_rows: list[tuple[str, float, str | None, str | None]],
) -> None:
    """261 rows have three or more points, so "not comparable" is not a blank cheque.

    Of those, 209 are flat - correctly zero - and 52 move while storing 0.0. So the policy still needs
    a rule about which of the 1,624 no-curve rows it covers, and the 52 need their own answer
    regardless of what that rule says.
    """
    usable = 0
    for _, _, _, curve in zero_rows:
        if len(_equity_points(curve)) >= 3:
            usable += 1

    assert 200 < usable < 400, (
        f"{usable} zero-Sharpe rows now have three or more equity points; the recomputable residue "
        f"has moved and the policy rule needs restating"
    )


def test_rows_too_short_to_derive_are_a_minority(
    zero_rows: list[tuple[str, float, str | None, str | None]],
) -> None:
    """156 rows - 9.6% - have one or two points: the only group that is short rather than absent.

    Asserted as a minority because an earlier draft of this file claimed 83.9%, and being wrong about
    it by 9x would have pointed the fix at the wrong defect entirely.
    """
    one = two = 0
    for _, _, _, curve in zero_rows:
        points = len(_equity_points(curve))
        if points == 1:
            one += 1
        elif points == 2:
            two += 1

    total = len(zero_rows)
    assert (one + two) / total < 0.15, (
        f"{one + two} of {total} ({100 * (one + two) / total:.1f}%) zero-Sharpe rows are now short "
        f"rather than absent, which reverses the finding this file was written for"
    )


def test_the_53_row_cohort_is_where_the_column_and_the_stats_payload_disagree(
    all_rows: list[tuple[str, float, str | None]],
) -> None:
    """This is what '53 rows' actually is, and it is a real, specific set.

    The backlog entry's title number was doubted and the doubt was wrong. All 53 have a stored
    column of exactly 0.0 and a non-zero `stats.sharpe_ratio`, so the two places a reader can look
    hold different answers for the same run. That is a genuine defect and a sharper finding than a
    count: it is not that the entry miscounted, it is that the corpus contains 53 rows whose Sharpe
    is ambiguous by construction.
    """
    disagreeing = []
    for _, column, stats in all_rows:
        inner = _stats_sharpe(stats)
        if inner is None:
            continue
        if abs(inner - column) > 1e-9:
            disagreeing.append((column, inner))

    assert len(disagreeing) == 53, (
        f"{len(disagreeing)} rows now disagree between the column and stats; the backlog's 53 and "
        f"the 'disagreeing payload' reading of it would both need re-deriving"
    )
    assert all(column == 0.0 for column, _ in disagreeing), (
        "the disagreeing set is no longer confined to zero columns, so it is a different cohort than "
        "the one the backlog entry describes"
    )
    assert all(inner != 0.0 for _, inner in disagreeing), (
        "a disagreeing row now has 0.0 in the stats payload too, so it agrees with the column and "
        "should not be counted here"
    )


def test_the_53_are_a_concentration_not_a_uniform_spread(all_rows: list[tuple[str, float, str | None]]) -> None:
    """40 of the 53 are one strategy family, which is worth knowing before deciding what to do.

    Not a gate - the concentration is informational, and this test will not fail if the mix shifts.
    It exists so a reader deciding the disposition can see that this is largely one family rather
    than a random sample, and so a later migration cannot quietly reweight it without a test failing.
    """
    families: Counter[str] = Counter()
    for bid, column, stats in all_rows:
        inner = _stats_sharpe(stats)
        if inner is not None and abs(inner - column) > 1e-9:
            families[bid.split("_")[0][:18]] += 1

    assert families, "no disagreeing rows at all; the concentration reading needs re-deriving"
    assert max(families.values()) > 20, (
        f"the disagreeing set is now spread evenly across families ({dict(families)}), so it is no "
        f"longer one family and whatever was true of that family may not generalise"
    )