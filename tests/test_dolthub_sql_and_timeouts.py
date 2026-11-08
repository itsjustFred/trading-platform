"""DV-B27/B28: dolt subprocesses time out and SQL is never interpolated raw.

Every ``dolt`` invocation goes through :func:`data_tools.dolthub_manager.run_dolt`
which enforces a default timeout (env-overridable via ``DOLT_TIMEOUT_SECONDS``)
and raises :class:`data_tools.dolthub_manager.DoltTimeoutError` on expiry.
SQL statements are built with :mod:`shared.sql` helpers — never by raw
f-string interpolation of symbols/tables.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import data_tools.dolthub_manager as dm
from data_tools.dolthub_manager import (
    DoltTimeoutError,
    build_ohlcv_select,
    build_symbol_select,
    run_dolt,
)
from shared.sql import quote_identifier, quote_literal


class TestSqlQuotingHelpers:
    def test_quote_identifier_wraps_and_escapes(self) -> None:
        assert quote_identifier("ohlcv") == "`ohlcv`"
        assert quote_identifier("we`ird") == "`we``ird`"

    def test_quote_identifier_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            quote_identifier("")

    def test_quote_literal_rejects_quotes(self) -> None:
        with pytest.raises(ValueError):
            quote_literal("AAPL'; DROP TABLE ohlcv; --")

    def test_quote_literal_accepts_plain_symbol(self) -> None:
        assert quote_literal("AAPL") == "'AAPL'"


class TestSqlBuilders:
    def test_build_symbol_select_quotes_table(self) -> None:
        sql = build_symbol_select("ohlcv")
        assert sql == "SELECT DISTINCT act_symbol FROM `ohlcv`"

    def test_build_symbol_select_escapes_table_identifier(self) -> None:
        sql = build_symbol_select("we`ird")
        assert sql == "SELECT DISTINCT act_symbol FROM `we``ird`"

    def test_build_symbol_select_filters_when_a_symbol_is_given(self) -> None:
        """Omitting the symbol is the every-symbol listing; passing one filters
        and quotes the literal rather than interpolating it raw (DV-B27)."""
        assert build_symbol_select("ohlcv", "AAPL") == (
            "SELECT DISTINCT act_symbol FROM `ohlcv` WHERE act_symbol = 'AAPL'"
        )

    def test_build_symbol_select_rejects_an_unsafe_symbol(self) -> None:
        with pytest.raises(ValueError):
            build_symbol_select("ohlcv", "AAPL' OR 1=1 --")

    def test_build_ohlcv_select_shape(self) -> None:
        sql = build_ohlcv_select("ohlcv", "AAPL")
        assert sql == (
            "SELECT date, open, high, low, close, volume FROM `ohlcv` "
            "WHERE act_symbol = 'AAPL' ORDER BY date"
        )

    def test_build_ohlcv_select_rejects_bad_symbol(self) -> None:
        with pytest.raises(ValueError):
            build_ohlcv_select("ohlcv", "AAPL'--")

    def test_list_symbols_queries_every_symbol_without_filter(
        self, monkeypatch
    ) -> None:
        """Regression: _list_symbols must not pass a symbol filter.

        Routing a wildcard through :func:`shared.sql.quote_literal` raised
        ``ValueError`` before the ``dolt`` call and broke stock export.
        """
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, 0, stdout="act_symbol\nAAPL\nMSFT\n", stderr=""
            )

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        assert dm._list_symbols("/tmp/repo", "ohlcv") == ["AAPL", "MSFT"]
        query = captured["cmd"][captured["cmd"].index("-q") + 1]
        assert query == "SELECT DISTINCT act_symbol FROM `ohlcv`"

    def test_no_raw_interpolated_sql_in_manager_source(self) -> None:
        """Guard against regression to f-string SQL in dolthub_manager."""
        import inspect

        source = inspect.getsource(dm)
        assert "FROM {table_name}" not in source
        assert "act_symbol='" not in source


class TestDolthubDownloaderSql:
    """DV-B27 for the DoltHub API downloader (same injection class)."""

    def _make_downloader(self):
        from data_tools.downloaders.dolthub_dl import DolthubDownloader

        return DolthubDownloader()

    def test_default_query_quotes_table_and_symbol(self) -> None:
        dl = self._make_downloader()
        # Capture the query handed to the API by stubbing requests.get.
        import data_tools.downloaders.dolthub_dl as mod

        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"rows": [], "schema": []}

        def fake_get(url, params=None, timeout=None):
            captured["q"] = params["q"]
            return FakeResponse()

        monkey = pytest.MonkeyPatch()
        monkey.setattr(mod.requests, "get", fake_get)
        try:
            with pytest.raises(RuntimeError, match="No data returned"):
                dl.download("AAPL", "1d", Path("/tmp/dl-out"), start="2024-01-01")
        finally:
            monkey.undo()
        q = captured["q"]
        assert "FROM `ohlcv`" in q
        assert "act_symbol = 'AAPL'" in q

    def test_default_query_rejects_symbol_injection(self) -> None:
        dl = self._make_downloader()
        with pytest.raises(ValueError):
            dl.download(
                "AAPL'; DROP TABLE ohlcv; --", "1d", Path("/tmp/dl-out")
            )

    def test_default_query_rejects_bad_date_bound(self) -> None:
        dl = self._make_downloader()
        with pytest.raises(ValueError, match="date bound"):
            dl.download(
                "AAPL", "1d", Path("/tmp/dl-out"), start="2024-01-01'; --"
            )


class TestDoltTimeout:
    def test_run_dolt_passes_timeout_to_subprocess(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        run_dolt(["dolt", "sql", "-q", "SHOW TABLES"], cwd=None)
        assert seen["timeout"] == dm.DEFAULT_DOLT_TIMEOUT_SECONDS

    def test_run_dolt_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("DOLT_TIMEOUT_SECONDS", "7")

        def fake_run(cmd, **kwargs):
            assert kwargs["timeout"] == 7
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        run_dolt(["dolt", "status"])

    def test_run_dolt_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("DOLT_TIMEOUT_SECONDS", "not-a-number")

        def fake_run(cmd, **kwargs):
            assert kwargs["timeout"] == dm.DEFAULT_DOLT_TIMEOUT_SECONDS
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        run_dolt(["dolt", "status"])

    def test_run_dolt_timeout_raises_dolt_timeout_error(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        with pytest.raises(DoltTimeoutError):
            run_dolt(["dolt", "pull"])

    def test_dolt_timeout_error_is_runtime_error(self) -> None:
        assert issubclass(DoltTimeoutError, RuntimeError)

    def test_list_tables_reports_timeout_as_error(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        assert dm._list_tables("/tmp/whatever") == []

    def test_export_table_csv_times_out(self, monkeypatch, tmp_path) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        out = tmp_path / "out.csv"
        with pytest.raises(DoltTimeoutError):
            dm._export_table_csv(tmp_path, "ohlcv", out)
