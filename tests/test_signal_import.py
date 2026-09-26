from __future__ import annotations

from pathlib import Path

import pytest

from data_tools.signal_import import load_signals_from_csv


def test_load_signals_from_csv_parses_semicolon_stockkit_format(tmp_path: Path) -> None:
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "\n".join(
            [
                # B3: explicit offsets (09:32-05:00 winter) — naive times
                # are dropped, never NY-guessed.
                "Symbol;Signal Price;Datum;Zeit;Signal Type",
                "AAPL;173,79;2026-02-23;09:32:00-05:00;BUY",
                "TSLA;250,10;2026-02-23;10:07:00-05:00;SELL",
            ]
        ),
        encoding="utf-8",
    )

    signals = load_signals_from_csv(str(csv_path))

    assert len(signals) == 2
    assert signals[0]["symbol"] == "AAPL"
    assert signals[0]["action"] == "buy"
    assert signals[0]["entry_price"] == 173.79
    assert signals[0]["timeframe"] == "1m"
    assert str(signals[0]["source"]).startswith("csv:")
    assert signals[0]["timestamp"].tzinfo is not None
    assert signals[0]["timestamp"].hour == 14  # 09:32 New York -> 14:32 UTC (winter)


def test_load_signals_from_csv_parses_date_only_as_daily(tmp_path: Path) -> None:
    csv_path = tmp_path / "signals_daily.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Symbol,Entry Price,Date,Action",
                "MSFT,410.25,2026-03-01,BUY",
            ]
        ),
        encoding="utf-8",
    )

    signals = load_signals_from_csv(str(csv_path))

    assert len(signals) == 1
    assert signals[0]["symbol"] == "MSFT"
    assert signals[0]["timeframe"] == "1d"
    assert signals[0]["entry_price"] == 410.25


def test_missing_action_column_is_rejected(tmp_path: Path) -> None:
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "Symbol,Entry Price,Date\nMSFT,410.25,2026-03-01\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="action column"):
        load_signals_from_csv(str(csv_path))


def test_blank_action_is_rejected(tmp_path: Path) -> None:
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "Symbol,Entry Price,Date,Action\nMSFT,410.25,2026-03-01,\n",
        encoding="utf-8",
    )

    assert load_signals_from_csv(str(csv_path)) == []


def test_unknown_action_drops_row_instead_of_buying(tmp_path: Path) -> None:
    """B3: unknown actions must never default to buy (invented longs)."""
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Symbol,Entry Price,Date,Action",
                "MSFT,410.25,2026-03-01,HOLD",
                "AAPL,100.00,2026-03-01,BUY",
            ]
        ),
        encoding="utf-8",
    )

    signals = load_signals_from_csv(str(csv_path))

    assert [signal["symbol"] for signal in signals] == ["AAPL"]


def test_naive_intraday_timestamp_drops_row(tmp_path: Path) -> None:
    """B3: naive intraday times without offset are unusable, not NY-guessed."""
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Symbol,Entry Price,Datetime,Action",
                "MSFT,410.25,2026-03-01 09:32:00,BUY",
                "AAPL,100.00,2026-03-01 09:32:00-05:00,BUY",
            ]
        ),
        encoding="utf-8",
    )

    signals = load_signals_from_csv(str(csv_path))

    assert [signal["symbol"] for signal in signals] == ["AAPL"]
    assert signals[0]["timestamp"].hour == 14
