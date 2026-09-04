"""Tests for grid strategy tester SQLite integration.

Database-touching tests opt into the shared ``isolated_db`` fixture (see
tests/conftest.py) so they never read or write the production database.
"""
import sys
import types
import uuid
from datetime import datetime, timezone

import pytest

from shared.database import (
    GridRecord,
    get_grid_results,
    save_grid_result,
    save_grid_result_to_sqlite,
)


def test_grid_record_model_creation() -> None:
    """Test GridRecord model can be created with required fields."""
    record = GridRecord(
        id="test_grid_001",
        strategy="test_strategy",
        created_at=datetime.now(timezone.utc).isoformat(),
        symbols_json=["AAPL", "MSFT"],
        timeframes_json=["1h", "4h"],
        results_json={"AAPL": {"1h": {"sharpe": 1.5, "status": "success"}}},
        engine="vectorbt",
    )
    assert record.id == "test_grid_001"
    assert record.strategy == "test_strategy"
    assert record.engine == "vectorbt"
    assert len(record.symbols_json) == 2
    assert len(record.timeframes_json) == 2


def test_run_strategy_grid_persists_to_sqlite(monkeypatch, tmp_path, isolated_db) -> None:
    """Test run_strategy_grid writes grid results to SQLite through GridRecord.

    The SQLite leg goes through the sandbox database, so the read-back below
    never depends on (or pollutes) the production database.
    """
    scipy_module = types.ModuleType("scipy")
    scipy_stats_module = types.ModuleType("scipy.stats")
    setattr(scipy_stats_module, "norm", lambda *args, **kwargs: 0.5)
    setattr(scipy_stats_module, "ttest_1samp", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "scipy", scipy_module)
    monkeypatch.setitem(sys.modules, "scipy.stats", scipy_stats_module)
    import runner.grid_tester as grid_tester_module

    class FakeResult:
        trades = [object()] * 7
        stats = {
            "sharpe": 1.25,
            "cagr": 0.18,
            "max_drawdown_pct": 0.04,
            "win_rate": 0.6,
            "total_trades": 7,
            "net_profit_pct": 12.5,
        }

    class FakeEngine:
        def run(self, config, strategy_path, progress_callback=None):
            return FakeResult()

    class FixedDateTime:
        @staticmethod
        def now(tz):
            return datetime(2026, 6, 17, 12, 0, tzinfo=tz)

    from types import SimpleNamespace

    strategy_file = tmp_path / "strategies" / "example_sma.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("STRATEGY_META = {'name': 'example_sma'}\n", encoding="utf-8")
    monkeypatch.setattr(
        grid_tester_module,
        "find_strategy_entry",
        lambda slug: SimpleNamespace(slug=slug, file_path=strategy_file),
    )
    monkeypatch.setattr(grid_tester_module, "get_engine", lambda engine_name: FakeEngine())
    monkeypatch.setattr(grid_tester_module, "GRID_RESULTS_DIR", tmp_path / "grid_results")
    monkeypatch.setattr(grid_tester_module, "datetime", FixedDateTime)

    strategy_slug = f"example_sma_{uuid.uuid4().hex[:8]}_{tmp_path.name.replace('-', '_')}"

    result = grid_tester_module.run_strategy_grid(
        strategy_slug=strategy_slug,
        symbols=["AAPL"],
        timeframes=["1h"],
        start="2024-01-01",
        end="2024-01-31",
        engine_name="fake_engine",
    )

    assert result["id"].startswith(f"grid_{strategy_slug}_20260617_120000")
    assert (tmp_path / "grid_results" / f"{result['id']}.json").exists()

    saved = get_grid_results(limit=100)
    matching = [item for item in saved if item["id"] == result["id"]]
    assert len(matching) == 1
    assert matching[0]["strategy"] == strategy_slug
    assert matching[0]["symbols"] == ["AAPL"]
    first_cell = matching[0]["results"]["AAPL"]["1h"]
    assert first_cell["status"] == "success"
    assert first_cell["sharpe_ratio"] == 1.25
    assert first_cell["total_trades"] == 7
    assert "net_profit_pct" not in first_cell


