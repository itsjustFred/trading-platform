from pathlib import Path

import pytest

from scripts.analyze_mt5_parity import (
    MT5_REPORTS,
    _broker_clock,
    _direction_counts,
    _load_reference,
    _match_by_entry_and_direction,
    _mt5_exit_reason,
    _platform_exit_reason,
    _series_stats,
    _sharpe_convention_candidates,
    _split_by_entry_and_direction,
    _timestamp_only_overlap_count,
    _top_session_minutes,
    _trade_return_frame,
    _utc_timestamp,
    _year_counts,
)
from shared.broker_clock import BROKER_CLOCKS, DUKASCOPY_MT5_FOREX, DUKASCOPY_MT5_INDEX
from shared.models import Trade, TradeExit
from validation.importers.mt5_xlsx import MT5XLSXImporter


def _trade(
    *,
    entry_time: str,
    direction: str = "LONG",
    entry_price: float = 100.0,
    exit_price: float = 99.0,
    stop_loss: float | None = None,
) -> Trade:
    meta = {}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    return Trade(
        id=1,
        symbol="TEST",
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=1.0,
        exits=[
            TradeExit(
                time="2024-01-01T13:00:00Z",
                price=exit_price,
                quantity=1.0,
                pnl=0.0,
            )
        ],
        pnl=0.0,
        meta=meta,
    )


class _Reference:
    def __init__(
        self,
        trades: list[Trade],
        stats: dict[str, float],
        initial_capital: float = 1000.0,
    ):
        self.trades = trades
        self.stats = stats
        self.settings = {}
        self.raw_metadata = {"parsed_meta": {"initial_capital": initial_capital}}


def test_utc_timestamp_normalizes_equivalent_iso_spellings():
    assert _utc_timestamp("2024-01-01T10:00:00Z") == _utc_timestamp(
        "2024-01-01T10:00:00+00:00"
    )


def test_match_by_entry_and_direction_uses_utc_timestamp_not_string_spelling():
    reference = [_trade(entry_time="2024-01-01T10:00:00Z", direction="LONG")]
    platform = [
        {
            "entry_time": "2024-01-01T10:00:00+00:00",
            "direction": "LONG",
            "entry_price": 100.0,
            "exits": [{"time": "2024-01-01T11:00:00+00:00", "price": 101.0}],
        }
    ]

    matches = _match_by_entry_and_direction(reference, platform)

    assert len(matches) == 1
    assert matches[0][1] is platform[0]


def test_split_by_entry_and_direction_preserves_unmatched_and_direction_conflicts():
    reference = [
        _trade(entry_time="2024-01-01T10:00:00Z", direction="LONG"),
        _trade(entry_time="2024-01-01T11:00:00Z", direction="SHORT"),
    ]
    platform = [
        {
            "entry_time": "2024-01-01T10:00:00+00:00",
            "direction": "LONG",
            "entry_price": 100.0,
            "exits": [{"time": "2024-01-01T10:30:00+00:00", "price": 101.0}],
        },
        {
            "entry_time": "2024-01-01T11:00:00+00:00",
            "direction": "LONG",
            "entry_price": 100.0,
            "exits": [{"time": "2024-01-01T11:30:00+00:00", "price": 101.0}],
        },
    ]

    matches, unmatched_reference, unmatched_platform = _split_by_entry_and_direction(
        reference,
        platform,
    )

    assert len(matches) == 1
    assert unmatched_reference == [reference[1]]
    assert unmatched_platform == [platform[1]]
    assert _timestamp_only_overlap_count(reference, platform) == 2
    assert _direction_counts(unmatched_reference) == {"SHORT": 1}
    assert _direction_counts(unmatched_platform) == {"LONG": 1}


def test_top_session_minutes_uses_broker_server_time_not_athens():
    # 2025-03-17..19: the US is on DST while the EU is not, so the index clock is
    # UTC+3 where Europe/Athens is still UTC+2. A fixed-Athens reading prints
    # 16:45/17:00 here, i.e. one hour off the server clock the EA traded on.
    trades = [
        _trade(entry_time="2025-03-17T14:45:00Z"),
        _trade(entry_time="2025-03-18T14:45:00Z"),
        _trade(entry_time="2025-03-19T15:00:00Z"),
    ]

    assert _top_session_minutes(trades, 2, DUKASCOPY_MT5_INDEX) == [("17:45", 2), ("18:00", 1)]


def test_top_session_minutes_keeps_the_legacy_fixed_offset_for_indices():
    # 2021-07-01 is in the legacy history: index CFDs ran on a fixed UTC+2 while
    # forex ran on Europe/Athens (UTC+3 that day). The two symbols differ by an hour
    # on the same UTC instant, which a single shared zone cannot express.
    trades = [_trade(entry_time="2021-07-01T14:45:00Z")]

    assert _top_session_minutes(trades, 1, DUKASCOPY_MT5_INDEX) == [("16:45", 1)]
    assert _top_session_minutes(trades, 1, DUKASCOPY_MT5_FOREX) == [("17:45", 1)]


