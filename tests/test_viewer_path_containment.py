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

    assert response.status_code in {400, 403}
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
    payload = response.json()
    assert payload["deleted"] == 0
    assert payload["items"][0]["status"] == "not_found"
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
    payload = response.json()
    assert payload["deleted"] == 0
    assert payload["items"][0]["status"] == "not_found"
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
        "Symbol;Signal Price;Datum;Zeit;Signal Type\n"
        "AAPL;173,79;2026-02-23;09:32:00-05:00;BUY\n"
        "AAPL;175,10;2026-02-23;10:07:00-05:00;SELL\n",
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


# ---------------------------------------------------------------------------
# Pre-mortem hardening (PR #50 review pipeline)
# Pin the containment helper contracts and the middleware configuration as
# CI-enforced invariants, so a future refactor cannot quietly weaken them.
# ---------------------------------------------------------------------------


def test_safe_asset_id_rejects_traversal_and_separators():
    import viewer.main as viewer_app_module

    for raw in ("..", ".", "", "  ", "a/b", "a\\b", "../secret", "id/../x", "a\nb"):
        assert viewer_app_module._safe_asset_id(raw) == "", f"{raw!r} must be rejected"
    assert viewer_app_module._safe_asset_id(" valid-id_1.2 ") == "valid-id_1.2"


def test_contained_asset_path_never_leaves_base_dir(security_client, tmp_path):
    import viewer.main as viewer_app_module

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    secret = backtests_dir.parent / "secret.trades.json"
    secret.write_text("{}", encoding="utf-8")

    contained = viewer_app_module._contained_asset_path(
        backtests_dir, "abc123", viewer_app_module.BACKTEST_TRADES_SUFFIX
    )
    assert contained == (backtests_dir / "abc123.trades.json").resolve()
    assert contained.parent == backtests_dir.resolve()

    for raw in ("../secret", "..%2Fsecret", "a/b", "", None, "sub/dir/id"):
        assert (
            viewer_app_module._contained_asset_path(backtests_dir, raw, ".json") is None
        ), f"{raw!r} must not resolve to an unlink path"
    assert secret.exists()


def test_contained_client_data_file_rejects_escape_via_dotdot(
    monkeypatch, tmp_path
):
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)
    data_root = tmp_path / "signals"
    data_root.mkdir(parents=True, exist_ok=True)
    outside = Path(tempfile.mkdtemp()) / "outside.csv"
    outside.write_text("x", encoding="utf-8")

    (data_root / "sub").mkdir(parents=True, exist_ok=True)
    real = data_root / "sub" / "file.csv"
    real.write_text("x", encoding="utf-8")
    resolved = viewer_app_module._contained_client_data_file(
        str(real), allowed_suffix=".csv"
    )
    assert resolved == real.resolve()

    for raw in (
        str(outside),
        str(data_root / "sub" / ".." / ".." / ".." / "outside.csv"),
        str(data_root / "sub" / "file.csv" / ".." / ".." / ".." / ".." / "outside.csv"),
        str(data_root / "sub" / "file.parquet"),
        "",
        None,
    ):
        assert (
            viewer_app_module._contained_client_data_file(
                raw, allowed_suffix=".csv"
            )
            is None
        ), f"{raw!r} must not pass containment"
    assert outside.exists()


def test_cors_middleware_disallows_wildcard_origins_and_credentials():
    """The CORS decision must stay CI-enforced, not just documented in prose."""
    import viewer.main as viewer_app_module

    cors = next(
        m
        for m in viewer_app_module.app.user_middleware
        if m.cls.__name__ == "CORSMiddleware"
    )
    kwargs = cors.kwargs
    assert kwargs["allow_origins"] == []
    assert kwargs["allow_credentials"] is False


def test_strategy_path_resolves_only_regular_files_inside_strategies_dir(
    monkeypatch, tmp_path
):
    """CodeRabbit hardening: the registry fallback must stay file-only and contained.

    An absolute identifier must not resolve (pathlib drops the base when
    joining an absolute path), and directories must not resolve even when
    they exist, because the value is handed to a subprocess as a code file.
    """
    from viewer import core as legacy_module

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(legacy_module, "STRATEGIES_DIR", strategies_dir)

    (strategies_dir / "demo.py").write_text("STRATEGY_META = {'name': 'D'}\n")
    nested = strategies_dir / "nested"
    nested.mkdir()

    assert legacy_module._strategy_path("demo.py") == (strategies_dir / "demo.py").resolve()
    # absolute identifier must not resolve even though /etc/hosts exists
    assert legacy_module._strategy_path("/etc/hosts") is None
    # directories must not resolve
    assert legacy_module._strategy_path("nested") is None
    # blank identifier must not resolve
    assert legacy_module._strategy_path("   ") is None
    # missing identifier must not resolve
    assert legacy_module._strategy_path("missing.py") is None


