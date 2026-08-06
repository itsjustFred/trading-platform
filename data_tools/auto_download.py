"""Ensure OHLCV data exists before runner scripts start a backtest."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from shared.constants import DATA_DIR, normalize_timeframe

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
US_INTRADAY_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h"}
HF_INTRADAY_COVERAGE_END = "2025-07-31"
ALPACA_INTRADAY_START = "2025-07-01"
_US_STOCK_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


@dataclass(frozen=True)
class DataRequirement:
    symbol: str
    timeframe: str
    start: str
    end: str


def ensure_backtest_data(
    *,
    symbols: list[str],
    timeframe: str,
    start: str = "",
    end: str = "",
    data_dir: str | Path = "",
    modeling_resolution: str = "main_timeframe",
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    """Download missing OHLCV files/ranges needed by a backtest.

    Returns a list of downloaded requirement payloads. If everything is already
    covered locally, returns an empty list.
    """
    data_root = _resolve_data_root(data_dir)
    requirements = _build_requirements(
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        modeling_resolution=modeling_resolution,
    )
    if not requirements:
        return []

    missing = [
        requirement
        for requirement in requirements
        if not _has_required_coverage(data_root, requirement)
    ]
    if not missing:
        return []

    downloaded: list[dict[str, Any]] = []
    total = len(missing)
    for index, requirement in enumerate(missing, start=1):
        if progress_callback is not None:
            progress_callback(
                5.0 + ((index - 1) / total) * 35.0,
                f"Downloading data for {requirement.symbol} {requirement.timeframe}...",
            )

        source_plan = _auto_download_source_plan(
            symbol=requirement.symbol,
            timeframe=requirement.timeframe,
            start=requirement.start or None,
            end=requirement.end or None,
        )
        for source_index, step in enumerate(source_plan, start=1):
            source = str(step.get("source") or "").strip().lower()
            step_start = str(step.get("start") or "").strip() or None
            step_end = str(step.get("end") or "").strip() or None
            if log_callback is not None:
                suffix = f" [{source_index}/{len(source_plan)}]" if len(source_plan) > 1 else ""
                log_callback(
                    f"Auto-download{suffix}: {requirement.symbol} {requirement.timeframe} "
                    f"via {source} ({step_start or 'auto'} -> {step_end or 'latest'})"
                )
            _download_missing_ohlcv_data(
                data_root=data_root,
                symbol=requirement.symbol,
                timeframe=requirement.timeframe,
                start=step_start,
                end=step_end,
                source=source,
            )

        if not _has_required_coverage(data_root, requirement):
            raise RuntimeError(
                "Auto-download did not provide required OHLCV coverage for "
                f"{requirement.symbol} {requirement.timeframe} "
                f"({requirement.start or 'auto'} -> {requirement.end or 'latest'})"
            )

        payload = {
            "symbol": requirement.symbol,
            "timeframe": requirement.timeframe,
            "start": requirement.start,
            "end": requirement.end,
            "sources": [step.get("source") for step in source_plan],
        }
        downloaded.append(payload)

        if progress_callback is not None:
            progress_callback(
                5.0 + (index / total) * 35.0,
                f"Downloaded data for {requirement.symbol} {requirement.timeframe}",
            )

    _invalidate_catalog_cache(data_root)
    return downloaded


def _resolve_data_root(data_dir: str | Path) -> Path:
    if data_dir:
        return Path(data_dir).expanduser().resolve()
    return DATA_DIR


def _parquet_has_bar_in_range(path: Path, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> bool:
    """Return whether the parquet at ``path`` has any timestamp within ``[start_ts, end_ts]``."""
    try:
        frame = pd.read_parquet(path, columns=["timestamp"])
    except Exception as exc:
        logger.warning("Could not inspect OHLCV timestamps for %s: %s", path, exc)
        return False
    if "timestamp" not in frame.columns or frame.empty:
        return False
    timestamps = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce", format="mixed"
    ).dropna()
    return ((timestamps >= start_ts) & (timestamps <= end_ts)).any()


def has_ohlcv_bars_in_range(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    data_dir: str | Path = "",
) -> bool:
    """Return whether canonical OHLCV data contains a valid bar in ``[start, end]``."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_symbol or not normalized_timeframe:
        return False

    start_ts = pd.to_datetime(str(start or ""), utc=True, errors="coerce")
    end_ts = pd.to_datetime(str(end or ""), utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        return False

    ohlcv_root = _resolve_data_root(data_dir) / "ohlcv"
    matches = sorted(
        path
        for path in ohlcv_root.rglob(f"**/{normalized_timeframe}.parquet")
        if path.parent.name.upper() == normalized_symbol
    )
    return any(_parquet_has_bar_in_range(path, start_ts, end_ts) for path in matches)


def _build_requirements(
    *,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    modeling_resolution: str,
) -> list[DataRequirement]:
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_timeframe:
        return []

    timeframes = [normalized_timeframe]
    intrabar_timeframe = _resolve_intrabar_timeframe(normalized_timeframe, modeling_resolution)
    if intrabar_timeframe and intrabar_timeframe not in timeframes:
        timeframes.append(intrabar_timeframe)

    requirements: list[DataRequirement] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        for required_timeframe in timeframes:
            requirements.append(
                DataRequirement(
                    symbol=symbol,
                    timeframe=required_timeframe,
                    start=str(start or "").strip(),
                    end=str(end or "").strip(),
                )
            )
    return requirements


def _resolve_intrabar_timeframe(base_timeframe: str, modeling_resolution: str | None) -> str | None:
    normalized = str(modeling_resolution or "").strip().lower()
    if normalized in {
        "",
        "main",
        "main_timeframe",
        "open_close",
        "open-close",
        base_timeframe.lower(),
    }:
        return None
    if normalized in {"m1", "1min", "min1"}:
        normalized = "1m"
    elif normalized in {"s1", "tick"}:
        pass
    else:
        normalized = normalize_timeframe(normalized)

    if normalized == "1-lower":
        try:
            base_index = TIMEFRAME_ORDER.index(base_timeframe)
        except ValueError:
            return None
        return TIMEFRAME_ORDER[base_index - 1] if base_index > 0 else None

    try:
        base_index = TIMEFRAME_ORDER.index(base_timeframe)
        target_index = TIMEFRAME_ORDER.index(normalized)
    except ValueError:
        return None
    if target_index >= base_index:
        return None
    return normalized


def _has_required_coverage(data_root: Path, requirement: DataRequirement) -> bool:
    coverage = _coverage_for(data_root, requirement.symbol, requirement.timeframe)
    if coverage is None:
        return False

    required_start = _normalize_coverage_date(requirement.start)
    required_end = _normalize_coverage_date(requirement.end)
    if required_start is None and required_end is None:
        return True

    available_start, available_end = coverage
    if required_start is not None and required_start < available_start:
        return False
    if required_end is not None and required_end > available_end:
        return False
    return True


def _coverage_for(data_root: Path, symbol: str, timeframe: str) -> tuple[str, str] | None:
    ohlcv_root = data_root / "ohlcv"
    matches = sorted(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None

    starts: list[str] = []
    ends: list[str] = []
    for path in matches:
        try:
            df = pd.read_parquet(path, columns=["timestamp"])
        except Exception as exc:
            logger.warning("Could not inspect OHLCV coverage for %s: %s", path, exc)
            continue
        if "timestamp" not in df.columns or df.empty:
            continue
        timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
        if timestamps.empty:
            continue
        starts.append(pd.Timestamp(timestamps.min()).date().isoformat())
        ends.append(pd.Timestamp(timestamps.max()).date().isoformat())

    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _normalize_coverage_date(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = pd.to_datetime(str(value), utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).date().isoformat()


def _is_forex_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if "/" in normalized:
        return True
    if len(normalized) == 6 and normalized.isalpha():
        return True
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        return normalized in {
            key.upper()
            for key, value in SYMBOL_CONFIG.items()
            if str(value.get("asset_class", "")).strip().lower() == "forex"
        }
    except Exception:
        return False


def _is_dukascopy_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        return normalized in {key.upper() for key in SYMBOL_CONFIG}
    except Exception:
        return False


def _is_us_intraday_stock(symbol: str, timeframe: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if normalized_timeframe not in US_INTRADAY_TIMEFRAMES:
        return False
    if _is_forex_symbol(normalized_symbol) or _is_dukascopy_symbol(normalized_symbol):
        return False
    if not _US_STOCK_SYMBOL_PATTERN.fullmatch(normalized_symbol):
        return False
    if normalized_symbol.endswith("USD") and len(normalized_symbol) > 5:
        return False
    return True


def _auto_download_source(symbol: str, timeframe: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if _is_dukascopy_symbol(normalized_symbol) or _is_forex_symbol(normalized_symbol):
        return "dukascopy"
    if _is_us_intraday_stock(normalized_symbol, normalized_timeframe):
        return "huggingface"
    return "yfinance"


def _auto_download_source_plan(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
) -> list[dict[str, str | None]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    default_source = _auto_download_source(normalized_symbol, normalized_timeframe)
    if default_source != "huggingface":
        return [{"source": default_source, "start": start, "end": end}]

    normalized_start = _normalize_coverage_date(start)
    normalized_end = _normalize_coverage_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        normalized_start, normalized_end = normalized_end, normalized_start

    if normalized_start is None and normalized_end is None:
        return [{"source": "alpaca", "start": None, "end": None}]

    plan: list[dict[str, str | None]] = []
    hf_start = normalized_start or normalized_end
    hf_end = normalized_end
    if hf_start is not None and hf_start > HF_INTRADAY_COVERAGE_END:
        hf_start = None
        hf_end = None
    elif hf_end is None or hf_end > HF_INTRADAY_COVERAGE_END:
        hf_end = HF_INTRADAY_COVERAGE_END
    if hf_start and hf_end and hf_start <= hf_end:
        plan.append({"source": "huggingface", "start": hf_start, "end": hf_end})

    alpaca_start = normalized_start or normalized_end
    alpaca_end = normalized_end
    if alpaca_end is None or alpaca_end >= ALPACA_INTRADAY_START:
        if alpaca_start is None or alpaca_start < ALPACA_INTRADAY_START:
            alpaca_start = ALPACA_INTRADAY_START
        if alpaca_end is None or alpaca_start <= alpaca_end:
            plan.append({"source": "alpaca", "start": alpaca_start, "end": alpaca_end or end})

    return plan or [{"source": "alpaca", "start": start, "end": end}]


def _download_missing_ohlcv_data(
    *,
    data_root: Path,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    source: str,
) -> None:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(timeframe) or "1d"
    selected_source = str(source or "").strip().lower() or "dukascopy"

    if selected_source == "huggingface":
        from data_tools.downloaders.huggingface_dl import HuggingFaceDownloader

        HuggingFaceDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source == "alpaca":
        from data_tools.downloaders.alpaca_dl import AlpacaDownloader

        try:
            AlpacaDownloader().download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=data_root,
                start=start,
                end=end,
            )
        except RuntimeError as exc:
            if "Missing Alpaca credentials" not in str(exc):
                raise
            logger.warning(
                "Alpaca credentials missing; falling back to yfinance for %s", normalized_symbol
            )
            from data_tools.downloaders.yfinance_dl import YFinanceDownloader

            YFinanceDownloader().download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=data_root,
                start=start,
                end=end,
            )
        return

    if selected_source == "yfinance":
        from data_tools.downloaders.yfinance_dl import YFinanceDownloader

        YFinanceDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source == "dolthub":
        from data_tools.downloaders.dolthub_dl import DolthubDownloader

        DolthubDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source in {"dukascopy", "jforex"}:
        from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader

        DukascopyNativeDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
            offer_side="bid",
        )
        return

    raise ValueError(
        f"Unsupported auto-download source '{selected_source}' for {normalized_symbol}"
    )


def _invalidate_catalog_cache(data_root: Path) -> None:
    try:
        from data_tools import cache_manager

        cache_manager.invalidate_after_download(data_root)
    except Exception as exc:
        logger.debug("Could not invalidate catalog cache after auto-download: %s", exc)
