"""Safety guards for the destructive ops scripts (Cleanup 20-C).

Both scripts destroy large portions of the research corpus, so they must be
dry-run by default, require an explicit confirmation, keep a restorable backup
and swap the new state in atomically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.lib
import pytest
from sqlmodel import select

import shared.database as database
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit


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
    stamps = pd.date_range("2024-01-01", periods=rows, freq="1h", tz="UTC")
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
    frame = _parquet_frame(0, 3)
    frame.to_parquet(root / "1h.parquet", index=False)
    frame.iloc[:2].to_parquet(root / "1h_part2.parquet", index=False)
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


def test_consolidate_unreadable_fragment_aborts_without_changes(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    (ohlcv_root / "EURUSD" / "broken_part3.parquet").write_bytes(b"not a parquet file")

    with pytest.raises(pyarrow.lib.ArrowInvalid):
        consolidate(ohlcv_root, backup_dir=ohlcv_root.parent / "backup")

    names = sorted(p.name for p in ohlcv_root.rglob("*.parquet"))
    assert "1h.parquet" in names
    assert "1h_part2.parquet" in names
    assert (ohlcv_root / "EURUSD" / "1h.parquet").read_bytes() != b""


def test_consolidate_confirmed_run_merges_fragments(ohlcv_root):
    from scripts.consolidate_parquets import consolidate

    merged, deleted, _ = consolidate(ohlcv_root, backup_dir=ohlcv_root.parent / "backup")

    assert (merged, deleted) == (1, 1)
    merged_frame = pd.read_parquet(ohlcv_root / "EURUSD" / "1h.parquet")
    assert list(merged_frame["timestamp"]) == list(merged_frame["timestamp"].sort_values())
    assert len(merged_frame) == 3


# ─── scripts/regenerate_backtests.py ─────────────────────────────────────────


def test_existing_db_path_prefers_absolute_engine_url(monkeypatch, tmp_path):
    from scripts import regenerate_backtests as script

    engine = database.create_engine(f"sqlite:///{(tmp_path / 'engine.db').as_posix()}")
    monkeypatch.setattr(database, "ENGINE", engine)

    assert script._existing_db_path() == tmp_path / "engine.db"
    engine.dispose()


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
    monkeypatch.setattr(
        script, "iter_strategy_files", lambda _root: [Path("strategies/demo/demo_strategy.py")]
    )
    monkeypatch.setattr(script, "_run_strategy", lambda *_a, **_k: False)

    outcome = script.regenerate(
        dry_run=False, assume_yes=True, strategies=["strategies/demo/demo_strategy.py"]
    )

    assert outcome.failures >= 1
    with database.get_session() as session:
        assert len(session.exec(select(database.BacktestRecord)).all()) == 1
    assert outcome.restored is True
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