def test_year_counts_buckets_on_the_server_calendar():
    # 2018-12-31 23:30 UTC is already 2019-01-01 01:30 on the legacy index clock,
    # and the report prints the server date, so both trades belong to 2019.
    trades = [
        _trade(entry_time="2018-12-31T23:30:00Z"),
        _trade(entry_time="2019-06-03T10:00:00Z"),
    ]

    assert _year_counts(trades, DUKASCOPY_MT5_INDEX) == {2019: 2}


@pytest.mark.parametrize(
    ("label", "expected_clock"),
    [
        ("EURUSD_H1", "dukascopy_mt5_forex"),
        ("NAS100_M15", "dukascopy_mt5_index"),
        ("SP500_M1", "dukascopy_mt5_index"),
    ],
)
def test_broker_clock_is_the_one_the_platform_run_used(label, expected_clock):
    assert _broker_clock(label) is BROKER_CLOCKS[expected_clock]


def test_every_report_has_a_broker_clock():
    assert set(MT5_REPORTS) == {"EURUSD_H1", "NAS100_M15", "SP500_M1"}
    for label in MT5_REPORTS:
        assert _broker_clock(label).name.startswith("dukascopy_mt5_")


def test_broker_clock_rejects_a_label_without_a_comparison_case():
    with pytest.raises(ValueError, match="no comparison case"):
        _broker_clock("GBPUSD_H1")


def test_load_reference_reads_the_report_on_the_case_broker_clock(monkeypatch):
    calls: list[tuple[Path, dict[str, object]]] = []

    def fake_load(self, path, **kwargs):
        calls.append((path, kwargs))
        return "reference"

    monkeypatch.setattr(MT5XLSXImporter, "load", fake_load)

    assert _load_reference("NAS100_M15", Path("NAS100_M15.xlsx")) == "reference"
    assert calls == [(Path("NAS100_M15.xlsx"), {"broker_clock": DUKASCOPY_MT5_INDEX})]


def test_exit_reason_helpers_classify_stop_and_platform_signal():
    mt5_stop = _trade(entry_time="2024-01-01T10:00:00Z", exit_price=99.0, stop_loss=99.0)
    mt5_signal = _trade(entry_time="2024-01-01T10:00:00Z", exit_price=100.5, stop_loss=99.0)
    platform_signal = {"exit_reason": "signal", "exits": [{"reason": "signal"}]}

    assert _mt5_exit_reason(mt5_stop) == "stop_loss"
    assert _mt5_exit_reason(mt5_signal) == "signal_or_none"
    assert _platform_exit_reason(platform_signal) == "signal"


def test_series_stats_handles_empty_and_quartiles():
    assert _series_stats([])["n"] == 0
    stats = _series_stats([1.0, 2.0, 3.0])
    assert stats["n"] == 3
    assert stats["mean"] == 2.0
    assert stats["median"] == 2.0


def test_sharpe_convention_candidates_include_trade_and_daily_variants():
    trades = [
        _trade(
            entry_time="2024-01-01T10:00:00Z",
            exit_price=101.0,
        ),
        _trade(
            entry_time="2024-01-02T10:00:00Z",
            exit_price=99.0,
        ),
        _trade(
            entry_time="2024-01-03T10:00:00Z",
            exit_price=102.0,
        ),
    ]
    trades[0].pnl = 10.0
    trades[0].exits[0].time = "2024-01-01T13:00:00Z"
    trades[1].pnl = -5.0
    trades[1].exits[0].time = "2024-01-02T13:00:00Z"
    trades[2].pnl = 15.0
    trades[2].exits[0].time = "2024-01-03T13:00:00Z"
    ref = _Reference(trades, {"total_deals": 6.0})

    candidates = _sharpe_convention_candidates(ref)

    assert candidates["trade_pnl_sqrt_trades"] is not None
    assert candidates["trade_pnl_sqrt_deals"] is not None
    assert candidates["daily_calendar_sqrt_252"] is not None
    assert candidates["trade_pnl_sqrt_deals"] > candidates["trade_pnl_sqrt_trades"]


def test_trade_return_frame_uses_canonical_initial_capital_resolver():
    trade = _trade(entry_time="2024-01-01T10:00:00Z")
    trade.pnl = 100.0
    reference = _Reference([trade], {}, initial_capital=1_000.0)
    reference.settings["initial_capital"] = 2_000.0

    frame = _trade_return_frame(reference)

    assert frame.iloc[0]["equity_before"] == pytest.approx(2_000.0)
    assert frame.iloc[0]["return_on_initial_capital"] == pytest.approx(0.05)
