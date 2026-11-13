"""Tests for validation.cli."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shared.models import Trade, TradeExit
from validation import cli as validation_cli
from validation.reference import ValidationReference


def _make_sample_trade(
    trade_id: int = 1,
    entry_time: str = "2024-01-01T10:00:00Z",
    pnl: float = 50.0,
    direction: str = "LONG",
) -> Trade:
    return Trade(
        id=trade_id,
        symbol="EURUSD",
        direction=direction,
        entry_time=entry_time,
        entry_price=1.1000,
        quantity=1000.0,
        exits=[TradeExit(time="2024-01-01T11:00:00Z", price=1.1050, quantity=1000.0)],
        pnl=pnl,
        stop_loss=1.0000,
        take_profit=1.2000,
    )


def _make_reference(**overrides: Any) -> ValidationReference:
    defaults = {
        "strategy_name": "test_strategy",
        "source_framework": "mt5_xlsx",
        "symbol": "EURUSD",
        "timeframe": "1h",
        "settings": {"period": 14},
        "trades": [_make_sample_trade()],
        "stats": {"total_trades": 1, "win_rate": 100.0},
        "raw_metadata": {"sheet": "Report"},
        "equity_curve": [
            {"time": "2024-01-01T00:00:00Z", "equity": 100_000.0},
            {"time": "2024-01-01T01:00:00Z", "equity": 100_050.0},
        ],
    }
    defaults.update(overrides)
    return ValidationReference(**defaults)


def _invoke_cli(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "validation.cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )


@pytest.fixture
def tmp_reference_json(tmp_path: Path) -> Path:
    ref = _make_reference()
    path = tmp_path / "reference.json"
    path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    return path


@pytest.fixture
def tmp_platform_json(tmp_path: Path) -> Path:
    ref = _make_reference()
    path = tmp_path / "platform.json"
    path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    return path


class TestCliHelp:
    def test_cli_help(self):
        result = _invoke_cli(["--help"])
        assert result.returncode == 0
        assert "usage:" in result.stdout.lower()
        assert "--reference" in result.stdout
        assert "--platform" in result.stdout
        assert "quantconnect" in result.stdout


class TestCliAutoImporterFallback:
    def test_auto_fallback_forwards_timeframe(self, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "result.json"
        source.write_text("{}", encoding="utf-8")
        seen: dict[str, Any] = {}

        class _Importer:
            def load(self, path: Path, **kwargs: Any) -> ValidationReference:
                seen["path"] = path
                seen.update(kwargs)
                return _make_reference()

        monkeypatch.setattr(validation_cli, "QuantConnectResultImporter", _Importer)

        result = validation_cli._load_validation_reference(source, "auto", timeframe="5m")

        assert result.timeframe == "1h"
        assert seen["path"] == source
        assert seen["timeframe"] == "5m"

    def test_auto_fallback_reports_both_parse_errors(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        source = tmp_path / "result.json"
        source.write_text("{}", encoding="utf-8")
        fallback_error = ValueError("fallback exploded")

        class _Importer:
            def load(self, path: Path, **kwargs: Any) -> ValidationReference:
                raise fallback_error

        monkeypatch.setattr(validation_cli, "QuantConnectResultImporter", _Importer)

        with pytest.raises(ValueError) as exc_info:
            validation_cli._load_validation_reference(source, "auto", timeframe="5m")

        message = str(exc_info.value)
        assert "canonical ValidationReference" in message
        assert "fallback exploded" in message
        assert exc_info.value.__cause__ is fallback_error

    def test_cli_capital_resolver_uses_settings_before_metadata(self) -> None:
        reference = _make_reference(
            settings={"initial_capital": 12_345},
            raw_metadata={"parsed_meta": {"initial_capital": 67_890}},
        )

        assert validation_cli._initial_capital(reference) == pytest.approx(12_345.0)


class TestCliJsonOutput:
    def test_cli_json_output(self, tmp_reference_json: Path, tmp_platform_json: Path):
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(tmp_reference_json),
            "--format", "json",
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["strategy_name"] == "test_strategy"
        assert data["passed"] is True
        assert data["trade_passed"] is True
        assert data["stat_passed"] is True


class TestCliMarkdownOutput:
    def test_cli_markdown_output(self, tmp_reference_json: Path, tmp_platform_json: Path):
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(tmp_reference_json),
            "--format", "markdown",
        ])
        assert result.returncode == 0
        assert "# Validation Report: test_strategy" in result.stdout
        assert (
            "Overall Passed**:** Yes" in result.stdout
            or "Overall Passed**: Yes" in result.stdout
        )


class TestCliMissingTradeExitCode1:
    def test_cli_missing_trade_exit_code_1(self, tmp_reference_json: Path, tmp_path: Path):
        platform = _make_reference(trades=[], allow_empty=True)
        platform_path = tmp_path / "platform_empty.json"
        platform_path.write_text(platform.model_dump_json(indent=2), encoding="utf-8")

        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(platform_path),
            "--format", "json",
        ])
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["passed"] is False
        assert data["trade_summary"]["missing"] == 1


class TestCliOutputToFile:
    def test_cli_output_to_file(
        self, tmp_reference_json: Path, tmp_platform_json: Path, tmp_path: Path
    ):
        out_path = tmp_path / "report.json"
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(tmp_reference_json),
            "--format", "json",
            "--output", str(out_path),
        ])
        assert result.returncode == 0
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["passed"] is True


class TestCliAutoImporterXLSX:
    def test_cli_auto_importer_xlsx(self, tmp_path: Path):
        pytest.importorskip("openpyxl")
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active

        # Header meta rows
        ws.append(["Expertenprogramm:", "", "", "TestStrategy"])
        ws.append(["Symbol:", "", "", "EURUSD"])
        ws.append(["Periode:", "", "", "H1 (1 hour)"])
        ws.append(["Ersteinlage:", "", "", "10000"])
        ws.append(["Hebel:", "", "", "1:100"])
        ws.append([])
        ws.append(["Orders"])
        ws.append(["Auftrag", "S / L", "T / P"])
        ws.append(["1", "1.0900", "1.1200"])
        ws.append([])
        ws.append(["Trades"])
        ws.append([
            "Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen",
            "Preis", "Auftrag", "Kommission", "Swap", "Gewinn", "Kommentar",
        ])
        ws.append([
            "2024.01.01 10:00:00", "1", "EURUSD", "buy", "in",
            "0.1", "1.10000", "1", "0", "0", "0", "entry",
        ])
        ws.append([
            "2024.01.01 11:00:00", "1", "EURUSD", "sell", "out",
            "0.1", "1.10500", "1", "0", "0", "50.00", "tp 1",
        ])

        ref_path = tmp_path / "report.xlsx"
        wb.save(ref_path)

        # Use the same file as both reference and platform
        result = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(ref_path),
            "--format", "json",
            "--importer", "auto",
        ])
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["strategy_name"] == "TestStrategy"
        assert data["passed"] is False
        assert data["equity_evidence_status"] == "missing"
        assert data["readiness"] is not None


class TestCliExitCode0OnPerfectMatch:
    def test_cli_exit_code_0_on_perfect_match(
        self, tmp_reference_json: Path, tmp_platform_json: Path
    ):
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(tmp_reference_json),
            "--format", "json",
        ])
        assert result.returncode == 0

    def test_cli_runs_readiness_without_golden_and_fails_closed(
        self, tmp_reference_json: Path, tmp_platform_json: Path
    ) -> None:
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--format", "json",
        ])

        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["readiness"] is not None
        assert data["readiness"]["level"] == "NOT_READY"
        assert data["readiness"]["passed"] is False

    def test_cli_runs_readiness_when_golden_is_supplied(
        self, tmp_reference_json: Path, tmp_platform_json: Path, tmp_path: Path
    ):
        golden_path = tmp_path / "golden.json"
        golden_path.write_text(tmp_reference_json.read_text(encoding="utf-8"), encoding="utf-8")
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(golden_path),
            "--format", "json",
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["readiness"]["level"] == "READY"
        assert data["readiness"]["passed"] is True


class TestCliTimeToleranceArgument:
    def test_cli_time_tolerance_argument(self, tmp_path: Path):
        ref = _make_reference(
            trades=[_make_sample_trade(entry_time="2024-01-01T10:00:00Z")]
        )
        platform = _make_reference(
            trades=[_make_sample_trade(entry_time="2024-01-01T10:00:05Z")]
        )
        ref_path = tmp_path / "ref.json"
        platform_path = tmp_path / "platform.json"
        ref_path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
        platform_path.write_text(platform.model_dump_json(indent=2), encoding="utf-8")

        # Without tolerance, times differ by 5s -> mismatch
        result_no_tol = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(platform_path),
            "--format", "json",
            "--time-tolerance", "0",
        ])
        assert result_no_tol.returncode == 1

        # With 10s tolerance, should match
        result_with_tol = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(platform_path),
            "--readiness-golden", str(ref_path),
            "--format", "json",
            "--time-tolerance", "10",
        ])
        assert result_with_tol.returncode == 0
        data = json.loads(result_with_tol.stdout)
        assert data["passed"] is True


class TestCliInvalidReferencePathExitCode2:
    def test_cli_invalid_reference_path_exit_code_2(self, tmp_path: Path):
        bad_ref = tmp_path / "nonexistent.json"
        platform = _make_reference()
        platform_path = tmp_path / "platform.json"
        platform_path.write_text(platform.model_dump_json(indent=2), encoding="utf-8")

        result = _invoke_cli([
            "--reference", str(bad_ref),
            "--platform", str(platform_path),
            "--format", "json",
        ])
        assert result.returncode == 2
        assert (
            "not found" in result.stderr.lower()
            or "failed to load reference" in result.stderr.lower()
            or "no such file or directory" in result.stderr.lower()
        )


class TestCliStatsToleranceArgument:
    def test_cli_stats_tolerance_argument(self, tmp_path: Path):
        ref_trade = _make_sample_trade()
        ref_trade.exits[0].pnl = 100.0
        ref = _make_reference(trades=[ref_trade])
        plat_trade = _make_sample_trade()
        plat_trade.exits[0].pnl = 200.0
        platform = _make_reference(trades=[plat_trade])

        ref_path = tmp_path / "ref.json"
        platform_path = tmp_path / "platform.json"
        ref_path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
        platform_path.write_text(platform.model_dump_json(indent=2), encoding="utf-8")

        # With strict stats tolerance (0.001), different PnL should fail
        result_strict = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(platform_path),
            "--format", "json",
            "--stats-tolerance", "0.001",
            "--pnl-absolute-tolerance", "0.01",
        ])
        assert result_strict.returncode == 1
        data_strict = json.loads(result_strict.stdout)
        assert data_strict["stat_passed"] is False

        # With both tolerances loosened, should pass. The stat gate requires
        # every computed tolerance to hold (DV-B35), so the absolute band is
        # widened here as well; loosening only the relative band must not be
        # enough on its own.
        result_loose = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(platform_path),
            "--readiness-golden", str(ref_path),
            "--format", "json",
            "--stats-tolerance", "1.0",
            "--pnl-absolute-tolerance", "1000.0",
        ])
        assert result_loose.returncode == 0
        data_loose = json.loads(result_loose.stdout)
        assert data_loose["stat_passed"] is True

    def test_loose_relative_tolerance_alone_does_not_pass(self, tmp_path: Path):
        """DV-B35: the abs tolerance must not be bypassed by a loose rel band."""
        ref_trade = _make_sample_trade()
        ref_trade.exits[0].pnl = 100.0
        ref = _make_reference(trades=[ref_trade])
        plat_trade = _make_sample_trade()
        plat_trade.exits[0].pnl = 200.0
        platform = _make_reference(trades=[plat_trade])

        ref_path = tmp_path / "ref.json"
        platform_path = tmp_path / "platform.json"
        ref_path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
        platform_path.write_text(platform.model_dump_json(indent=2), encoding="utf-8")

        result = _invoke_cli([
            "--reference", str(ref_path),
            "--platform", str(platform_path),
            "--format", "json",
            "--stats-tolerance", "1.0",
            "--pnl-absolute-tolerance", "0.01",
        ])
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["stat_passed"] is False


class TestCliHtmlImporter:
    def test_cli_html_importer(self, tmp_path: Path):
        pytest.importorskip("bs4")
        html_content = """<!DOCTYPE html>
