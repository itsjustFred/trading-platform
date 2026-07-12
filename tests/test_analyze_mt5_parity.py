from shared.models import Trade, TradeExit
from scripts.analyze_mt5_parity import (
    _match_by_entry_and_direction,
    _mt5_exit_reason,
    _platform_exit_reason,
    _series_stats,
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
