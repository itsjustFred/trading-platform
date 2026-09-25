#!/usr/bin/env python3
"""Continuable MT5 parity diagnostics for IntradayVolatilityBreakout."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runner.engines.base import resolve_strategy_parameters
from scripts.regenerate_mt5_parity_artifacts import (
    COMPARISON_CASES,
    STRATEGY_PATH,
    _load_ohlcv,
    _opposite_side_series,
)
from strategies.intraday_volatility_breakout.intraday_volatility_breakout import generate_signals
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import resolve_initial_capital

MT5_SOURCE_TIMEZONE = "Europe/Athens"
SESSION_TIMEZONE = ZoneInfo("Europe/Athens")


@dataclass(frozen=True)
class CasePaths:
    label: str
    mt5: Path
    platform: Path


MT5_REPORTS = {
    "EURUSD_H1": Path("strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx"),
    "NAS100_M15": Path("strategy-comparisons/intraday_volatility_breakout/NAS100_M15.xlsx"),
    "SP500_M1": Path("strategy-comparisons/intraday_volatility_breakout/SP500_M1.xlsx"),
}


def _case_by_label() -> dict[str, dict[str, Any]]:
    return {str(case["label"]): dict(case) for case in COMPARISON_CASES}


def _platform_path(label: str) -> Path:
    return Path(_case_by_label()[label]["output"])


def _load_platform(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _trade_exit(trade: Any) -> Any | None:
    exits = getattr(trade, "exits", None)
    if exits:
        return exits[-1]
    if isinstance(trade, dict):
        raw_exits = trade.get("exits") or []
        return raw_exits[-1] if raw_exits else None
    return None


def _trade_exit_price(trade: Any) -> float | None:
    exit_obj = _trade_exit(trade)
    if exit_obj is None:
        return None
    if isinstance(exit_obj, dict):
        value = exit_obj.get("price")
    else:
        value = getattr(exit_obj, "price", None)
    return float(value) if value is not None else None


def _trade_exit_time(trade: Any) -> str | None:
    exit_obj = _trade_exit(trade)
    if exit_obj is None:
        return None
    if isinstance(exit_obj, dict):
        return exit_obj.get("time")
    return getattr(exit_obj, "time", None)


def _platform_exit_reason(trade: dict[str, Any]) -> str:
    exit_obj = _trade_exit(trade)
    if isinstance(exit_obj, dict):
        return str(exit_obj.get("reason") or trade.get("exit_reason") or "")
    return str(trade.get("exit_reason") or "")


def _mt5_exit_reason(trade: Any) -> str:
    reason = str(getattr(trade, "exit_reason", "") or "")
    if reason:
        return reason
    stop_loss = getattr(trade, "meta", {}).get("stop_loss")
    exit_price = _trade_exit_price(trade)
    if stop_loss is None or exit_price is None:
        return "signal_or_none"
    stop = float(stop_loss)
    tolerance = max(1e-5, abs(stop) * 1e-7)
    return "stop_loss" if abs(exit_price - stop) <= tolerance else "signal_or_none"


def _match_by_entry_and_direction(
    reference_trades: list[Any],
    platform_trades: list[dict[str, Any]],
) -> list[tuple[Any, dict[str, Any]]]:
    matches, _, _ = _split_by_entry_and_direction(reference_trades, platform_trades)
    return matches


def _split_by_entry_and_direction(
    reference_trades: list[Any],
    platform_trades: list[dict[str, Any]],
) -> tuple[list[tuple[Any, dict[str, Any]]], list[Any], list[dict[str, Any]]]:
    platform_by_key: dict[tuple[pd.Timestamp, str], deque[dict[str, Any]]] = defaultdict(deque)
    for trade in platform_trades:
        key = (_utc_timestamp(trade["entry_time"]), str(trade.get("direction", "")).upper())
        platform_by_key[key].append(trade)

    matched: list[tuple[Any, dict[str, Any]]] = []
    unmatched_reference: list[Any] = []
    for trade in reference_trades:
        key = (_utc_timestamp(trade.entry_time), str(trade.direction).upper())
        if platform_by_key[key]:
            matched.append((trade, platform_by_key[key].popleft()))
        else:
            unmatched_reference.append(trade)
    unmatched_platform = [trade for queue in platform_by_key.values() for trade in queue]
    return matched, unmatched_reference, unmatched_platform


def _timestamp_only_overlap_count(reference_trades: list[Any], platform_trades: list[dict[str, Any]]) -> int:
    reference_counts = Counter(_utc_timestamp(trade.entry_time) for trade in reference_trades)
    platform_counts = Counter(_utc_timestamp(trade["entry_time"]) for trade in platform_trades)
    return sum(min(count, platform_counts.get(timestamp, 0)) for timestamp, count in reference_counts.items())


def _direction_counts(trades: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for trade in trades:
        if isinstance(trade, dict):
            direction = str(trade.get("direction", "")).upper()
        else:
            direction = str(getattr(trade, "direction", "")).upper()
        counts[direction or "UNKNOWN"] += 1
    return dict(sorted(counts.items()))


def _entry_timestamp(trade: Any) -> pd.Timestamp:
    if isinstance(trade, dict):
        return _utc_timestamp(trade["entry_time"])
    return _utc_timestamp(trade.entry_time)


def _session_minute_label(timestamp: pd.Timestamp) -> str:
    session_timestamp = timestamp.tz_convert(SESSION_TIMEZONE)
    return f"{session_timestamp.hour:02d}:{session_timestamp.minute:02d}"


def _top_session_minutes(trades: list[Any], limit: int) -> list[tuple[str, int]]:
    counts = Counter(_session_minute_label(_entry_timestamp(trade)) for trade in trades)
    return counts.most_common(limit)


def _year_counts(trades: list[Any]) -> dict[int, int]:
    counts = Counter(_entry_timestamp(trade).year for trade in trades)
    return dict(sorted(counts.items()))


def _series_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None}
    series = pd.Series(values, dtype=float)
    return {
        "n": int(series.count()),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "p25": float(series.quantile(0.25)),
        "p75": float(series.quantile(0.75)),
    }


def _format_stats(stats: dict[str, float | int | None]) -> str:
    if not stats["n"]:
        return "n=0"
    return (
        f"n={stats['n']} mean={float(stats['mean']):.6g} "
        f"median={float(stats['median']):.6g} "
        f"p25={float(stats['p25']):.6g} p75={float(stats['p75']):.6g}"
    )


def _annualized_sharpe(values: pd.Series, periods: float) -> float | None:
    series = pd.to_numeric(values, errors="coerce").dropna()
    if len(series) < 2:
        return None
    std = float(series.std(ddof=1))
    if std == 0.0 or not math.isfinite(std):
        return None
    return float(series.mean()) / std * math.sqrt(periods)


def _annualized_sortino_like(values: pd.Series, periods: float) -> float | None:
    series = pd.to_numeric(values, errors="coerce").dropna()
    if len(series) < 2:
        return None
    downside = series[series < 0.0]
    if downside.empty:
        return None
    downside_deviation = math.sqrt(float((downside**2).mean()))
    if downside_deviation == 0.0 or not math.isfinite(downside_deviation):
        return None
    return float(series.mean()) / downside_deviation * math.sqrt(periods)


def _trade_return_frame(reference: Any) -> pd.DataFrame:
    initial_capital = resolve_initial_capital(reference)
    rows: list[dict[str, Any]] = []
    equity = initial_capital
    for trade in reference.trades:
        pnl = float(trade.pnl)
        exit_time = _trade_exit_time(trade)
        if exit_time is None:
            continue
        before = equity
        equity += pnl
        rows.append(
            {
                "time": _utc_timestamp(exit_time),
                "pnl": pnl,
                "equity_before": before,
                "equity_after": equity,
                "return_on_prior_equity": pnl / before if before else 0.0,
                "return_on_initial_capital": pnl / initial_capital if initial_capital else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _daily_returns_from_reference_trades(reference: Any, *, business_days: bool = False) -> pd.Series:
    frame = _trade_return_frame(reference)
    if frame.empty:
        return pd.Series(dtype=float)
    initial_capital = resolve_initial_capital(reference)
    daily_equity = frame.set_index("time").groupby(pd.Grouper(freq="D"))["equity_after"].last()
    full_index = pd.date_range(
        frame["time"].min().normalize(),
        frame["time"].max().normalize(),
        freq="B" if business_days else "D",
        tz="UTC",
    )
    if full_index.empty:
        return pd.Series(dtype=float)
    daily_equity = daily_equity.reindex(full_index).ffill().fillna(initial_capital)
    return daily_equity.pct_change().dropna()


def _sharpe_convention_candidates(reference: Any) -> dict[str, float | None]:
    frame = _trade_return_frame(reference)
    if frame.empty:
        return {}
    trade_count = len(frame)
    total_deals = float(reference.stats.get("total_deals") or trade_count)
    daily_returns = _daily_returns_from_reference_trades(reference)
    business_returns = _daily_returns_from_reference_trades(reference, business_days=True)
    active_daily_returns = daily_returns[daily_returns != 0.0]
    monthly_returns = (
        frame.set_index("time")["equity_after"].resample("ME").last().pct_change().dropna()
    )
    return {
        "trade_pnl_sqrt_trades": _annualized_sharpe(frame["pnl"], trade_count),
        "trade_pnl_sqrt_deals": _annualized_sharpe(frame["pnl"], total_deals),
        "trade_pnl_sqrt_252": _annualized_sharpe(frame["pnl"], 252.0),
        "trade_return_prior_equity_sqrt_trades": _annualized_sharpe(
            frame["return_on_prior_equity"],
            trade_count,
        ),
        "trade_return_prior_equity_unannualized": _annualized_sharpe(
            frame["return_on_prior_equity"],
            1.0,
        ),
        "daily_calendar_sqrt_252": _annualized_sharpe(daily_returns, 252.0),
        "daily_business_sqrt_252": _annualized_sharpe(business_returns, 252.0),
        "daily_nonzero_sqrt_252": _annualized_sharpe(active_daily_returns, 252.0),
        "daily_nonzero_sqrt_active_days": _annualized_sharpe(
            active_daily_returns,
            float(len(active_daily_returns)),
        ),
        "monthly_sqrt_12": _annualized_sharpe(monthly_returns, 12.0),
        "trade_pnl_sortino_like_sqrt_trades": _annualized_sortino_like(frame["pnl"], trade_count),
    }


def _local_stop_touch_counts(
    *,
    case: dict[str, Any],
    data_dir: Path,
    bad_matches: list[tuple[Any, dict[str, Any]]],
) -> tuple[int, int, int]:
    if not bad_matches:
        return 0, 0, 0

    df = _load_ohlcv(
        data_dir,
        str(case["execution_symbol"]),
        str(case["execution_timeframe"]),
        str(case["start"]),
        str(case["end"]),
    )
    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    windows = 0
    mt5_stop_touches = 0
    platform_stop_touches = 0
    for reference_trade, platform_trade in bad_matches:
        mt5_stop = getattr(reference_trade, "meta", {}).get("stop_loss")
        platform_stop = platform_trade.get("meta", {}).get("sl_price")
        reference_exit_time = _trade_exit_time(reference_trade)
        if mt5_stop is None or platform_stop is None or reference_exit_time is None:
            continue

        start = timestamps.searchsorted(_utc_timestamp(platform_trade["entry_time"]), side="right")
        end = timestamps.searchsorted(_utc_timestamp(reference_exit_time), side="right")
        if end <= start:
            continue

        windows += 1
        side = str(platform_trade.get("direction", "")).upper()
        if side == "LONG":
            prices = _opposite_side_series(df, side, "low").iloc[start:end]
            mt5_touched = bool((prices <= float(mt5_stop)).any())
            platform_touched = bool((prices <= float(platform_stop)).any())
        else:
            prices = _opposite_side_series(df, side, "high").iloc[start:end]
            mt5_touched = bool((prices >= float(mt5_stop)).any())
            platform_touched = bool((prices >= float(platform_stop)).any())
        mt5_stop_touches += int(mt5_touched)
        platform_stop_touches += int(platform_touched)
    return windows, mt5_stop_touches, platform_stop_touches


def summary(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        platform = _load_platform(_platform_path(label))
        mt5_pnl = float(ref.stats.get("total_pnl") or 0.0)
        platform_pnl = float(platform.get("stats", {}).get("total_pnl") or 0.0)
        mt5_dirs: dict[str, int] = {}
        platform_dirs: dict[str, int] = {}
        for trade in ref.trades:
            mt5_dirs[trade.direction] = mt5_dirs.get(trade.direction, 0) + 1
        for trade in platform.get("trades", []):
            platform_dirs[str(trade.get("direction"))] = platform_dirs.get(str(trade.get("direction")), 0) + 1
        print(
            f"{label}: mt5_trades={len(ref.trades)} platform_trades={len(platform.get('trades', []))} "
            f"trade_delta={len(platform.get('trades', [])) - len(ref.trades)} "
            f"mt5_pnl={mt5_pnl:.2f} platform_pnl={platform_pnl:.2f} pnl_delta={platform_pnl - mt5_pnl:.2f}"
        )
        print(f"  dirs mt5={mt5_dirs} platform={platform_dirs}")


def entry_minutes(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        times = pd.to_datetime([trade.entry_time for trade in ref.trades], utc=True, errors="coerce")
        minutes = pd.Series(times.hour * 60 + times.minute)
        print(f"\n{label} entry minutes")
        print(minutes.value_counts().sort_index().to_string())


def stop_atr(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    cases = _case_by_label()
    params = resolve_strategy_parameters(STRATEGY_PATH, {"session_timezone_offset_hours": args.session_offset})
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        case = cases[label]
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        _, _, indicators, _ = generate_signals(df, params)
        mt5_rows: list[dict[str, Any]] = []
        our_atr = indicators["daily_atr"]
        our_stop_distance = abs(pd.to_numeric(indicators["sl_price"], errors="coerce") - df["close"])
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        by_ts = pd.Series(df.index, index=ts)
        for trade in ref.trades[: args.limit]:
            sl = trade.meta.get("stop_loss")
            if sl is None:
                continue
            entry_ts = pd.Timestamp(trade.entry_time)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize("UTC")
            else:
                entry_ts = entry_ts.tz_convert("UTC")
            idx = by_ts.index.searchsorted(entry_ts)
            if idx >= len(by_ts):
                continue
            row_index = int(by_ts.iloc[idx])
            mt5_stop_distance = abs(float(trade.entry_price) - float(sl))
            mt5_atr = mt5_stop_distance / 0.25
            mt5_rows.append(
                {
                    "entry_time": trade.entry_time,
                    "dir": trade.direction,
                    "mt5_entry": trade.entry_price,
                    "mt5_sl": sl,
                    "mt5_stop": mt5_stop_distance,
                    "mt5_atr": mt5_atr,
                    "our_time": ts.iloc[row_index].isoformat(),
                    "our_close": float(df["close"].iloc[row_index]),
                    "our_atr": float(our_atr.iloc[row_index]) if pd.notna(our_atr.iloc[row_index]) else None,
                    "our_stop_at_entry_signal": float(our_stop_distance.iloc[row_index]) if pd.notna(our_stop_distance.iloc[row_index]) else None,
                }
            )
        print(f"\n{label} first {len(mt5_rows)} MT5 stop-implied ATR rows")
        for row in mt5_rows:
            ratio = row["our_atr"] / row["mt5_atr"] if row["our_atr"] and row["mt5_atr"] else None
            our_atr_text = f"{row['our_atr']:.6g}" if row["our_atr"] is not None else "None"
            ratio_text = f"{ratio:.3f}" if ratio is not None else "None"
            print(
                f"{row['entry_time']} {row['dir']} mt5_atr={row['mt5_atr']:.6g} "
                f"our_atr={our_atr_text} ratio={ratio_text} "
                f"mt5_entry={row['mt5_entry']} our_close={row['our_close']}"
            )


def signal_counts(args: argparse.Namespace) -> None:
    cases = _case_by_label()
    for label, case in cases.items():
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        print(f"\n{label}")
        for offset in args.offsets:
            for use_close in (True, False):
                params = resolve_strategy_parameters(
                    STRATEGY_PATH,
                    {
                        "session_timezone_offset_hours": offset,
                        "use_candle_close": use_close,
                    },
                )
                entries, _, indicators, _ = generate_signals(df, params)
                dirs = indicators["direction"].dropna().value_counts().to_dict()
                print(f"offset={offset:+g} use_close={use_close} entries={int(entries.sum())} dirs={dirs}")




def data_quality(args: argparse.Namespace) -> None:
    cases = _case_by_label()
    importer = MT5XLSXImporter()
    for label, case in cases.items():
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        ohlc_cols = ["open", "high", "low", "close"]
        print(f"\n{label} data quality {case['execution_symbol']} {case['execution_timeframe']} rows={len(df)}")
        print("time", df["timestamp"].min(), df["timestamp"].max())
        for col in ohlc_cols:
            print(col, "nan", int(pd.to_numeric(df[col], errors="coerce").isna().sum()))
        ref = importer.load(MT5_REPORTS[label], source_timezone=MT5_SOURCE_TIMEZONE)
        times = pd.to_datetime([trade.entry_time for trade in ref.trades], utc=True, errors="coerce")
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        ts_index = pd.Series(df.index, index=ts)
        for offset in args.offsets:
            found = 0
            found_valid = 0
            price_diffs = []
            for trade, mt5_time in zip(ref.trades, times, strict=False):
                query_time = mt5_time - pd.Timedelta(hours=offset)
                pos = ts_index.index.searchsorted(query_time)
                if pos >= len(ts_index):
                    continue
                row_index = int(ts_index.iloc[pos])
                row_time = ts.iloc[row_index]
                if abs((row_time - query_time).total_seconds()) > args.tolerance_seconds:
                    continue
                found += 1
                close = pd.to_numeric(pd.Series([df["close"].iloc[row_index]]), errors="coerce").iloc[0]
                if pd.notna(close):
                    found_valid += 1
                    price_diffs.append(float(close) - float(trade.entry_price))
            avg_diff = sum(price_diffs) / len(price_diffs) if price_diffs else None
            print(
                f"mt5_time_minus_offset={offset:+g}h matched={found}/{len(ref.trades)} "
                f"valid_close={found_valid} avg_close_minus_mt5_entry={avg_diff}"
            )


def matched_stop_path(args: argparse.Namespace) -> None:
    cases = _case_by_label()
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        case = cases[label]
        reference = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        platform = _load_platform(_platform_path(label))
        platform_trades = list(platform.get("trades", []))
        matches = _match_by_entry_and_direction(reference.trades, platform_trades)

        print(f"\n{label}")
        print(f"matched={len(matches)} mt5_trades={len(reference.trades)} platform_trades={len(platform_trades)}")

        reason_pairs = Counter((_mt5_exit_reason(ref), _platform_exit_reason(plat)) for ref, plat in matches)
        for pair, count in sorted(reason_pairs.items()):
            print(f"reason_pair {pair[0]} -> {pair[1]}: {count}")

        stop_ratios: list[float] = []
        bad_bucket_ratios: list[float] = []
        bad_matches: list[tuple[Any, dict[str, Any]]] = []
        entry_diffs: list[float] = []
        exit_diffs: list[float] = []
        exit_time_diffs: list[float] = []
        for reference_trade, platform_trade in matches:
            platform_exit = _trade_exit(platform_trade)
            mt5_exit_price = _trade_exit_price(reference_trade)
            platform_exit_price = _trade_exit_price(platform_trade)
            mt5_stop = getattr(reference_trade, "meta", {}).get("stop_loss")
            platform_stop = platform_trade.get("meta", {}).get("sl_price")
            mt5_reason = _mt5_exit_reason(reference_trade)
            platform_reason = _platform_exit_reason(platform_trade)

            entry_diffs.append(float(platform_trade["entry_price"]) - float(reference_trade.entry_price))
            if mt5_exit_price is not None and platform_exit_price is not None:
                exit_diffs.append(platform_exit_price - mt5_exit_price)
            if platform_exit is not None:
                reference_exit_time = _trade_exit_time(reference_trade)
                platform_exit_time = _trade_exit_time(platform_trade)
                if reference_exit_time and platform_exit_time:
                    exit_time_diffs.append(
                        (
                            _utc_timestamp(platform_exit_time)
                            - _utc_timestamp(reference_exit_time)
                        ).total_seconds()
                    )

            if mt5_stop is None or platform_stop is None:
                continue
            mt5_distance = abs(float(reference_trade.entry_price) - float(mt5_stop))
            platform_distance = abs(float(platform_trade["entry_price"]) - float(platform_stop))
            if mt5_distance <= 0:
                continue
            ratio = platform_distance / mt5_distance
            stop_ratios.append(ratio)
            if mt5_reason != "stop_loss" and platform_reason == "stop_loss":
                bad_matches.append((reference_trade, platform_trade))
                bad_bucket_ratios.append(ratio)

        print(f"entry_price_diff {_format_stats(_series_stats(entry_diffs))}")
        print(f"exit_price_diff {_format_stats(_series_stats(exit_diffs))}")
        print(f"exit_time_diff_seconds {_format_stats(_series_stats(exit_time_diffs))}")
        print(f"stop_distance_ratio {_format_stats(_series_stats(stop_ratios))}")
        print(f"bad_bucket_stop_distance_ratio {_format_stats(_series_stats(bad_bucket_ratios))}")

        if args.skip_ohlc:
            continue
        windows, mt5_touches, platform_touches = _local_stop_touch_counts(
            case=case,
            data_dir=args.data_dir,
            bad_matches=bad_matches,
        )
        print(
            "bad_bucket_local_ohlc_touches "
            f"windows={windows} mt5_stop={mt5_touches} platform_stop={platform_touches}"
        )


def sharpe_conventions(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        reference = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        report_sharpe = float(reference.stats.get("sharpe_ratio") or 0.0)
        candidates = _sharpe_convention_candidates(reference)
        print(f"\n{label}")
        print(
            f"report_sharpe={report_sharpe:.6f} "
            f"trades={len(reference.trades)} deals={reference.stats.get('total_deals')}"
        )
        for name, value in candidates.items():
            if value is None:
                print(f"{name}: unavailable")
                continue
            print(f"{name}: {value:.6f} delta={value - report_sharpe:+.6f}")


def unmatched_entries(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        reference = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        platform = _load_platform(_platform_path(label))
        platform_trades = list(platform.get("trades", []))
        matches, unmatched_reference, unmatched_platform = _split_by_entry_and_direction(
            reference.trades,
            platform_trades,
        )
        timestamp_overlap = _timestamp_only_overlap_count(reference.trades, platform_trades)
        direction_conflicts = max(0, timestamp_overlap - len(matches))

        print(f"\n{label}")
        print(
            f"entry_direction_matches={len(matches)} "
            f"timestamp_only_overlap={timestamp_overlap} "
            f"direction_conflicts={direction_conflicts}"
        )
        print(
            f"unmatched_mt5={len(unmatched_reference)} "
            f"unmatched_platform={len(unmatched_platform)}"
        )
        print(f"unmatched_mt5_directions={_direction_counts(unmatched_reference)}")
        print(f"unmatched_platform_directions={_direction_counts(unmatched_platform)}")
        print(f"unmatched_mt5_years={_year_counts(unmatched_reference)}")
        print(f"unmatched_platform_years={_year_counts(unmatched_platform)}")
        print(
            "unmatched_mt5_session_minutes="
            f"{_top_session_minutes(unmatched_reference, args.top)}"
        )
        print(
            "unmatched_platform_session_minutes="
            f"{_top_session_minutes(unmatched_platform, args.top)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/mnt/block-volume-data"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary").set_defaults(func=summary)
    sub.add_parser("entry-minutes").set_defaults(func=entry_minutes)
    p_stop = sub.add_parser("stop-atr")
    p_stop.add_argument("--session-offset", type=float, default=4.0)
    p_stop.add_argument("--limit", type=int, default=8)
    p_stop.set_defaults(func=stop_atr)
    p_counts = sub.add_parser("signal-counts")
    p_counts.add_argument("--offsets", type=float, nargs="+", default=[0, 1, 2, 3, 4])
    p_counts.set_defaults(func=signal_counts)
    p_quality = sub.add_parser("data-quality")
    p_quality.add_argument("--offsets", type=float, nargs="+", default=[-4, -3, -2, -1, 0, 1, 2, 3, 4])
    p_quality.add_argument("--tolerance-seconds", type=int, default=60)
    p_quality.set_defaults(func=data_quality)
    p_match = sub.add_parser("matched-stop-path")
    p_match.add_argument("--skip-ohlc", action="store_true", help="Skip local OHLC stop-touch replay.")
    p_match.set_defaults(func=matched_stop_path)
    sub.add_parser("sharpe-conventions").set_defaults(func=sharpe_conventions)
    p_unmatched = sub.add_parser("unmatched-entries")
    p_unmatched.add_argument("--top", type=int, default=5, help="Number of top session-minute buckets to print.")
    p_unmatched.set_defaults(func=unmatched_entries)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
