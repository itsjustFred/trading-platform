"""Matched-open validation for the MT5 reference audit parser.

Regression tests for CodeRabbit cycle-2 finding 1
(``parse_mt5_reference.py`` ``parse_log_trades``): the ``opened @`` line
matched by timestamp must agree with the entry deal in side, open price
and lots — a bare timestamp hit must not silently accept a wrong pair.

Lots comparison is tolerance-based on purpose: the EA logs Lots with a
JS-float repr (``1.6600000000000001``) while deals log ``1.66``.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_AUDIT_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
    / "audit"
    / "parse_mt5_reference.py"
)


def _load_parser():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "parse_mt5_reference_under_test", _AUDIT_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PARSER = _load_parser()

ENTRY_TS = "2018.01.04 16:45:00"
EXIT_TS = "2018.01.04 18:45:40"


def _deal_line(ts: str, ticket: int, side: str, vol: str, price: str) -> str:
    return (
        f"CS\t0\t11:55:35.528\tTrades\t{ts}   deal #{ticket} {side} "
        f"{vol} EURUSD at {price} done (based on order #{ticket})"
    )


def _open_line(ts: str, side: str, price: str, lots: str) -> str:
    return (
        f"CS\t0\t11:55:35.528\tZenomTrader Discord IntradayVolatilityBreakout "
        f"(EURUSD,H1)\t{ts}   {side} opened @ {price} | Lots: {lots} | "
        f"SL: 1.20000 | Breakout"
    )


def _write_log(path: pathlib.Path, lines: list[str]) -> pathlib.Path:
    path.write_bytes("\n".join(lines).encode("utf-16"))
    return path


def _valid_lines(
    open_side: str = "Long",
    open_price: str = "1.20824",
    open_lots: str = "1.6600000000000001",
    deal_vol: str = "1.66",
) -> list[str]:
    return [
        _deal_line(ENTRY_TS, 2, "buy", deal_vol, "1.20824"),
        _deal_line(EXIT_TS, 3, "sell", deal_vol, "1.20643"),
        _open_line(ENTRY_TS, open_side, open_price, open_lots),
    ]


def test_valid_pair_accepted_with_js_float_lots_repr(tmp_path: pathlib.Path) -> None:
    """EA logs Lots=1.6600000000000001 vs deal 1.66 — must pass via tolerance."""
    log = _write_log(tmp_path / "t.log", _valid_lines())
    trades = _PARSER.parse_log_trades(log, 1.0)
    assert len(trades) == 1
    assert trades[0]["dir"] == "LONG"


def test_side_mismatch_rejected(tmp_path: pathlib.Path) -> None:
    log = _write_log(tmp_path / "t.log", _valid_lines(open_side="Short"))
    with pytest.raises(ValueError, match="open side mismatch"):
        _PARSER.parse_log_trades(log, 1.0)


def test_open_price_mismatch_rejected(tmp_path: pathlib.Path) -> None:
    log = _write_log(tmp_path / "t.log", _valid_lines(open_price="1.99999"))
    with pytest.raises(ValueError, match="open price mismatch"):
        _PARSER.parse_log_trades(log, 1.0)


def test_lots_mismatch_rejected(tmp_path: pathlib.Path) -> None:
    log = _write_log(tmp_path / "t.log", _valid_lines(open_lots="2.50"))
    with pytest.raises(ValueError, match="open lots mismatch"):
        _PARSER.parse_log_trades(log, 1.0)
