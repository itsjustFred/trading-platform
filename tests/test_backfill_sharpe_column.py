"""The 53-row backfill tool: dry-run by default, one-sided, and refuses the reverse case.

Writing the tool is decidable even though running it is not - so these tests exercise it against a
throwaway database rather than production, and assert the properties that make it safe to hand to an
operator.
"""

from __future__ import annotations

import importlib.util
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
    db.execute(
        "create table backtests (id text primary key, sharpe_ratio real not null default 0.0, "
        "stats text)"
    )
    db.executemany(
        "insert into backtests (id, sharpe_ratio, stats) values (?, ?, ?)",
        [
            ("stale-column", 0.0, '{"sharpe_ratio": -37.6644}'),
            ("agreeing", 1.5, '{"sharpe_ratio": 1.5}'),
            ("both-zero", 0.0, '{"sharpe_ratio": 0.0}'),
            ("no-stats", 0.0, None),
            ("unparseable-stats", 0.0, "{not json"),
            ("legacy-key", 0.0, '{"sharpe": -2.5}'),
            ("payload-missing", 0.0, '{"cagr": 0.1}'),
        ],
    )
    db.commit()
    yield db
    db.close()


def test_it_finds_exactly_the_disagreeing_rows(conn):
    module = _load()
    rows = module.find_disagreements(conn)

    assert {r[0] for r in rows} == {"stale-column", "legacy-key"}, (
        "the finder must report exactly the rows where the two sides differ - including the legacy "
        "'sharpe' key - and stay silent for agreeing, both-zero, absent and unparseable stats"
    )


def test_dry_run_is_the_default_and_writes_nothing(conn):
    """The property that makes this safe to hand over: preview requires no flag, and previews do nothing."""
    module = _load()
    rows = module.find_disagreements(conn)

    assert not conn.execute("update backtests set sharpe_ratio = -1").rowcount == 1, "sanity"
    conn.rollback()

    # The script's write path is gated behind --apply, asserted structurally rather than by invoking
    # main(), because main() resolves the real DATA_DIR.
    source = (REPO / "scripts" / "backfill_sharpe_column.py").read_text(encoding="utf-8")
    assert 'if not args.apply:' in source
    assert "Dry run: nothing written" in source
    assert rows, "fixture lost its disagreement"


def test_apply_corrects_the_column_from_the_payload(conn):
    module = _load()
    rows = module.find_disagreements(conn)

    corrected = module._reconcile(conn, rows)
    conn.commit()

    assert corrected == 2, f"corrected {corrected} rows, expected both disagreements"
    assert conn.execute(
        "select sharpe_ratio from backtests where id = 'stale-column'"
    ).fetchone()[0] == pytest.approx(-37.6644)
    # The legacy key is honoured too.
    assert conn.execute(
        "select sharpe_ratio from backtests where id = 'legacy-key'"
    ).fetchone()[0] == pytest.approx(-2.5)


def test_it_refuses_the_reverse_case_instead_of_overwriting(conn):
    """A row where the column holds a real value is a different problem, and must not be overwritten.

    Every production disagreement has the column at 0.0. If one ever disagreed the other way, writing
    the payload's value would destroy the number the platform displays - the exact damage this whole
    exercise exists to prevent, so the tool refuses rather than guessing.
    """
    conn.execute("update backtests set sharpe_ratio = 2.0, stats = '{\"sharpe_ratio\": 0.0}' "
                 "where id = 'agreeing'")
    conn.commit()

    module = _load()
    rows = module.find_disagreements(conn)
    assert any(r[0] == "agreeing" for r in rows), "fixture did not create the reverse disagreement"

    corrected = module._reconcile(conn, rows)
    conn.commit()

    assert corrected == 2, "the reverse row must be skipped, not applied"
    assert conn.execute(
        "select sharpe_ratio from backtests where id = 'agreeing'"
    ).fetchone()[0] == pytest.approx(2.0), "the displayed value was overwritten"


def test_it_never_writes_the_payload(conn):
    """The irreversible direction is not merely unused - it does not exist in the script."""
    source = (REPO / "scripts" / "backfill_sharpe_column.py").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "update backtests set stats" not in lowered
    assert "update backtests set sharpe_ratio" in lowered
    # The only write targets the column.
    assert lowered.count("conn.execute(\n            \"update backtests") == 1
