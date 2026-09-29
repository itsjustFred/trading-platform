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
    def test_build_distinct_symbol_select_quotes_table(self) -> None:
        assert dm.build_distinct_symbol_select("ohlcv") == "SELECT DISTINCT act_symbol FROM `ohlcv`"

    def test_build_distinct_symbol_select_rejects_empty_table(self) -> None:
        with pytest.raises(ValueError):
            dm.build_distinct_symbol_select("")

    def test_build_symbol_select_quotes_table_and_symbol(self) -> None:
        sql = build_symbol_select("ohlcv", "AAPL")
        assert sql == "SELECT DISTINCT act_symbol FROM `ohlcv` WHERE act_symbol = 'AAPL'"

    def test_build_symbol_select_escapes_table_identifier(self) -> None:
        sql = build_symbol_select("we`ird")
        assert sql == "SELECT DISTINCT act_symbol FROM `we``ird`"

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


class TestStocksExportHelpersSql:
    """The extracted ``_export_stocks`` helpers must use the safe builders."""

    def test_list_symbols_quotes_table_and_runs_under_timeout(self, monkeypatch, tmp_path) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="act_symbol\nAAPL\nMSFT\n", stderr="")

        monkeypatch.setattr(dm.subprocess, "run", fake_run)

        assert dm._list_symbols(tmp_path, "ohlcv") == ["AAPL", "MSFT"]
        assert seen["cmd"] == [
            "dolt",
            "sql",
            "-q",
            "SELECT DISTINCT act_symbol FROM `ohlcv`",
            "-r",
            "csv",
        ]
        assert seen["timeout"] == dm.DEFAULT_DOLT_TIMEOUT_SECONDS

    def test_list_symbols_timeout_is_not_fatal(self, monkeypatch, tmp_path) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        assert dm._list_symbols(tmp_path, "ohlcv") == []

    def test_export_symbol_csv_quotes_table_and_symbol(self, monkeypatch, tmp_path) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["timeout"] = kwargs["timeout"]
            if kwargs.get("stdout") is not None:
                kwargs["stdout"].write("date,open,high,low,close,volume\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(dm.subprocess, "run", fake_run)
        dm._export_symbol_csv(tmp_path, "ohlcv", "AAPL", tmp_path, tmp_path / "AAPL" / "1d.parquet")

        assert seen["cmd"][3] == (
            "SELECT date, open, high, low, close, volume FROM `ohlcv` "
            "WHERE act_symbol = 'AAPL' ORDER BY date"
        )
        assert seen["timeout"] == dm.DEFAULT_DOLT_TIMEOUT_SECONDS


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


def test_no_sql_string_interpolates_an_unquoted_value() -> None:
    """The rule the module already documents, enforced independently of syntax.

    `test_no_raw_interpolated_sql_in_manager_source` denies two literal strings.
    That is a denylist of *spellings*, and measured against it:

    - an f-string using `FROM {table_name}` -> caught (first literal)
    - an f-string with spaces, `act_symbol = '{symbol}'` -> caught (first literal)
    - a `.format()` call -> caught (second literal, `act_symbol='`)
    - **the same injection written with `%` formatting -> NOT caught**

    The last one is the finding. `%s` and `FROM %s` match neither literal, and
    the query is exactly as injectable as the ones the guard does catch. A
    denylist of spellings cannot cover a rule about values, because there are
    four ways to interpolate in Python and only two were named.

    The module already states the rule it means: the table name goes through
    `quote_identifier` and a given symbol through `quote_literal`, both of which
    validate and raise on anything unsafe. So this asserts the rule directly -
    every value interpolated into a SQL string must have been through one of
    those helpers - across f-strings, `%` and `.format()` alike.
    """
    import ast
    import inspect

    source = inspect.getsource(dm)
    tree = ast.parse(source)

    quoting_helpers = {"quote_identifier", "quote_literal"}
    sql_verbs = ("SELECT", "INSERT", "UPDATE", "DELETE", "FROM", "WHERE")

    def _looks_like_sql(text: str) -> bool:
        upper = text.upper()
        return any(verb in upper for verb in sql_verbs)

    def _is_quoted(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and (
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        ) in quoting_helpers

    offenders: list[str] = []

    for node in ast.walk(tree):
        interpolated: list[tuple[str, ast.AST]] = []

        if isinstance(node, ast.JoinedStr):
            # f-string: the literal chunks tell us whether this is SQL, and the
            # FormattedValue nodes are the values spliced into it.
            literal_text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if not _looks_like_sql(literal_text):
                continue
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    interpolated.append(("f-string", part.value))

        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            # `"... %s ..." % (a, b)`
            left = node.left
            if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                continue
            if not _looks_like_sql(left.value):
                continue
            right = node.right
            targets = (
                list(right.elts)
                if isinstance(right, ast.Tuple)
                else [right]
            )
            for target in targets:
                interpolated.append(("%-format", target))

        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "format"
            and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str)
            and _looks_like_sql(node.func.value.value)
        ):
            for arg in node.args:
                interpolated.append((".format()", arg))

        for mechanism, value in interpolated:
            if not _is_quoted(value):
                offenders.append(
                    f"line {getattr(value, 'lineno', '?')}: {mechanism} interpolates "
                    f"{ast.dump(value)[:60]} into a SQL string without going through "
                    f"{sorted(quoting_helpers)}"
                )

    assert not offenders, (
        "these SQL strings interpolate a value that was never quoted, so a table "
        "name or symbol containing a quote or a semicolon reaches dolt verbatim:\n  "
        + "\n  ".join(offenders)
    )
