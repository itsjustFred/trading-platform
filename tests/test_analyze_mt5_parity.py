from shared.models import Trade, TradeExit
from scripts.analyze_mt5_parity import (
    _direction_counts,
    _match_by_entry_and_direction,
    _mt5_exit_reason,
    _platform_exit_reason,
    _sharpe_convention_candidates,
    _series_stats,
    _split_by_entry_and_direction,
    _timestamp_only_overlap_count,
    _top_session_minutes,
    _utc_timestamp,
)


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


def test_top_session_minutes_uses_athens_local_time():
    trades = [
        _trade(entry_time="2024-01-01T14:45:00Z"),
        _trade(entry_time="2024-01-02T14:45:00Z"),
        _trade(entry_time="2024-01-03T15:00:00Z"),
    ]

    assert _top_session_minutes(trades, 2) == [("16:45", 2), ("17:00", 1)]


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
