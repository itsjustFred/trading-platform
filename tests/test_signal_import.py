from __future__ import annotations

from pathlib import Path

from data_tools.signal_import import load_signals_from_csv


def test_load_signals_from_csv_parses_semicolon_stockkit_format(tmp_path: Path) -> None:
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Symbol;Signal Price;Datum;Zeit (NY);Signal Type",
                "AAPL;173,79;2026-02-23;09:32;BUY",
                "TSLA;250,10;2026-02-23;10:07;SELL",
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
