"""Security regression tests for Cleanup 18-A (viewer CORS + path containment).

These tests document the agreed behavior of the viewer backend:

- CORS must not reflect arbitrary client origins and must never send
  credentials along a wildcard origin.
- Strategy identifiers are resolved through the strategy registry, never
  through arbitrary filesystem paths handed to a subprocess.
- Data file deletion is contained to the resolved OHLCV data tree with a
  parquet-only extension whitelist.
- Bulk-delete endpoints never turn a client id into an unlink path.
- CSV signal replay and resample subprocesses only accept paths inside the
  OHLCV data tree.
"""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_parquet(path: Path) -> None:
    """Write a tiny OHLCV parquet file, as the catalog tooling would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="1h"),
            "open": [1.0, 1.0, 1.0, 1.0],
            "high": [1.1, 1.1, 1.1, 1.1],
            "low": [0.9, 0.9, 0.9, 0.9],
            "close": [1.05, 1.05, 1.05, 1.05],
            "volume": [10, 10, 10, 10],
        }
    )
    frame.to_parquet(path, index=False)


@pytest.fixture
def security_client(monkeypatch, tmp_path) -> TestClient:
    """Viewer client with DATA_DIR pointed at an isolated tmp tree."""
    import viewer.main as viewer_app_module
    from viewer import core as legacy_module

    for module in (viewer_app_module, legacy_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
        monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
        monkeypatch.setattr(module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
        monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")

    viewer_app_module.DATA_TASKS.clear()
    viewer_app_module._COALESCED_CLEANUP_PENDING.clear()
    return TestClient(viewer_app_module.app)


# ---------------------------------------------------------------------------
# Seams 1 + 2: CORS
# ---------------------------------------------------------------------------


def test_cors_does_not_reflect_arbitrary_origins(security_client):
    response = security_client.get(
        "/api/backtests", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 200
    allow_origin = response.headers.get("access-control-allow-origin")
    assert allow_origin is None or allow_origin == "*"


def test_cors_never_sends_credentials(security_client):
    response = security_client.options(
        "/api/backtests",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-credentials") != "true"
    allow_origin = response.headers.get("access-control-allow-origin")
    assert allow_origin is None or allow_origin == "*"


# ---------------------------------------------------------------------------
# Seam 3: strategy resolution registry allowlist
# ---------------------------------------------------------------------------


def test_backtest_run_rejects_strategy_path_outside_strategies_dir(
    security_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module

    launched: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):  # pragma: no cover - must not run
        if "runner.cli" in [str(part) for part in cmd]:
            launched.append(cmd)
        raise AssertionError("subprocess must not be started for escaped paths")

    monkeypatch.setattr(viewer_app_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(viewer_app_module, "_dolthub_pull", lambda: None)

    outside = tmp_path / "outside" / "evil.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("raise SystemExit('pwned')\n", encoding="utf-8")

    response = security_client.post(
        "/api/backtests/run",
        json={"strategy": str(outside), "symbols": ["AAPL"]},
    )

    assert response.status_code == 404
    assert launched == []


def test_backtest_run_resolves_registry_identifier(
    security_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module
    from shared.strategy_registry import StrategyEntry

    strategies_dir = tmp_path / "strategies" / "demo_strategy"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    strategy_file = strategies_dir / "demo_strategy.py"
    strategy_file.write_text("STRATEGY_META = {'name': 'Demo'}\n", encoding="utf-8")

    entry = StrategyEntry(
        slug="demo_strategy",
        file_path=strategy_file,
        relative_file="demo_strategy/demo_strategy.py",
        directory=strategies_dir,
        documentation_path=None,
        meta={"name": "Demo"},
    )
    monkeypatch.setattr(
        viewer_app_module.legacy_viewer, "_strategy_entries", lambda: [entry]
    )
    monkeypatch.setattr(viewer_app_module, "_dolthub_pull", lambda: None)
    monkeypatch.setattr(
        viewer_app_module, "_ensure_backtest_data_availability", lambda **kwargs: []
    )

    class _FakeProc:
        def __init__(self, lines):
            self.stdout = iter(lines)
            self.pid = 1234

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

        def kill(self):
            return None

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(["PROGRESS:100.00:Completed - 0 trades found\n"])

    monkeypatch.setattr(viewer_app_module.subprocess, "Popen", fake_popen)

    response = security_client.post(
        "/api/backtests/run",
        json={"strategy": "demo_strategy", "symbols": ["AAPL"]},
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    record = viewer_app_module.DATA_TASKS[task_id]
    command = record["command"]
    strategy_arguments = [
        str(command[index + 1])
        for index, part in enumerate(command)
        if str(part) == "--strategy"
    ]
    assert strategy_arguments == [str(strategy_file)]


# ---------------------------------------------------------------------------
# Seam 4: data file deletion containment
# ---------------------------------------------------------------------------


def test_delete_symbol_data_rejects_path_outside_data_dir(
    security_client, tmp_path
):
    outside = Path(tempfile.mkdtemp()) / "notes.txt"
    outside.write_text("keep me", encoding="utf-8")

    response = security_client.delete(f"/api/data/symbol?path={outside}")

    assert response.status_code == 403
    assert outside.exists()


def test_delete_symbol_data_rejects_non_parquet_suffix_inside_data_dir(
    security_client, tmp_path
):
    target = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")

    response = security_client.delete(f"/api/data/symbol?path={target}")

    assert response.status_code == 400
    assert target.exists()


def test_delete_symbol_data_removes_contained_parquet(
    security_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module

    target = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    _make_parquet(target)
    monkeypatch.setattr(viewer_app_module, "_refresh_catalog", lambda: None)
    monkeypatch.setattr(viewer_app_module.cache_manager, "invalidate_cache", lambda *a, **k: None)
    monkeypatch.setattr(viewer_app_module, "_invalidate_data_views_cache", lambda: None)

    response = security_client.delete(f"/api/data/symbol?path={target}")

    assert response.status_code == 200
    assert not target.exists()


# ---------------------------------------------------------------------------
# Seam 5: bulk-delete id containment
# ---------------------------------------------------------------------------


def test_backtest_bulk_delete_ignores_path_traversal_ids(security_client, tmp_path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    secret = backtests_dir / "secret.trades.json"
    secret.write_text("{}", encoding="utf-8")

    response = security_client.post(
        "/api/backtests/delete-bulk",
        json={"ids": ["../backtests/secret"]},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert secret.exists()


def test_portfolio_bulk_delete_ignores_path_traversal_ids(security_client, tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    secret = portfolios_dir / "secret.json"
    secret.write_text("{}", encoding="utf-8")

    response = security_client.post(
        "/api/portfolios/delete-bulk",
        json={"ids": ["../portfolios/secret"]},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert secret.exists()


def test_portfolio_delete_rejects_path_traversal_id(security_client, tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    secret = portfolios_dir / "secret.json"
    secret.write_text("{}", encoding="utf-8")

    response = security_client.delete("/api/portfolios/..%2Fportfolios%2Fsecret")

    assert response.status_code in {404, 422}
    assert secret.exists()


# ---------------------------------------------------------------------------
# Seam 6: CSV signal replay containment
# ---------------------------------------------------------------------------


def test_signal_replay_rejects_csv_outside_data_dir(security_client, tmp_path):
    outside = Path(tempfile.mkdtemp()) / "signals.csv"
    outside.write_text("Symbol;Action\nAAPL;buy\n", encoding="utf-8")

    response = security_client.post(
        "/api/backtests/run-from-signals",
        json={
            "signal_source": "csv",
            "filepath": str(outside),
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        },
    )

    assert response.status_code == 403


def test_signal_replay_accepts_csv_inside_data_dir(
    security_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module

    csv_path = tmp_path / "signals" / "signals_demo.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "Symbol;Signal Price;Datum;Zeit (NY);Signal Type\n"
        "AAPL;173,79;2026-02-23;09:32;BUY\n"
        "AAPL;175,10;2026-02-23;10:07;SELL\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class _FakeAdapter:
        def __init__(self, **kwargs):
            captured["adapter_kwargs"] = kwargs

        def run(self, **kwargs):
            from shared.models import (
                BacktestMeta,
                BacktestResult,
                EquityPoint,
                Trade,
            )

            meta = BacktestMeta(
                id=f"replay-{uuid.uuid4().hex[:8]}",
                created_at="2026-03-11T10:00:00Z",
                engine="native",
                strategies=["CSV signals"],
                backtest_start="2024-01-01",
                backtest_end="2024-01-31",
                initial_capital=100000.0,
                duration_days=30,
                data={"symbols": ["AAPL"], "timeframes": ["1h"]},
            )
            result = BacktestResult(
                meta=meta,
                trades=[],
                equity_curve=[EquityPoint(time="2024-01-02T00:00:00Z", equity=100000.0)],
            )
            captured["run_kwargs"] = kwargs
            return result

    monkeypatch.setattr(viewer_app_module, "SignalReplayAdapter", _FakeAdapter)
    monkeypatch.setattr(viewer_app_module, "save_result_to_sqlite", lambda _r: None)
    monkeypatch.setattr(viewer_app_module, "_upsert_backtest_meta_cache", lambda _i: None)
    monkeypatch.setattr(
        viewer_app_module,
        "legacy_viewer",
        SimpleNamespace(
            _load_ohlcv_df=lambda symbol, timeframe: pd.DataFrame(
                {
                    "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="1h"),
                    "open": [1.0] * 4,
                    "high": [1.1] * 4,
                    "low": [0.9] * 4,
                    "close": [1.05] * 4,
                    "volume": [10] * 4,
                }
            ),
            BARS_BEFORE=60,
            BARS_AFTER=25,
        ),
    )

    response = security_client.post(
        "/api/backtests/run-from-signals",
        json={
            "signal_source": "csv",
            "filepath": str(csv_path),
            "start_date": "2026-02-23",
            "end_date": "2026-02-24",
        },
    )

    assert response.status_code == 202, response.text


# ---------------------------------------------------------------------------
# Seam 7: resample containment
# ---------------------------------------------------------------------------


def test_resample_rejects_source_path_outside_data_dir(security_client, tmp_path):
    import viewer.main as viewer_app_module

    outside = Path(tempfile.mkdtemp()) / "1h.parquet"
    _make_parquet(outside)

    response = security_client.post(
        "/api/data/resample",
        json={"source_path": str(outside), "target_timeframes": "1d"},
    )

    assert response.status_code == 400
    assert viewer_app_module.DATA_TASKS == {}
    assert not list(outside.parent.glob("*1d.parquet"))


def test_resample_accepts_source_path_inside_data_dir(
    security_client, tmp_path, monkeypatch
):
    import viewer.main as viewer_app_module
    from viewer import core as legacy_module

    source = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    _make_parquet(source)

    started: dict[str, object] = {}

    def fake_start(task_id, cmd, **kwargs):
        started["task_id"] = task_id
        started["cmd"] = list(cmd)
        return {"task_id": task_id, "status": "starting"}

    monkeypatch.setattr(legacy_module, "_start_data_task", fake_start)
    monkeypatch.setattr(viewer_app_module, "_LEGACY_START_DATA_TASK", fake_start)

    response = security_client.post(
        "/api/data/resample",
        json={"source_path": str(source), "target_timeframes": "1d"},
    )

    assert response.status_code == 200
    command = started["cmd"]
    source_arguments = [
        str(command[index + 1])
        for index, part in enumerate(command)
        if str(part) == "--source"
    ]
    assert source_arguments == [str(source.resolve())]
