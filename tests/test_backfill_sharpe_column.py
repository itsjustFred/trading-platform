"""The 53-row backfill tool: dry-run by default, one-sided, and refuses the reverse case.

Writing the tool is decidable even though running it is not - so these tests exercise it against a
throwaway database rather than production, and assert the properties that make it safe to hand to an
operator.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    path = REPO / "scripts" / "backfill_sharpe_column.py"
    spec = importlib.util.spec_from_file_location("backfill_sharpe_column_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "probe.db")
    # The four scalar stat columns with the same sentinels production carries: 0.0 for sharpe_ratio,
    # psr and cagr, and 1.0 for p_value, whose 1.0 is a deliberate "never computed" value.
    db.execute(
        "create table backtests (id text primary key, sharpe_ratio real not null default 0.0, "
        "psr real not null default 0.0, cagr real not null default 0.0, "
        "p_value real not null default 1.0, stats text)"
    )
    db.executemany(
        "insert into backtests (id, sharpe_ratio, psr, cagr, p_value, stats) "
        "values (?, ?, ?, ?, ?, ?)",
        [
            ("stale-column", 0.0, 0.0, 0.0, 1.0, '{"sharpe_ratio": -37.6644}'),
            ("agreeing", 1.5, 0.0, 0.0, 1.0, '{"sharpe_ratio": 1.5}'),
            ("both-zero", 0.0, 0.0, 0.0, 1.0, '{"sharpe_ratio": 0.0}'),
            ("no-stats", 0.0, 0.0, 0.0, 1.0, None),
            ("unparseable-stats", 0.0, 0.0, 0.0, 1.0, "{not json"),
            ("legacy-key", 0.0, 0.0, 0.0, 1.0, '{"sharpe": -2.5}'),
            ("payload-missing", 0.0, 0.0, 0.0, 1.0, '{"total_trades": 3}'),
        ],
    )
    db.commit()
    yield db
    db.close()


def test_it_finds_exactly_the_disagreeing_rows(conn):
    """Only stale sentinels. The agreeing, absent, unparseable and both-zero rows stay out."""
    module = _load()
    rows = module.find_disagreements(conn)

    found = {(r[0], r[1]) for r in rows}
    assert ("stale-column", "sharpe_ratio") in found
    assert ("legacy-key", "sharpe_ratio") in found, "the legacy 'sharpe' key must be honoured"
    assert ("agreeing", "sharpe_ratio") not in found, "an agreeing row is not a disagreement"
    assert ("both-zero", "sharpe_ratio") not in found
    assert ("no-stats", "sharpe_ratio") not in found
    assert ("unparseable-stats", "sharpe_ratio") not in found
    assert ("payload-missing", "sharpe_ratio") not in found


def test_each_column_is_reconciled_independently(conn):
    """A row can be stale in one column and healthy in another, so cells are the unit, not rows."""
    conn.execute(
        "update backtests set sharpe_ratio = 2.0, psr = 0.0, cagr = 0.0, p_value = 1.0, "
        "stats = '{\"sharpe_ratio\": 2.0, \"psr\": 0.33, \"cagr\": -0.67, \"p_value\": 0.75}' "
        "where id = 'agreeing'"
    )
    conn.commit()

    module = _load()
    rows = module.find_disagreements(conn)
    columns = {column for _id, column, _c, _p in rows if _id == "agreeing"}

    assert columns == {"psr", "cagr", "p_value"}, (
        f"expected the three sentinel columns, got {sorted(columns)}"
    )
    assert "sharpe_ratio" not in columns, "sharpe_ratio agrees at 2.0 and must not be touched"

    module._reconcile(conn, rows)
    conn.commit()
    values = dict(
        conn.execute("select id, psr from backtests where id = 'agreeing'").fetchall()
    )
    assert values["agreeing"] == pytest.approx(0.33)


def test_p_value_uses_its_own_sentinel_of_one_not_zero(conn):
    """p_value defaults to 1.0, not 0.0 - a 0.0 column there is a real value and must be left alone."""
    conn.execute("update backtests set p_value = 0.0, stats = '{\"p_value\": 0.75}' where id = 'both-zero'")
    conn.commit()

    module = _load()
    rows = module.find_disagreements(conn)
    assert not any(column == "p_value" for _id, column, _c, _p in rows), (
        "a p_value column of 0.0 was treated as a sentinel. It is not - p_value's sentinel is 1.0, so "
        "0.0 is a real value and reconciling it would overwrite real data"
    )


def test_a_column_holding_a_real_value_is_never_reconciled(conn):
    """Recomputation drift is not staleness, and reconciling it would destroy information."""
    conn.execute(
        "insert into backtests (id, sharpe_ratio, psr, cagr, p_value, stats) "
        "values ('drift', 0.0, 0.0, 0.1803, 1.0, '{\"cagr\": 0.1786}')"
    )
    conn.commit()

    module = _load()
    rows = module.find_disagreements(conn)
    assert not any(r[0] == "drift" for r in rows), (
        "a row whose cagr column holds a real 0.1803 against a payload 0.1786 must not be reported - "
        "that is recomputation drift, and overwriting a real value with a different real value "
        "destroys information rather than restoring it"
    )


def test_declared_undefined_rows_are_reported_and_never_applied(conn):
    """A sentinel the producer deliberately set is a real marker, not staleness."""
    conn.execute(
        "insert into backtests (id, sharpe_ratio, psr, cagr, p_value, stats) "
        "values ('withheld', 0.0, 0.0, 0.0, 1.0, "
        "'{\"sharpe_ratio\": -5.0, \"undefined_metrics\": [\"sharpe_ratio\"]}')"
    )
    conn.commit()

    module = _load()
    withheld = module.find_declared_undefined(conn)
    assert ("withheld", "sharpe_ratio") in withheld, (
        "a producer-declared undefined metric must be reported so the script can refuse to overwrite it"
    )
    rows = module.find_disagreements(conn)
    module._reconcile(conn, rows)
    conn.commit()

    value = conn.execute("select sharpe_ratio from backtests where id = 'withheld'").fetchone()[0]
    assert value == 0.0, "the deliberate 'never computed' marker was overwritten"


def test_the_corpus_has_no_declared_undefined_sentinels_today():
    """Zero today, measured. This is the check that makes the refusal path worth keeping."""
    import sqlite3 as _sqlite3

    db = Path("/home/ubuntu/data/backtests.db")
    if not db.exists():
        pytest.skip(f"{db} is absent, so there is no corpus to inspect")
    with _sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        rows = connection.execute("select sharpe_ratio, psr, cagr, p_value, stats from backtests").fetchall()

    withheld = 0
    for *values, stats in rows:
        if not stats:
            continue
        try:
            payload = json.loads(stats)
        except (TypeError, ValueError):
            continue
        undefined = payload.get("undefined_metrics")
        if not isinstance(undefined, list):
            continue
        for column, value in zip(("sharpe_ratio", "psr", "cagr", "p_value"), values):
            if column in undefined and payload.get(column) is not None:
                withheld += 1
    assert withheld == 0, (
        f"{withheld} cells now declare their metric undefined while publishing a value. The refusal "
        f"path in this script is no longer hypothetical and the operator must be told before any apply."
    )


def test_dry_run_is_the_default_and_the_write_targets_columns_only():
    source = (REPO / "scripts" / "backfill_sharpe_column.py").read_text(encoding="utf-8")
    assert "if not args.apply:" in source
    assert "Dry run: nothing written" in source
    lowered = source.lower()
    assert "update backtests set stats" not in lowered, "the payload must never be written"
    assert "update backtests set {column}" in lowered


def test_production_corpus_is_what_the_script_reports():
    """The headline number, asserted so a corpus change surfaces instead of silently altering scope.

    Opt-in and read-only. The live database is not part of this repository and a test run must not
    open it - and a hard-coded home path would not survive Windows or macOS either. Point
    `TRADING_PRODUCTION_DB` at a copy (or at production, read-only) to run it; the numbers below are
    the snapshot the script was written against, so a difference means the corpus moved and the
    figures need reconciling rather than the assertion loosening.
    """
    import os
    import sqlite3 as _sqlite3

    configured = os.environ.get("TRADING_PRODUCTION_DB")
    if not configured:
        pytest.skip("set TRADING_PRODUCTION_DB to a production copy to check the corpus")
    db = Path(configured)
    if not db.is_file():
        pytest.skip(f"{db} is absent")
    with _sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        found = _load().find_disagreements(connection)
    counts = {}
    for _id, column, _c, _p in found:
        counts[column] = counts.get(column, 0) + 1
    assert len(found) == 3065, f"the script now reports {len(found)} stale cells, not 3065"
    assert counts.get("sharpe_ratio") == 53
    assert counts.get("cagr") == 1046, (
        "cagr's sentinel count moved; note that its 54 drift rows are excluded by design, so this "
        "figure should be the sentinel population only"
    )


def test_the_irreversible_direction_does_not_exist_in_the_script():
    source = (REPO / "scripts" / "backfill_sharpe_column.py").read_text(encoding="utf-8")
    assert "update backtests set stats" not in source.lower()