def test_contained_client_data_file_accepts_signals_dir_and_widened_trees(
    monkeypatch, tmp_path
):
    """The default containment base is the whole DATA_DIR: production keeps
    signal CSVs under DATA_DIR / "signals" (server_planning/01_current_state.md,
    frontend placeholder /home/ubuntu/data/signals/...), which is NOT inside
    DATA_DIR / "ohlcv". Callers that manage a single subtree narrow via
    base_dir — the resample and symbol-delete seams do exactly that.
    """
    import viewer.main as viewer_app_module

    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)
    signals_csv = tmp_path / "signals" / "signals_demo.csv"
    signals_csv.parent.mkdir(parents=True, exist_ok=True)
    signals_csv.write_text("Symbol;Action\nAAPL;buy\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("x", encoding="utf-8")

    # Default base: the whole DATA_DIR tree, so the signals directory works.
    assert viewer_app_module._contained_client_data_file(
        str(signals_csv), allowed_suffix=".csv"
    ) == signals_csv.resolve()
    # Narrowed base (resample / symbol-delete seam): ohlcv only.
    assert (
        viewer_app_module._contained_client_data_file(
            str(signals_csv), allowed_suffix=".csv", base_dir=tmp_path / "ohlcv"
        )
        is None
    )
    # Anything outside DATA_DIR is still rejected under the default base.
    assert (
        viewer_app_module._contained_client_data_file(
            str(outside), allowed_suffix=".csv"
        )
        is None
    )
    assert outside.exists()


def test_viewer_ohlcv_loader_rejects_wildcard_identity_and_match_budget(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    _make_parquet(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet")

    assert core._load_ohlcv_df("*", "1h") is None
    assert core._load_ohlcv_df("EURUSD", "*") is None

    _make_parquet(tmp_path / "ohlcv" / "yfinance" / "EURUSD" / "1h.parquet")
    candidates = core._ohlcv_candidates(
        (tmp_path / "ohlcv").resolve(),
        "EURUSD",
        "1h",
    )
    assert candidates
    assert all("dukascopy" in str(path) for path in candidates)

    _make_parquet(tmp_path / "ohlcv" / "dukascopy" / "archive" / "EURUSD" / "1h.parquet")
    monkeypatch.setattr(core, "_OHLCV_MAX_MATCHES", 1)

    assert core._load_ohlcv_df("EURUSD", "1h") is None
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_enforces_byte_budget(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "_OHLCV_MAX_BYTES", 1)
    core._OHLCV_DF_CACHE.clear()
    _make_parquet(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet")

    assert core._load_ohlcv_df("EURUSD", "1h") is None
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_reads_only_canonical_columns(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "open": [1.0, 1.1],
            "high": [1.2, 1.3],
            "low": [0.9, 1.0],
            "close": [1.1, 1.2],
            "volume": [2, 3],
            "untrusted_extra": ["x" * 1000, "y" * 1000],
        }
    ).to_parquet(path, index=False)
    observed: dict[str, object] = {}
    original = core.pq.ParquetFile.iter_batches

    def tracked_iter_batches(self, *args, **kwargs):
        observed["columns"] = kwargs.get("columns")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(core.pq.ParquetFile, "iter_batches", tracked_iter_batches)

    loaded = core._load_ohlcv_df("EURUSD", "1h")

    assert loaded is not None
    assert observed["columns"] == core._OHLCV_COLUMNS
    assert "untrusted_extra" not in loaded.columns
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_rejects_noncanonical_schema_before_read(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "BAD" / "1h.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "open": [1.0, 1.1],
            "high": [1.2, 1.3],
            "low": [0.9, 1.0],
            "close": [1.1, 1.2],
            "untrusted_extra": ["x" * 1000, "y" * 1000],
        }
    ).to_parquet(path, index=False)

    def fail_iter_batches(*_args, **_kwargs):
        raise AssertionError("noncanonical parquet must be rejected before batching")

    monkeypatch.setattr(core.pq.ParquetFile, "iter_batches", fail_iter_batches)

    assert core._load_ohlcv_df("BAD", "1h") is None
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_maps_mixed_case_columns_to_canonical_names(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "Timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "Open": [1.0, 1.1],
            "HIGH": [1.2, 1.3],
            "low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Volume": [2, 3],
            "untrusted_extra": ["x", "y"],
        }
    ).to_parquet(path, index=False)
    observed: dict[str, object] = {}
    original = core.pq.ParquetFile.iter_batches

    def tracked_iter_batches(self, *args, **kwargs):
        observed["columns"] = kwargs.get("columns")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(core.pq.ParquetFile, "iter_batches", tracked_iter_batches)

    loaded = core._load_ohlcv_df("EURUSD", "1h")

    assert loaded is not None
    assert observed["columns"] == ["Timestamp", "Open", "HIGH", "low", "Close", "Volume"]
    assert list(loaded.columns) == core._OHLCV_COLUMNS
    assert len(loaded) == 2
    assert "untrusted_extra" not in loaded.columns
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_rejects_mixed_case_schema_missing_canonical_column(
    monkeypatch, tmp_path
):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "MIXED" / "1h.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "Timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
        }
    ).to_parquet(path, index=False)

    def fail_iter_batches(*_args, **_kwargs):
        raise AssertionError("incomplete mixed-case parquet must be rejected before batching")

    monkeypatch.setattr(core.pq.ParquetFile, "iter_batches", fail_iter_batches)

    assert core._load_ohlcv_df("MIXED", "1h") is None
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_defers_footer_reads_to_signature_cache_misses(
    monkeypatch, tmp_path
):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "INCOMPLETE" / "1h.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "open": [1.0, 1.1],
            "high": [1.2, 1.3],
            "low": [0.9, 1.0],
            "close": [1.1, 1.2],
        }
    ).to_parquet(path, index=False)

    assert core._load_ohlcv_df("INCOMPLETE", "1h") is None

    def fail_row_count(*_args, **_kwargs):
        raise AssertionError("parquet footer must not be read for an unchanged signature")

    monkeypatch.setattr(core, "_parquet_row_count", fail_row_count)

    assert core._load_ohlcv_df("INCOMPLETE", "1h") is None
    core._OHLCV_DF_CACHE.clear()