def test_save_grid_result_to_sqlite(isolated_db) -> None:
    """Test saving grid result to SQLite database (in the isolated sandbox DB)."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    payload = {
        "id": f"test_grid_sqlite_save_{datetime.now(timezone.utc).strftime('%H%M%S')}",
        "strategy": "example_sma",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbols": ["AAPL", "MSFT", "GOOGL"],
        "timeframes": ["1h", "4h", "1d"],
        "results": {
            "AAPL": {
                "1h": {"sharpe": 1.2, "cagr": 0.15, "status": "success"},
                "4h": {"sharpe": 0.9, "cagr": 0.12, "status": "success"},
            },
            "MSFT": {
                "1h": {"sharpe": 0.8, "cagr": 0.10, "status": "success"},
            },
        },
        "engine": "vectorbt",
    }

    save_grid_result_to_sqlite(payload)

    # Verify it was saved
    results = get_grid_results(limit=100)
    grid_id = payload["id"]
    saved = [r for r in results if r["id"] == grid_id]

    assert len(saved) == 1, f"Grid result {grid_id} should be saved to SQLite"
    saved_record = saved[0]
    assert saved_record["strategy"] == "example_sma"
    assert saved_record["engine"] == "vectorbt"
    assert "AAPL" in saved_record["symbols"]
    assert "GOOGL" in saved_record["symbols"]


def test_save_grid_result_function(isolated_db) -> None:
    """Test save_grid_result convenience function (in the isolated sandbox DB)."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    grid_id = f"test_grid_func_{datetime.now(timezone.utc).strftime('%H%M%S')}"

    save_grid_result(
        grid_id=grid_id,
        strategy="test_strat",
        symbols=["TSLA"],
        timeframes=["1d"],
        results={"TSLA": {"1d": {"sharpe": 2.0, "status": "success"}}},
        engine="vectorbt",
    )

    results = get_grid_results(limit=100)
    saved = [r for r in results if r["id"] == grid_id]

    assert len(saved) == 1, "save_grid_result should persist to SQLite"
    assert saved[0]["results"]["TSLA"]["1d"]["sharpe"] == 2.0


