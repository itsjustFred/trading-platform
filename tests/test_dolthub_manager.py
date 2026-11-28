import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_tools.dolthub_manager as dolthub_manager
from data_tools.dolthub_manager import _stock_export_path
from shared import ohlcv_store


def test_stock_export_path_uses_manifest_store_layout(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(dolthub_manager, "DATA_DIR", data_dir)

    canonical_path = _stock_export_path("AAPL")

    assert canonical_path == ohlcv_store.data_path(data_dir, "dolthub", "AAPL", "1d")
    assert canonical_path == data_dir / "ohlcv" / "dolthub" / "AAPL" / "AAPL_D1.parquet"


def test_export_stocks_skips_symbols_that_fail_symbol_validation(tmp_path, monkeypatch, caplog):
    """_export_stocks must not route symbol values through path helpers before validation."""
    import data_tools.dolthub_manager as dolthub_manager

    stocks_repo = tmp_path / "stocks"
    (stocks_repo / dolthub_manager.DOLT_METADATA_DIR).mkdir(parents=True)
    export_dir = tmp_path / "exports"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(dolthub_manager, "DOLT_DIR", tmp_path)
    monkeypatch.setattr(dolthub_manager, "EXPORT_DIR", export_dir)
    monkeypatch.setattr(dolthub_manager, "DATA_DIR", data_dir)

    monkeypatch.setattr(dolthub_manager, "_stocks_table_name", lambda repo: "ohlcv")
    monkeypatch.setattr(
        dolthub_manager,
        "_list_symbols",
        lambda repo, table: ["AAPL", "../escape", "BAD SYMBOL", ".."],
    )
    exported: list[str] = []

    def _capture_export(repo, table, symbol, target_dir, out_parquet):
        exported.append(symbol)
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        out_parquet.write_text("parquet placeholder")

    monkeypatch.setattr(dolthub_manager, "_export_symbol_csv", _capture_export)

    with caplog.at_level(logging.WARNING, logger="dolthub_manager"):
        dolthub_manager._export_stocks()

    assert exported == ["AAPL"]
    assert (data_dir / "ohlcv" / "dolthub" / "AAPL" / "AAPL_D1.parquet").exists()
    assert not (data_dir / "ohlcv" / "dolthub" / "escape").exists()
    warnings = [record.getMessage() for record in caplog.records]
    assert any("../escape" in message for message in warnings)
    assert any("BAD SYMBOL" in message for message in warnings)
    assert any("'..'" in message for message in warnings)


def test_export_data_skips_unsafe_symbols_before_path_construction(tmp_path, monkeypatch):
    stocks_repo = tmp_path / "dolthub" / "stocks"
    (stocks_repo / dolthub_manager.DOLT_METADATA_DIR).mkdir(parents=True)
    monkeypatch.setattr(dolthub_manager, "DOLT_DIR", tmp_path / "dolthub")
    monkeypatch.setattr(dolthub_manager, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(dolthub_manager, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(dolthub_manager, "_list_tables", lambda _repo: ["ohlcv"])
    monkeypatch.setattr(
        dolthub_manager,
        "run_dolt",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="act_symbol\n../escape\nBAD SYMBOL\n..\n"
        ),
    )

    dolthub_manager.export_data()

    assert not (tmp_path / "data" / "ohlcv" / "dolthub" / "escape").exists()


def test_export_table_csv_keeps_previous_file_when_dolt_fails(tmp_path, monkeypatch):
    output = tmp_path / "ohlcv.csv"
    output.write_text("previous", encoding="utf-8")

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("dolt failed")

    monkeypatch.setattr(dolthub_manager, "run_dolt", fail_run)

    with pytest.raises(RuntimeError, match="dolt failed"):
        dolthub_manager._export_table_csv(tmp_path, "ohlcv", output)

    assert output.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_repo_parquet_export_keeps_previous_file_when_write_fails(tmp_path, monkeypatch):
    repo = tmp_path / "earnings"
    (repo / dolthub_manager.DOLT_METADATA_DIR).mkdir(parents=True)
    output_dir = tmp_path / "exports"
    output_path = output_dir / "earnings.parquet"
    output_dir.mkdir()
    output_path.write_text("previous parquet", encoding="utf-8")

    monkeypatch.setattr(dolthub_manager, "DOLT_DIR", tmp_path)
    monkeypatch.setattr(dolthub_manager, "_list_tables", lambda _repo: ["earnings"])
    monkeypatch.setattr(
        dolthub_manager,
        "_export_table_csv",
        lambda _repo, _table, path: path.write_text(
            "value\n1\n", encoding="utf-8"
        ),
    )
    monkeypatch.setattr(
        dolthub_manager,
        "write_parquet_atomically",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    dolthub_manager._export_repo_tables("earnings", output_dir)

    assert output_path.read_text(encoding="utf-8") == "previous parquet"
    assert not (output_dir / "earnings.csv").exists()


def test_a_failing_stocks_repo_does_not_take_the_fundamentals_exports_with_it(
    tmp_path, monkeypatch
):
    """One broken repo must not cost every other table its export.

    `export_data` inlined the symbol listing instead of calling `_list_symbols`,
    and the inline copy had no `except`. A `DoltTimeoutError` from
    `SELECT DISTINCT act_symbol` - a large table, a transient dolt failure, the
    600s default - escaped the function before `_export_repo_tables("earnings")`
    and `_export_repo_tables("rates")` ran. So a slow `stocks` repo meant *no*
    fundamentals at all, with a traceback and no partial-success report.

    The guard this branch added went to `_list_symbols`, which nothing in
    production called. Hardening the copy that is dead is not hardening.
    """
    import data_tools.dolthub_manager as dolthub_manager

    stocks_repo = tmp_path / "stocks"
    (stocks_repo / dolthub_manager.DOLT_METADATA_DIR).mkdir(parents=True)
    monkeypatch.setattr(dolthub_manager, "DOLT_DIR", tmp_path)
    monkeypatch.setattr(dolthub_manager, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(dolthub_manager, "_stocks_table_name", lambda repo: "ohlcv")

    monkeypatch.setattr(
        dolthub_manager, "_list_tables", lambda repo: ["ohlcv"]
    )

    seen_queries: list[str] = []

    def _only_the_symbol_listing_times_out(args, **_kwargs):
        query = " ".join(str(a) for a in args)
        seen_queries.append(query)
        if "DISTINCT" in query.upper():
            raise dolthub_manager.DoltTimeoutError("dolt exceeded 600s")
        return SimpleNamespace(stdout="table\n", returncode=0)

    # Only the *symbol listing* fails. The first attempt stubbed every `run_dolt`
    # to raise, but `_list_tables` runs first and swallows its own failure, so
    # `table_name` came back None, the whole stocks block was skipped, and the
    # fundamentals exported for a reason that had nothing to do with the code
    # under test. The mutation passed on a test that was never exercising it.
    monkeypatch.setattr(
        dolthub_manager, "run_dolt", _only_the_symbol_listing_times_out
    )

    exported_tables: list[str] = []

    def _capture_tables(name, target):
        exported_tables.append(name)

    monkeypatch.setattr(dolthub_manager, "_export_repo_tables", _capture_tables)

    dolthub_manager.export_data()

    assert any("DISTINCT" in q for q in seen_queries), (
        f"the symbol listing was never reached, so this test proved nothing: "
        f"{seen_queries}"
    )
    assert exported_tables == ["earnings", "rates"], (
        f"a failing stocks symbol listing skipped the fundamentals exports: "
        f"{exported_tables}. Earnings and rates have nothing to do with the "
        f"equity repo, and a traceback is not a partial-success report."
    )