def test_viewer_ohlcv_loader_rechecks_signature_when_source_changes(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core._OHLCV_DF_CACHE.clear()
    path = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    _make_parquet(path)

    first = core._load_ohlcv_df("EURUSD", "1h")

    assert first is not None
    assert len(first) == 4

    footer_reads: list[str] = []
    parquet_row_count = core._parquet_row_count

    def tracked_row_count(target):
        footer_reads.append(str(target))
        return parquet_row_count(target)

    monkeypatch.setattr(core, "_parquet_row_count", tracked_row_count)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="1h"),
            "open": [1.0] * 6,
            "high": [1.1] * 6,
            "low": [0.9] * 6,
            "close": [1.05] * 6,
            "volume": [10] * 6,
        }
    ).to_parquet(path, index=False)

    second = core._load_ohlcv_df("EURUSD", "1h")

    assert footer_reads == [str(path)]
    assert second is not None
    assert len(second) == 6
    core._OHLCV_DF_CACHE.clear()


def test_task_routes_reject_invalid_task_ids(viewer_client):
    import viewer.main as viewer_app_module

    for task_id in ("short", "bad_id", "a" * 65):
        response = viewer_client.get(f"/api/data/tasks/{task_id}")
        assert response.status_code == 400
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        viewer_app_module._validated_task_id("../escape")
    assert exc_info.value.status_code == 400


def test_viewer_ohlcv_loader_rejects_row_budget_before_materialization(monkeypatch, tmp_path):
    from viewer import core

    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "_OHLCV_MAX_ROWS", 3)
    core._OHLCV_DF_CACHE.clear()
    _make_parquet(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet")
    def fail_materialization(*_args):
        raise AssertionError("rows were materialized")

    monkeypatch.setattr(core, "_read_parquet_frame_bounded", fail_materialization)

    assert core._load_ohlcv_df("EURUSD", "1h") is None


def test_failed_maintenance_rollback_retains_journal_and_requires_manual_cleanup(
    tmp_path,
    monkeypatch,
):
    import viewer.main as viewer_app_module

    artifact = tmp_path / "asset-1.json"
    artifact.write_text("payload", encoding="utf-8")
    journal = viewer_app_module._maintenance_journal_path(artifact)
    staged = viewer_app_module._maintenance_staged_path(artifact)
    real_replace = viewer_app_module.os.replace

    def fail_restore(source, destination):
        if Path(source) == staged and Path(destination) == artifact:
            raise OSError("restore failed")
        return real_replace(source, destination)

    monkeypatch.setattr(viewer_app_module.os, "replace", fail_restore)

    def fail_database_delete(_asset_id):
        raise OSError("database delete failed")

    result = viewer_app_module._delete_asset_with_artifacts(
        "asset-1",
        kind="portfolio",
        artifact_path=artifact,
        db_delete=fail_database_delete,
    )

    assert result["status"] == "cleanup_pending"
    assert result["manual_required"] is True
    assert result["recovery_required"] is True
    assert journal.exists()
    assert staged.exists()
    assert not artifact.exists()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rollback_failed"
    assert not viewer_app_module._recover_maintenance_delete_journal(
        journal,
        kind="portfolio",
        expected_path=artifact,
        db_delete=lambda _asset_id: True,
    )
    assert journal.exists()