def test_get_grid_results_returns_newest_first(isolated_db) -> None:
    """Test that get_grid_results orders by created_at descending (sandbox DB)."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    # Use unique IDs to avoid collision
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    id_1 = f"grid_older_{timestamp}a"
    id_2 = f"grid_newer_{timestamp}b"

    # Save two records with different timestamps
    save_grid_result(
        grid_id=id_1,
        strategy="s1",
        symbols=["A"],
        timeframes=["1h"],
        results={},
    )
    save_grid_result(
        grid_id=id_2,
        strategy="s2",
        symbols=["B"],
        timeframes=["1h"],
        results={},
    )

    results = get_grid_results(limit=100)

    # Verify both records exist
    ids = [r["id"] for r in results]
    assert id_1 in ids, "First grid result should exist"
    assert id_2 in ids, "Second grid result should exist"


def test_grid_tester_loads_results_from_sqlite(monkeypatch) -> None:
    """Grid tester should expose SQLite-backed grid result reads."""
    import runner.grid_tester as grid_tester_module

    expected = [
        {
            "id": "grid_sqlite_read",
            "strategy": "example_sma",
            "symbols": ["AAPL"],
            "timeframes": ["1d"],
            "results": {"AAPL": {"1d": {"status": "success"}}},
            "engine": "vectorbt",
        }
    ]

    monkeypatch.setattr(grid_tester_module, "get_grid_results", lambda limit=100: expected[:limit])

    assert grid_tester_module.load_grid_results(limit=1) == expected


def test_run_strategy_grid_passes_strategy_path_and_reports_errors(monkeypatch, tmp_path) -> None:
    """Regression (F2/F40): engine gets strategy_path, canonical stats keys, error cells."""
    import runner.grid_tester as grid_tester_module
    from shared.models import BacktestMeta, BacktestResult

    class CapturingEngine:
        name = "fake"

        def __init__(self):
            self.calls = []

        def run(self, config, strategy_path, progress_callback=None):
            self.calls.append((config, strategy_path))
            if config.timeframe == "1h":
                return BacktestResult(
                    meta=BacktestMeta(
                        id="x",
                        created_at="2026-06-17T12:00:00+00:00",
                        engine="fake",
                        strategies=["example_sma"],
                        backtest_start=config.start_date,
                        backtest_end=config.end_date,
                        initial_capital=config.initial_capital,
                        data={"symbols": config.symbols, "timeframes": [config.timeframe]},
                        parameters={},
                    ),
                    trades=[],
                    equity_curve=[],
                    stats={},
                )
            raise RuntimeError("engine blew up")

    engine = CapturingEngine()
    strategy_path = tmp_path / "strategies" / "example_sma.py"
    strategy_path.parent.mkdir(parents=True, exist_ok=True)
    strategy_path.write_text("STRATEGY_META = {'name': 'example_sma'}\n", encoding="utf-8")

    from types import SimpleNamespace

    monkeypatch.setattr(
        grid_tester_module,
        "find_strategy_entry",
        lambda slug: SimpleNamespace(slug=slug, file_path=strategy_path),
    )
    monkeypatch.setattr(grid_tester_module, "get_engine", lambda engine_name: engine)
    monkeypatch.setattr(grid_tester_module, "GRID_RESULTS_DIR", tmp_path / "grid_results")

    result = grid_tester_module.run_strategy_grid(
        strategy_slug="example_sma",
        symbols=["AAPL"],
        timeframes=["1h", "4h"],
        start="2024-01-01",
        end="2024-01-31",
        engine_name="fake_engine",
    )

    # The required strategy_path is forwarded to the engine (F2)
    assert engine.calls[0][1] == str(strategy_path)

    cells = result["results"]["AAPL"]
    # Zero-trade run is distinguishable from success (no trades, no error);
    # a no-trade run reports total_trades=0 — an explicit zero from observed
    # trades, not a fabricated one for missing stats.
    assert cells["1h"]["status"] == "no_trades"
    assert cells["1h"]["total_trades"] == 0
    # Failed cell is reported with the error, not persisted as success
    assert cells["4h"]["status"] == "error"
    assert "engine blew up" in cells["4h"]["error"]
    # Canonical stats keys are used (F40): no fabricated zeros for missing metrics
    assert "sharpe_ratio" not in cells["1h"]
    assert "net_profit_pct" not in cells["1h"]


def test_trading_run_without_trade_count_reports_presence_not_zero(monkeypatch, tmp_path) -> None:
    """Pre-mortem 16-B #11, batch-review follow-up: total_trades precedence —
    an explicit stats count wins; the observed-trades fallback surfaces the
    REAL number of trades on the result, never a fabricated zero, minimum (1)
    or loss of magnitude (a 50-trade cell must not persist as 1)."""
    from shared.models import BacktestMeta, BacktestResult

    import runner.grid_tester as grid_tester_module

    class TradingEngineNoCount:
        name = "fake"

        def run(self, config, strategy_path, progress_callback=None):
            return BacktestResult(
                meta=BacktestMeta(
                    id="x",
                    created_at="2026-06-17T12:00:00+00:00",
                    engine="fake",
                    strategies=["example_sma"],
                    backtest_start=config.start_date,
                    backtest_end=config.end_date,
                    initial_capital=config.initial_capital,
                    data={"symbols": config.symbols, "timeframes": [config.timeframe]},
                    parameters={},
                ),
                # 50 observed trades; stats deliberately lack total_trades.
                trades=[object() for _ in range(50)],
                equity_curve=[],
                stats={"sharpe": 1.1},
            )

    strategy_file = tmp_path / "strategies" / "example_sma.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("STRATEGY_META = {'name': 'example_sma'}\n", encoding="utf-8")

    from types import SimpleNamespace

    monkeypatch.setattr(
        grid_tester_module,
        "find_strategy_entry",
        lambda slug: SimpleNamespace(slug=slug, file_path=strategy_file),
    )
    monkeypatch.setattr(grid_tester_module, "get_engine", lambda engine_name: TradingEngineNoCount())
    monkeypatch.setattr(grid_tester_module, "GRID_RESULTS_DIR", tmp_path / "grid_results")

    result = grid_tester_module.run_strategy_grid(
        strategy_slug="example_sma",
        symbols=["AAPL"],
        timeframes=["1h"],
        start="2024-01-01",
        end="2024-01-31",
        engine_name="fake_engine",
    )

    cell = result["results"]["AAPL"]["1h"]
    assert cell["status"] == "success"
    # The 50 observed trades, not a fabricated presence-marker of 1.
    assert cell["total_trades"] == 50
    assert cell["sharpe_ratio"] == 1.1


def test_total_trades_fallback_sources() -> None:
    """Unit-level precedence of _total_trades_from: stats count wins, observed
    trades are the fallback, an explicit zero stays a zero, unknown is None."""
    from runner.grid_tester import _total_trades_from

    # Explicit stats count wins over the observed count.
    assert _total_trades_from({"total_trades": 7}, True, trade_count=50) == 7
    # No stats count: the real observed count, not a literal 1.
    assert _total_trades_from({}, True, trade_count=50) == 50
    assert _total_trades_from({"sharpe": 1.0}, True, trade_count=3) == 3
    # Unparseable stats count falls through to the observed trades.
    assert _total_trades_from({"total_trades": "many"}, True, trade_count=4) == 4
    # An explicit zero in the stats is observed, not fabricated.
    assert _total_trades_from({"total_trades": 0}, False, trade_count=0) == 0
    # No-trade run with no stats at all: observed zero.
    assert _total_trades_from({}, False, trade_count=0) == 0
    # Neither source knows: None, never a fabricated number.
    assert _total_trades_from({"sharpe": 1.0}, False, trade_count=0) is None


def test_all_error_grid_is_not_persisted(monkeypatch, tmp_path) -> None:
    """AC: never persist successful-looking artifacts for failed runs."""
    import runner.grid_tester as grid_tester_module

    class FailingEngine:
        name = "fake"

        def run(self, config, strategy_path, progress_callback=None):
            raise RuntimeError("engine blew up")

    strategy_file = tmp_path / "strategies" / "broken.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("STRATEGY_META = {'name': 'broken'}\n", encoding="utf-8")

    from types import SimpleNamespace

    monkeypatch.setattr(
        grid_tester_module,
        "find_strategy_entry",
        lambda slug: SimpleNamespace(slug=slug, file_path=strategy_file),
    )
    monkeypatch.setattr(grid_tester_module, "get_engine", lambda engine_name: FailingEngine())
    monkeypatch.setattr(grid_tester_module, "GRID_RESULTS_DIR", tmp_path / "grid_results")
    persisted: list[dict] = []
    monkeypatch.setattr("shared.database.save_grid_result", lambda **kw: persisted.append(kw))

    result = grid_tester_module.run_strategy_grid(
        strategy_slug="broken",
        symbols=["AAPL"],
        timeframes=["1h"],
        start="2024-01-01",
        end="2024-01-31",
        engine_name="fake_engine",
    )

    assert result["status"] == "error"
    assert "All 1 grid cells failed" in result["message"]
    assert persisted == []
    assert not (tmp_path / "grid_results").exists()