<html>
<head><title>Report</title></head>
<body>
<table>
  <tr><td>Expertenprogramm:</td><td>HtmlStrategy</td></tr>
  <tr><td>Symbol:</td><td>EURUSD</td></tr>
  <tr><td>Periode:</td><td>H1 (1 hour)</td></tr>
  <tr><td>Ersteinlage:</td><td>10000</td></tr>
</table>
<h2>Orders</h2>
<table>
  <thead><tr><th>Auftrag</th><th>S / L</th><th>T / P</th></tr></thead>
  <tbody><tr><td>1</td><td>1.0900</td><td>1.1200</td></tr></tbody>
</table>
<h2>Trades</h2>
<table>
  <thead>
    <tr>
      <th>Zeit</th><th>Trade</th><th>Symbol</th><th>Typ</th><th>Richtung</th>
      <th>Volumen</th><th>Preis</th><th>Auftrag</th><th>Kommission</th>
      <th>Swap</th><th>Gewinn</th><th>Kommentar</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>2024.01.01 10:00:00</td><td>1</td><td>EURUSD</td><td>buy</td><td>in</td>
      <td>0.1</td><td>1.10000</td><td>1</td><td>0</td><td>0</td><td>0</td><td></td>
    </tr>
    <tr>
      <td>2024.01.01 11:00:00</td><td>1</td><td>EURUSD</td><td>sell</td><td>out</td>
      <td>0.1</td><td>1.10500</td><td>1</td><td>0</td><td>0</td><td>50.00</td><td>tp 1</td>
    </tr>
  </tbody>
</table>
</body>
</html>"""
        html_path = tmp_path / "report.html"
        html_path.write_text(html_content, encoding="utf-8")

        result = _invoke_cli([
            "--reference", str(html_path),
            "--platform", str(html_path),
            "--format", "json",
            "--importer", "mt5_html",
        ])
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["strategy_name"] == "HtmlStrategy"
        assert data["passed"] is False
        assert data["equity_evidence_status"] == "missing"


class TestCliStdinStdoutBehavior:
    def test_cli_default_output_is_stdout(self, tmp_reference_json: Path, tmp_platform_json: Path):
        result = _invoke_cli([
            "--reference", str(tmp_reference_json),
            "--platform", str(tmp_platform_json),
            "--readiness-golden", str(tmp_reference_json),
        ])
        assert result.returncode == 0
        # Default format is markdown, so verify markdown output on stdout
        assert "# Validation Report" in result.stdout
