"""A migration that resolved nothing must say so, rather than reporting success.

`_backfill_backtest_equity_curves` selects every backtest whose `equity_curve` is empty, tries
`equity_points` and then a legacy JSON artifact, and moves on when neither produces anything. It
kept **no count at all**, so these two outcomes were indistinguishable:

- every row already had a curve, so there was nothing to do;
- every row was in scope and **none** of them could be resolved, so nothing was written.

On production the second one is what happens. Measured read-only:

- **1,211** rows have `equity_curve = '[]'`, so all of them are in scope;
- **0** of them have matching `equity_points` rows;
- the artifact fallback reads `DATA_DIR/backtests/{{id}}.trades.json`, and this repo's `DATA_DIR`
  holds **0** such artifacts against **2,377** under the production data dir.

So 1,211 rows go in, nothing comes out, and the migration exits cleanly. An operator reading that
concludes the backfill ran. This is the failure this repo keeps auditing for - a report that cannot
tell "checked and fine" from "did not manage to check" - one level down, in a migration.

Logged rather than raised, deliberately: a deployment whose artifacts genuinely live elsewhere
should not have every process fail to start, and the counts distinguish the two situations for
a reader. What was unacceptable was the silence, not the outcome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

import shared.database as database


def _seed_empty_curves(ids: list[str]) -> None:
    with database.get_engine().connect() as connection:
        connection.execute(
            database.text(
                "INSERT INTO backtests (id, strategies, engine, backtest_start, backtest_end, "
                "data, initial_capital, parameters, tags, stats, notes, created_at, "
                "equity_curve) VALUES (:id, '[]', 'native', '', '', '{}', 100000, '{}', "
                "'[]', '{}', '', '', '[]')"
            ),
            [{"id": name} for name in ids],
        )
        connection.commit()


def _seed_equity_points(backtest_id: str) -> None:
    """Give one backtest a curve via the source the backfill tries first."""
    with database.get_engine().connect() as connection:
        connection.execute(
            database.text(
                "INSERT INTO equity_points (backtest_id, time, equity) VALUES (:b, 1.0, 1000.0)"
            ),
            {"b": backtest_id},
        )
        connection.commit()


def _run_backfill() -> None:
    with Session(database.get_engine()) as session:
        database._backfill_backtest_equity_curves(session.connection())


def _messages(caplog: pytest.LogCaptureFixture, level: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == level]


def test_a_backfill_that_resolves_nothing_says_so(
    isolated_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect, behaviourally: rows in scope, nothing resolved, and now it is reported."""
    _seed_empty_curves(["empty-0", "empty-1", "empty-2"])
    with caplog.at_level("WARNING", logger="database"):
        _run_backfill()

    warnings = _messages(caplog, "WARNING")
    assert warnings, (
        "the backfill resolved nothing and said nothing; an operator would conclude it ran"
    )
    assert "resolved nothing" in warnings[-1], warnings[-1]
    assert "3" in warnings[-1], (
        f"the report does not say how many rows were in scope: {warnings[-1]}"
    )


def test_a_backfill_with_nothing_in_scope_stays_quiet(
    isolated_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """**The other direction, and why this is not just "always warn".**

    A warning on every clean start trains the reader to skip it, which is the cry-wolf failure the
    resource gate's threshold tests exist to prevent. With no rows in scope there is nothing to
    report.
    """
    with caplog.at_level("WARNING", logger="database"):
        _run_backfill()
    assert not _messages(caplog, "WARNING"), (
        "the backfill warned with no rows in scope; a warning that always fires is one nobody reads"
    )


def test_a_partial_fill_reports_progress_rather_than_nothing(
    isolated_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Some rows resolved is a third case, and neither message fits it.

    Without this, "3 of 4 filled" would be reported as either silence or total failure, and both
    readings are wrong.

    My first version of this test set one row's `equity_curve` directly - so the backfill itself
    filled **nothing** and correctly warned. The row has to be fillable *by the backfill*, which
    means seeding `equity_points`, the source it tries first.
    """
    _seed_empty_curves(["empty-0", "empty-1", "empty-2", "empty-3"])
    _seed_equity_points("empty-0")
    with caplog.at_level("INFO", logger="database"):
        _run_backfill()

    infos = _messages(caplog, "INFO")
    assert any("filled" in m for m in infos), infos
    assert not _messages(caplog, "WARNING"), (
        "a backfill that filled some rows also claimed to have resolved nothing"
    )


def test_the_report_names_the_directory_it_looked_in(
    isolated_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning has to be actionable, and "1,211 rows unchanged" is not.

    The whole diagnosis is that the artifact directory it consults is the wrong one, so the message
    must say which directory that was - otherwise the reader has to re-derive it.
    """
    _seed_empty_curves(["empty-0", "empty-1"])
    with caplog.at_level("WARNING", logger="database"):
        _run_backfill()

    message = _messages(caplog, "WARNING")[-1]
    expected = str(Path(database.DATA_DIR) / "backtests")
    assert expected in message, (
        f"the warning does not name the artifact directory it consulted ({expected}): {message}"
    )
