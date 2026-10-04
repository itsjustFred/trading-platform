"""Safety guards for the destructive ops scripts (Cleanup 20-C).

Both scripts destroy large portions of the research corpus, so they must be
dry-run by default, require an explicit confirmation, keep a restorable backup
and swap the new state in atomically.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.lib
import pytest
from sqlmodel import select

import shared.database as database
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_db(monkeypatch, tmp_path: Path):
    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    yield database
    engine.dispose()


def _result(backtest_id: str) -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id=backtest_id,
            created_at=datetime.now(UTC).isoformat(),
            engine="vectorbt",
            strategies=["demo_strategy"],
            backtest_start="2024-01-01",
            backtest_end="2024-02-01",
            data={"symbols": ["EURUSD"], "timeframes": ["1h"], "source": "demo"},
            initial_capital=100_000.0,
            parameters={},
            tags=[],
            notes="",
        ),
        stats={"total_trades": 1},
        trades=[
            Trade(
                id=1,
                symbol="EURUSD",
                direction="LONG",
                entry_time="2024-01-02T00:00:00Z",
                entry_price=1.1,
                quantity=1.0,
                pnl=10.0,
                exits=[
                    TradeExit(
                        time="2024-01-03T00:00:00Z",
                        price=1.2,
                        quantity=1.0,
                        pnl=10.0,
                    )
                ],
            )
        ],
        equity_curve=[],
    )


def _seed_corpus(database, tmp_path: Path) -> Path:
    database.init_db()
    database.save_result_to_sqlite(_result("bt-seed"))
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    (backtests_dir / "bt-seed.trades.json").write_text(
        json.dumps(_result("bt-seed").to_dict()), encoding="utf-8"
    )
    return backtests_dir


def _parquet_frame(start_hour: int, rows: int) -> pd.DataFrame:
    stamps = pd.date_range("2024-01-01", periods=rows, freq="1h", tz="UTC") + pd.Timedelta(
        hours=start_hour
    )
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": 100,
        }
    )


@pytest.fixture
def ohlcv_root(tmp_path: Path) -> Path:
    root = tmp_path / "ohlcv" / "EURUSD"
    root.mkdir(parents=True)
    # Disjoint timestamps so the deduped merge of both fragments is 5 rows.
    frame = _parquet_frame(0, 3)
    frame.to_parquet(root / "1h.parquet", index=False)
    frame = _parquet_frame(10, 2)
    frame.to_parquet(root / "1h_part2.parquet", index=False)
    return tmp_path / "ohlcv"


# ─── scripts/consolidate_parquets.py ─────────────────────────────────────────


def test_consolidate_dry_run_leaves_tree_untouched(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    before = sorted((p.relative_to(ohlcv_root), p.stat().st_size) for p in ohlcv_root.rglob("*"))

    merged, deleted, _backup = consolidate(ohlcv_root, dry_run=True)

    after = sorted((p.relative_to(ohlcv_root), p.stat().st_size) for p in ohlcv_root.rglob("*"))
    assert (merged, deleted) == (0, 0)
    assert before == after


def test_consolidate_keeps_backup_dir_outside_tree(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    backup_dir = ohlcv_root.parent / "backup"
    merged, deleted, backup = consolidate(ohlcv_root, backup_dir=backup_dir)

    assert (merged, deleted) == (1, 1)
    assert backup is not None
    assert backup.is_dir()
    assert backup_dir not in ohlcv_root.rglob("*")
    assert backup != ohlcv_root
    restored = {p.name for p in backup.rglob("*.parquet")}
    assert "1h.parquet" in restored
    assert "1h_part2.parquet" in restored
    assert not list(ohlcv_root.rglob("*.backup"))


def test_consolidate_is_reversible_from_backup(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    snapshot = sorted(
        (p.relative_to(ohlcv_root), p.read_bytes()) for p in ohlcv_root.rglob("*.parquet")
    )
    _, _, backup = consolidate(ohlcv_root, backup_dir=ohlcv_root.parent / "backup")

    for item in ohlcv_root.rglob("*"):
        if item.is_file():
            item.unlink()

    from scripts.consolidate_parquets import restore_backup

    restore_backup(backup, ohlcv_root)
    restored = sorted(
        (p.relative_to(ohlcv_root), p.read_bytes()) for p in ohlcv_root.rglob("*.parquet")
    )
    assert restored == snapshot


def test_consolidate_unreadable_fragment_aborts_before_deleting_fragments(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    (ohlcv_root / "EURUSD" / "broken_part3.parquet").write_bytes(b"not a parquet file")

    # Captured before the run, so the post-failure comparison is not tautological.
    snapshot = sorted(
        (p.relative_to(ohlcv_root), p.read_bytes())
        for p in ohlcv_root.rglob("*.parquet")
        if p.name != "broken_part3.parquet"
    )

    with pytest.raises(pyarrow.lib.ArrowInvalid):
        consolidate(ohlcv_root, backup_dir=ohlcv_root.parent / "backup")

    # The failed run leaves the tree byte-identical to before: no fragment is
    # deleted, and any canonical file an earlier merge already replaced is
    # restored from the snapshot.
    assert (
        sorted(
            (p.relative_to(ohlcv_root), p.read_bytes())
            for p in ohlcv_root.rglob("*.parquet")
            if p.name != "broken_part3.parquet"
        )
        == snapshot
    )
    names = sorted(p.name for p in ohlcv_root.rglob("*.parquet"))
    assert "1h.parquet" in names
    assert "1h_part2.parquet" in names
    assert "broken_part3.parquet" in names


def test_consolidate_confirmed_run_merges_fragments(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    merged, deleted, _ = consolidate(ohlcv_root, backup_dir=ohlcv_root.parent / "backup")

    assert (merged, deleted) == (1, 1)
    merged_frame = pd.read_parquet(ohlcv_root / "EURUSD" / "1h.parquet")
    assert list(merged_frame["timestamp"]) == list(merged_frame["timestamp"].sort_values())
    # 3 rows from 1h.parquet + 2 non-overlapping rows from 1h_part2.parquet.
    assert len(merged_frame) == 5


def test_consolidate_refuses_backup_dir_inside_tree(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    inside = ohlcv_root / "EURUSD" / "backup"

    with pytest.raises(ValueError, match="outside the OHLCV tree"):
        consolidate(ohlcv_root, backup_dir=inside)

    assert not inside.exists()
    assert (ohlcv_root / "EURUSD" / "1h.parquet").exists()


# ─── scripts/regenerate_backtests.py ─────────────────────────────────────────


def test_existing_db_path_prefers_absolute_engine_url(monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    engine = database.create_engine(f"sqlite:///{(tmp_path / 'engine.db').as_posix()}")
    monkeypatch.setattr(database, "ENGINE", engine)

    assert script._existing_db_path() == tmp_path / "engine.db"
    engine.dispose()


def test_sqlite_backup_uses_online_backup_api(tmp_path, monkeypatch):
    from scripts import regenerate_backtests as script

    db_path = tmp_path / "backtests.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE backtests (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO backtests (id) VALUES ('bt-seed')")

    backed_up_from: list[Path] = []
    real_connect = sqlite3.connect

    class _TrackingConnection:
        """Wrap a real connection so ``backup`` calls can be observed."""

        def __init__(self, path: Path):
            self._connection = real_connect(path)

        def backup(self, target, **kwargs):
            backed_up_from.append(db_path)
            return self._connection.backup(target, **kwargs)

        def execute(self, *args, **kwargs):
            return self._connection.execute(*args, **kwargs)

        def commit(self):
            return self._connection.commit()

        def close(self):
            return self._connection.close()

    def tracking_connect(path, *args, **kwargs):
        if Path(path) == db_path and not args and not kwargs:
            return _TrackingConnection(Path(path))
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(script.sqlite3, "connect", tracking_connect)

    destination = script._dump_sqlite_backup(db_path, backup_dir)

    assert destination == backup_dir / "backtests.db"
    assert backed_up_from == [db_path], "snapshot must use the sqlite online backup API"
    with sqlite3.connect(destination) as restored:
        rows = restored.execute("SELECT id FROM backtests").fetchall()
    assert rows == [("bt-seed",)]


def test_sqlite_backup_survives_persisted_wal_content(tmp_path):
    from scripts import regenerate_backtests as script

    db_path = tmp_path / "backtests.db"
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE backtests (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO backtests (id) VALUES ('bt-seed')")
    connection.commit()
    # The commit above lives in <db>-wal until the last connection closes; a raw
    # file copy taken while the writer is still open misses it.
    assert (tmp_path / "backtests.db-wal").stat().st_size > 0
    open_writer = sqlite3.connect(db_path)

    try:
        destination = script._dump_sqlite_backup(db_path, backup_dir)
    finally:
        open_writer.close()
        connection.close()

    with sqlite3.connect(destination) as restored:
        rows = restored.execute("SELECT id FROM backtests").fetchall()
    assert rows == [("bt-seed",)]
    assert not (backup_dir / "backtests.db-wal").exists()


def test_existing_db_path_falls_back_when_engine_url_is_not_absolute(monkeypatch):
    from scripts import regenerate_backtests as script

    engine = database.create_engine("sqlite:///relative.db")
    monkeypatch.setattr(database, "ENGINE", engine)

    # A relative engine URL cannot be trusted to point at the live corpus, so the
    # resolver must fall back to shared.database.DB_PATH.
    assert script._existing_db_path() == database.DB_PATH
    engine.dispose()


def test_regenerate_dry_run_reports_plan_without_destroying(isolated_db, monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    backtests_dir = _seed_corpus(isolated_db, tmp_path)
    monkeypatch.setattr(script, "DATA_DIR", tmp_path)
    monkeypatch.setattr(script, "BACKTESTS_DIR", backtests_dir)

    plan = script.plan_regeneration()

    with database.get_session() as session:
        assert session.exec(select(database.BacktestRecord)).all()
    assert (backtests_dir / "bt-seed.trades.json").exists()
    assert hasattr(plan, "entries")


def test_regenerate_confirmed_run_restores_corpus_when_all_runs_fail(
    isolated_db, monkeypatch, tmp_path
):
    from scripts import regenerate_backtests as script

    backtests_dir = _seed_corpus(isolated_db, tmp_path)
    monkeypatch.setattr(script, "DATA_DIR", tmp_path)
    monkeypatch.setattr(script, "BACKTESTS_DIR", backtests_dir)

    def failing_run(*_args, **_kwargs):
        raise script.BacktestRunError("demo_strategy: run failed (boom)")

    monkeypatch.setattr(script, "_run_strategy", failing_run)
    monkeypatch.setattr(
        script, "iter_strategy_files", lambda _root: [Path("strategies/demo/demo_strategy.py")]
    )
    monkeypatch.setattr(script, "detect_strategy_type", lambda _p, _root: object())
    monkeypatch.setattr(script, "load_strategy_meta", lambda _p, _root: {"tags": {}})
    monkeypatch.setattr(
        script, "_build_symbol_index", lambda _d: {"EURUSD": {"1h": [tmp_path / "x.parquet"]}}
    )
    monkeypatch.setattr(script, "_read_parquet_date_range", lambda _p: ("2024-01-01", "2024-02-01"))

    outcome = script.regenerate(dry_run=False, assume_yes=True, strategies=[])

    assert outcome.successes == 0
    assert outcome.failures >= 1
    with database.get_session() as session:
        assert len(session.exec(select(database.BacktestRecord)).all()) == 1
    assert outcome.restored is True
    assert (backtests_dir / "bt-seed.trades.json").exists()


def test_regenerate_refuses_backup_dir_inside_backtests_dir(isolated_db, monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    backtests_dir = _seed_corpus(isolated_db, tmp_path)
    monkeypatch.setattr(script, "DATA_DIR", tmp_path)
    monkeypatch.setattr(script, "BACKTESTS_DIR", backtests_dir)

    with pytest.raises(ValueError, match="outside"):
        script.regenerate(
            dry_run=False,
            assume_yes=True,
            backup_dir=backtests_dir / "snapshot",
        )

    # Nothing was purged: the validation runs before the destructive steps.
    with database.get_session() as session:
        assert len(session.exec(select(database.BacktestRecord)).all()) == 1
    assert (backtests_dir / "bt-seed.trades.json").exists()


def test_regenerate_refuses_without_confirmation(isolated_db, monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    backtests_dir = _seed_corpus(isolated_db, tmp_path)
    monkeypatch.setattr(script, "DATA_DIR", tmp_path)
    monkeypatch.setattr(script, "BACKTESTS_DIR", backtests_dir)

    with pytest.raises(SystemExit):
        script.regenerate(
            dry_run=False, assume_yes=False, strategies=["s.py"], _input=lambda _p: "n"
        )

    with database.get_session() as session:
        assert len(session.exec(select(database.BacktestRecord)).all()) == 1
    assert (backtests_dir / "bt-seed.trades.json").exists()


def test_regenerate_keeps_json_backup_outside_backtests_dir(isolated_db, monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    backtests_dir = _seed_corpus(isolated_db, tmp_path)
    monkeypatch.setattr(script, "DATA_DIR", tmp_path)
    monkeypatch.setattr(script, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(script, "iter_strategy_files", lambda _root: [])
    monkeypatch.setattr(script, "_build_symbol_index", lambda _d: {})

    outcome = script.regenerate(dry_run=False, assume_yes=True, strategies=[])

    assert outcome.successes == 0
    assert outcome.restored is True
    backups = [p for p in tmp_path.rglob("*.trades.json") if p.parent != backtests_dir]
    assert any(p.name == "bt-seed.trades.json" for p in backups)
    assert (backtests_dir / "bt-seed.trades.json").exists()


# ---------------------------------------------------------------------------
# `recalculate_backtest_stats.py --dry-run` wrote to the database.
#
# This file already holds `test_consolidate_dry_run_leaves_tree_untouched`, so "a dry run leaves
# things alone" is a contract this module owns rather than a new idea. The stats recalculator broke
# it, and did so in the most misleading way available.
#
# `init_db()` runs `create_all` and `_migrate_schema` - both write DDL - and it cannot be avoided on
# this path, because `list_backtest_ids` and `load_backtest_result` each call it internally.
# Measured
# on an empty directory:
#
#     $ recalculate_backtest_stats.py --dry-run
#     Dry run: yes
#     SQLite: scanned=0 updated=0 failed=0
#     $ ls
#     backtests.db            # 479 KB, 17 tables, created by the dry run
#
# "Dry run: yes" above "updated=0" reads as *nothing changed*, and against production that is
# the one thing an operator used `--dry-run` to avoid: applying a pending schema migration.
#
# So the claim is now verified rather than assumed - the schema is fingerprinted either side of the
# run and a change is reported and failed with exit 2. The writes themselves remain, because
# removing them means reworking how every reader obtains its schema; what does not remain is the
# script's ability to assert something it never checked.
#
# Driven as a subprocess with `DATA_DIR` pointed at a scratch directory, so the real CLI, the real
# argument parsing and the real `init_db` all run and production is never opened.


def _run_recalculate(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATA_DIR"] = str(data_dir)
    env["TRADING_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "scripts/recalculate_backtest_stats.py", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=900,
    )


def _tables(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_recalculate_dry_run_refuses_to_claim_it_changed_nothing(tmp_path: Path) -> None:
    """The regression.

    A dry run against a database that does not exist yet cannot be a dry run, because `init_db`
    builds the schema. It must say so and fail, rather than report `updated=0`.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = _run_recalculate(data_dir, "--dry-run")

    assert result.returncode == 2, (
        f"a dry run that created the schema reported rc={result.returncode}\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "CHANGED the database schema" in result.stderr, result.stderr
    # It must name what it created, not merely carry the label. claim-check.sh learned this the
    # hard way - a gate that only says "wrong" makes the reader re-derive the figure by hand, which
    # is how its Size row went stale in the first place. Emptying the `created` list while keeping
    # the line survived my first version of this test.
    assert "created  :" in result.stderr, result.stderr
    assert "(none)" not in result.stderr.split("created  :", 1)[1].splitlines()[0], (
        "the report says nothing was created, but the run created the whole schema:\n"
        + result.stderr
    )
    assert "backtests" in result.stderr, (
        "the report does not name a single created object: " + result.stderr
    )
    assert "not a dry run" in result.stderr, result.stderr
    # And it must not also claim the run was verified clean.
    assert "schema unchanged by this run" not in result.stdout, result.stdout


def test_recalculate_dry_run_on_a_current_schema_is_unaffected(tmp_path: Path) -> None:
    """The control, and the reason the fix is a verification rather than a refusal.

    A fix that made every dry run fail would be useless: the legitimate case is a dry run against a
    database whose schema is already current, which must still succeed and say it verified.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert _run_recalculate(data_dir, "--skip-json").returncode == 0

    result = _run_recalculate(data_dir, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema unchanged by this run" in result.stdout, result.stdout


def test_the_schema_check_applies_only_to_dry_runs(tmp_path: Path) -> None:
    """A real run is *supposed* to create the schema, so it must not be failed for doing so.

    Without this, the obvious "fix" - refuse whenever the schema changes - would break the script's
    actual purpose.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = _run_recalculate(data_dir, "--skip-json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema unchanged by this run" not in result.stdout, result.stdout
    assert _tables(data_dir / "backtests.db"), "a real run should still create the schema"


def test_a_dry_run_on_a_current_schema_writes_no_rows(tmp_path: Path) -> None:
    """The other half of "dry run": no row content either.

    The fingerprint is deliberately schema-only, which is sound because both persisted-stats writes
    sit behind `if not dry_run`. This is the test that keeps that assumption honest - if a write
    path ever lost its guard, a schema comparison would not notice, and only this would.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert _run_recalculate(data_dir, "--skip-json").returncode == 0
    db_path = data_dir / "backtests.db"
    before = _tables(db_path)
    digest_before = db_path.read_bytes()

    assert _run_recalculate(data_dir, "--dry-run").returncode == 0

    assert _tables(db_path) == before
    assert db_path.read_bytes() == digest_before, (
        "a dry run modified the database file's bytes; the schema-only fingerprint would not "
        "have caught it"
    )


def test_the_fingerprint_sees_an_ALTER_that_adds_no_new_object(tmp_path: Path) -> None:
    """The half a name-only comparison cannot see - tested directly, and honestly.

    `_migrate_schema` delegates to `_add_*_column` helpers, so on a populated production database -
    which already has every table - the dry run's only write is `ALTER TABLE ... ADD COLUMN`. That
    adds **no new row to `sqlite_master`**; it rewrites the `sql` of a table already listed.
            Comparing
    object names alone would pass, `updated=0` would print, and a schema migration would have been
    applied to production by the command meant to prevent exactly that.

    **Production is in that state right now.** Measured read-only: `services` has no
    `last_attempt_at` and `live_deployments` no `stopped_enforced_at`, so both migrations
            are pending
    there. So this is not a hypothetical branch.

    Why this test pokes `_schema_fingerprint` rather than driving the script end to end: I could not
    get `_migrate_schema` to apply the ALTER through the script's own path on a seeded scratch
    database - it created the tables via `create_all` and returned OK, yet the column never
            appeared,
    while calling `init_db()` directly in the same directory added it every time. I did not
            find that
    discrepancy, so I am not going to write a test that asserts a mechanism I could not observe. The
    property under test - *does the fingerprint notice a definition change?* - is stated here
    directly, which is both decidable and independent of `_migrate_schema`'s behaviour.
    """
    script = _load_recalculate_module()
    db_path = tmp_path / "fp.db"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(script, "DB_PATH", db_path)
    try:
        # A missing database reads as an empty schema, so "absent" is distinguishable from
        # "changed".
        assert script._schema_fingerprint() == (frozenset(), frozenset())

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE services (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        before = script._schema_fingerprint()
        assert before[0], "the seeded table should appear in the object-name half"

        conn.execute("ALTER TABLE services ADD COLUMN last_attempt_at DATETIME")
        conn.commit()
        conn.close()
        after = script._schema_fingerprint()

        assert after[0] == before[0], (
            "an ALTER that adds a column adds no sqlite_master row, so the name half must be "
            "unchanged - that is the whole reason the definition half exists"
        )
        assert after[1] != before[1], (
            "the fingerprint did not notice a column being added to an existing table; a dry run "
            "would migrate the schema and still report updated=0"
        )
    finally:
        monkey.undo()


def _load_recalculate_module():
    """Import `scripts/recalculate_backtest_stats.py` by path - `scripts/` is not a package."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "recalculate_backtest_stats.py"
    spec = importlib.util.spec_from_file_location("recalculate_backtest_stats_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
