"""Tests for tools/mt5_ci/inspect_report.py.

The CI job trusts the report only after this assertion passes: a report can
exist and look normal while naming the stock Moving Average sample, the wrong
symbol, or zero trades. These cases are pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "mt5_ci" / "inspect_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mt5_ci_inspect_report", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DEALS_HEADER = (
    "<tr><th>Time</th><th>Deal</th><th>Symbol</th><th>Type</th><th>Direction</th>"
    "<th>Volume</th><th>Price</th><th>Order</th><th>Commission</th><th>Swap</th>"
    "<th>Profit</th><th>Balance</th><th>Comment</th></tr>"
)
DEALS_ROWS = (
    "<tr><td>2026.02.24 16:30:00</td><td>2</td><td>EURUSD_CI</td><td>buy</td><td>in</td>"
    "<td>0.10</td><td>1.17000</td><td>2</td><td>0.00</td><td>0.00</td><td>0.00</td>"
    "<td>100000.00</td><td></td></tr>"
    "<tr><td>2026.02.24 22:55:00</td><td>3</td><td>EURUSD_CI</td><td>sell</td><td>out</td>"
    "<td>0.10</td><td>1.17200</td><td>3</td><td>0.00</td><td>0.00</td><td>20.00</td>"
    "<td>100020.00</td><td>tp 1.17200</td></tr>"
)


def _report(
    expert: str = "IntradayVolatilityBreakout",
    symbol: str = "EURUSD_CI",
    trades: str = "1",
    german: bool = False,
    include_deals: bool = True,
) -> str:
    expert_label = "Expertenprogramm:" if german else "Expert:"
    symbol_label = "Symbol:"
    period_label = "Periode:" if german else "Period:"
    deposit_label = "Ersteinlage:" if german else "Initial Deposit:"
    trades_label = "Gesamtzahl Trades:" if german else "Total Trades:"
    deals_header = DEALS_HEADER
    if german:
        deals_header = (
            "<tr><th>Zeit</th><th>Deal</th><th>Symbol</th><th>Typ</th><th>Richtung</th>"
            "<th>Volumen</th><th>Preis</th><th>Auftrag</th><th>Kommission</th><th>Swap</th>"
            "<th>Gewinn</th><th>Kontostand</th><th>Kommentar</th></tr>"
        )
    deals_table = (
        f"<table>{deals_header}{DEALS_ROWS}</table>" if include_deals else ""
    )
    return (
        "<html><head><title>Strategy Tester</title></head><body>"
        "<table>"
        f"<tr><td>{expert_label}</td><td>{expert}</td></tr>"
        f"<tr><td>{symbol_label}</td><td>{symbol}</td></tr>"
        f"<tr><td>{period_label}</td><td>H1 (2026.02.20 - 2026.04.02)</td></tr>"
        f"<tr><td>{deposit_label}</td><td>100 000.00</td></tr>"
        f"<tr><td>{trades_label}</td><td>{trades}</td></tr>"
        "</table>"
        f"{deals_table}"
        "</body></html>"
    )


def _write(path: Path, html: str, utf16: bool = False) -> Path:
    if utf16:
        path.write_bytes(b"\xff\xfe" + html.encode("utf-16-le"))
    else:
        path.write_text(html, encoding="utf-8")
    return path


def test_english_report_summary(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report())

    summary = module.summarise(path)

    assert summary["expert"] == "IntradayVolatilityBreakout"
    assert summary["symbol"] == "EURUSD_CI"
    assert summary["period"].startswith("H1")
    assert summary["total_trades_value"] == 1.0
    assert summary["deal_rows"] == 2
    assert [deal["direction"] for deal in summary["deals"]] == ["in", "out"]
    assert summary["deals"][1]["price"] == 1.172
    assert summary["deals"][1]["profit"] == 20.0
    assert summary["deals"][1]["comment"] == "tp 1.17200"
    assert module.check(summary, "IntradayVolatilityBreakout", "EURUSD_CI", 1) == []


def test_german_report_summary(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(german=True))

    summary = module.summarise(path)

    assert summary["expert"] == "IntradayVolatilityBreakout"
    assert summary["total_trades_value"] == 1.0
    assert module.check(summary, "IntradayVolatilityBreakout", "EURUSD_CI", 1) == []


def test_utf16_report_is_decoded(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(), utf16=True)

    summary = module.summarise(path)
    assert summary["expert"] == "IntradayVolatilityBreakout"


def test_fallback_sample_expert_fails(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(expert="Moving Average"))

    failures = module.check(module.summarise(path), "IntradayVolatilityBreakout", "EURUSD_CI", 1)

    assert any("stock sample" in failure for failure in failures)


def test_wrong_symbol_fails(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(symbol="EURUSD"))

    failures = module.check(module.summarise(path), "IntradayVolatilityBreakout", "EURUSD_CI", 1)

    assert any("symbol" in failure for failure in failures)


def test_zero_trades_fails(tmp_path: Path) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(trades="0", include_deals=False))

    failures = module.check(module.summarise(path), "IntradayVolatilityBreakout", "EURUSD_CI", 1)

    assert any("trades" in failure for failure in failures)
    assert any("deals" in failure for failure in failures)


def test_missing_report_returns_1(tmp_path: Path, capsys) -> None:
    module = _load_module()

    code = module.main(["--report", str(tmp_path / "absent.html"), "--root", str(tmp_path)])

    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cli_writes_json_and_exits_zero(tmp_path: Path, capsys) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report())
    out = tmp_path / "summary.json"

    code = module.main(
        [
            "--report",
            str(path),
            "--expect-expert",
            "IntradayVolatilityBreakout",
            "--expect-symbol",
            "EURUSD_CI",
            "--json-out",
            str(out),
            "--root",
            str(tmp_path),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["expert"] == "IntradayVolatilityBreakout"
    assert payload["deals"]
    assert "OK" in capsys.readouterr().err


def test_cli_exits_nonzero_on_assertion(tmp_path: Path, capsys) -> None:
    module = _load_module()
    path = _write(tmp_path / "report.html", _report(trades="0", include_deals=False))

    code = module.main(["--report", str(path), "--min-trades", "1", "--root", str(tmp_path)])

    assert code == 1
    assert "FAIL" in capsys.readouterr().err


def test_grouped_thousands_are_parsed(tmp_path: Path) -> None:
    module = _load_module()

    assert module.parse_number("100 000.00") == 100000.0
    assert module.parse_number("1.234.567") == 1234567.0
    assert module.parse_number("45.1%") == 45.1
    assert module.parse_number("") is None
    assert module.parse_number("n/a") is None


def test_deals_table_inside_single_report_table(tmp_path: Path) -> None:
    """MT5 writes the whole report as one table; the deals header is a mid-table row."""
    module = _load_module()
    html = (
        "<html><body><table>"
        "<tr><td colspan='13'>Strategy Tester Report</td></tr>"
        "<tr><td colspan='3'>Expert:</td><td colspan='10'>IntradayVolatilityBreakout</td></tr>"
        "<tr><td colspan='3'>Symbol:</td><td colspan='10'>EURUSD_CI</td></tr>"
        "<tr><td colspan='3'>Total Trades:</td><td colspan='10'>1</td></tr>"
        "<tr><td colspan='13'><b>Orders</b></td></tr>"
        "<tr><td>Open Time</td><td>Order</td><td>Symbol</td><td>Type</td><td>Volume</td>"
        "<td>Price</td><td>S / L</td><td>T / P</td><td>Time</td><td>State</td><td>Comment</td></tr>"
        "<tr><td>2026.03.02 16:45:00</td><td>2</td><td>EURUSD_CI</td><td>buy</td><td>4.4</td>"
        "<td>1.17170</td><td>1.17057</td><td></td><td>2026.03.02 16:45:00</td><td>filled</td>"
        "<td>Breakout Long</td></tr>"
        "<tr><td colspan='13'><b>Deals</b></td></tr>"
        "<tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td>"
        "<td>Volume</td><td>Price</td><td>Order</td><td>Commission</td><td>Swap</td>"
        "<td>Profit</td><td>Balance</td><td>Comment</td></tr>"
        "<tr><td>2026.02.20 00:00:00</td><td>1</td><td></td><td>balance</td><td></td>"
        "<td></td><td></td><td></td><td>0.00</td><td>0.00</td><td>0.00</td>"
        "<td>100 000.00</td><td></td></tr>"
        "<tr><td>2026.03.02 16:45:00</td><td>2</td><td>EURUSD_CI</td><td>buy</td><td>in</td>"
        "<td>4.4</td><td>1.17170</td><td>2</td><td>0.00</td><td>0.00</td><td>0.00</td>"
        "<td>100 000.00</td><td>Breakout Long</td></tr>"
        "<tr><td>2026.03.02 17:15:40</td><td>3</td><td>EURUSD_CI</td><td>sell</td><td>out</td>"
        "<td>4.4</td><td>1.17057</td><td>3</td><td>0.00</td><td>0.00</td><td>-497.20</td>"
        "<td>99 502.80</td><td>sl 1.17057</td></tr>"
        "</table></body></html>"
    )
    path = _write(tmp_path / "report.htm", html)

    summary = module.summarise(path)

    assert summary["expert"] == "IntradayVolatilityBreakout"
    assert summary["symbol"] == "EURUSD_CI"
    assert summary["total_trades_value"] == 1.0
    # The balance row is not a deal.
    assert summary["deal_rows"] == 2
    assert [deal["type"] for deal in summary["deals"]] == ["buy", "sell"]
    assert module.check(summary, "IntradayVolatilityBreakout", "EURUSD_CI", 1) == []


def test_cli_refuses_report_outside_allowed_root(tmp_path: Path, capsys) -> None:
    """A crafted --report must not read outside the allowed roots (S8707)."""
    module = _load_module()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = _write(tmp_path / "outside.html", _report())

    code = module.main(["--report", str(outside), "--root", str(allowed)])

    assert code == 2
    assert "outside" in capsys.readouterr().err


def test_cli_refuses_json_out_outside_allowed_root(tmp_path: Path, capsys) -> None:
    """A crafted --json-out must not write outside the allowed roots (S8707)."""
    module = _load_module()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    report = _write(allowed / "report.html", _report())
    outside = tmp_path / "escaped" / "summary.json"

    code = module.main(
        ["--report", str(report), "--json-out", str(outside), "--root", str(allowed)]
    )

    assert code == 2
    assert "outside" in capsys.readouterr().err
    assert not outside.exists()


def test_default_root_is_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.chdir(tmp_path)

    roots = module.allowed_roots([])

    assert roots == (tmp_path.resolve(),)
    with pytest.raises(ValueError, match="outside"):
        module.resolve_cli_path(Path("../escape.html"), roots, "--report")


def test_multiple_roots_are_accepted(tmp_path: Path) -> None:
    """The CI job reads the report from the portable MT5 directory."""
    module = _load_module()
    workspace = tmp_path / "workspace"
    terminal = tmp_path / "mt5"
    workspace.mkdir()
    terminal.mkdir()

    roots = module.allowed_roots([str(workspace), str(terminal)])

    assert module.resolve_cli_path(terminal / "report.htm", roots, "--report") == (
        terminal / "report.htm"
    ).resolve()
    assert module.resolve_cli_path(workspace / "summary.json", roots, "--json-out") == (
        workspace / "summary.json"
    ).resolve()
