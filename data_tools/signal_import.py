from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from shared.constants import normalize_timeframe

_SYMBOL_KEYS = ("symbol", "ticker", "instrument")
_ACTION_KEYS = ("action", "side", "signal_type", "signal")
_PRICE_KEYS = ("entry_price", "signal_price", "price")
_TIMESTAMP_KEYS = ("timestamp", "datetime", "signal_timestamp")
_DATE_KEYS = ("date", "datum")
_TIME_KEYS = ("time", "zeit", "zeit_ny", "time_ny")


def _normalize_header(value: str) -> str:
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("(", "_")
        .replace(")", "")
        .replace("/", "_")
        .replace("-", "_")
        .replace(" ", "_")
    )
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _detect_separator(path: Path) -> str:
    first_line = ""
    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        first_line = handle.readline()
    return ";" if first_line.count(";") >= first_line.count(",") else ","


def _find_column(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    return None


def _parse_decimal(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        numeric = float(text)
    except ValueError:
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _parse_action(value: Any) -> str | None:
    action = str(value or "").strip().lower()
    if action in {"buy", "long", "entry", "enter", "open", "open_long"}:
        return "buy"
    if action in {"sell", "short", "exit", "close", "close_long", "open_short"}:
        return "sell"
    if "short" in action:
        return "sell"
    if "sell" in action:
        return "sell"
    if "buy" in action or "long" in action:
        return "buy"
    return None


def _is_intraday_timestamp(value: pd.Timestamp) -> bool:
    return any((value.hour, value.minute, value.second))


def _parse_timestamp(
    row: pd.Series,
    *,
    timestamp_column: str | None,
    date_column: str | None,
    time_column: str | None,
) -> tuple[datetime | None, str]:
    has_ny_time = bool(time_column and "ny" in _normalize_header(time_column))
    source_timezone = "America/New_York" if has_ny_time else "UTC"

    timestamp_value = str(row.get(timestamp_column, "")).strip() if timestamp_column else ""
    date_value = str(row.get(date_column, "")).strip() if date_column else ""
    time_value = str(row.get(time_column, "")).strip() if time_column else ""

    parsed: pd.Timestamp | pd.NaT = pd.NaT
    if timestamp_value:
        parsed = pd.to_datetime(timestamp_value, errors="coerce")
    elif date_value and time_value:
        parsed = pd.to_datetime(f"{date_value} {time_value}", errors="coerce")
    elif date_value:
        parsed = pd.to_datetime(date_value, errors="coerce")

    if pd.isna(parsed):
        return None, normalize_timeframe("1d")

    ts = pd.Timestamp(parsed)
    if ts.tzinfo is None:
        ts = ts.tz_localize(source_timezone)
    timeframe = normalize_timeframe("1m") if _is_intraday_timestamp(ts) or bool(time_value) else normalize_timeframe("1d")
    ts_utc = ts.tz_convert("UTC")
    return ts_utc.to_pydatetime(), timeframe


def load_signals_from_csv(filepath: str) -> list[dict[str, Any]]:
    """Load replay signals from a CSV file.

    Returns dict payloads compatible with `ReplaySignal.from_payload(...)`.
    """
    path = Path(filepath).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Signal CSV not found: {path}")

    separator = _detect_separator(path)
    frame = pd.read_csv(path, sep=separator, dtype=str, encoding="utf-8-sig", keep_default_na=False, na_filter=False)
    if frame.empty:
        return []

    normalized_columns = {_normalize_header(column): column for column in frame.columns}
    symbol_column = _find_column(normalized_columns, _SYMBOL_KEYS)
    if symbol_column is None:
        raise ValueError("CSV requires a symbol column (e.g. Symbol)")

    action_column = _find_column(normalized_columns, _ACTION_KEYS)
    price_column = _find_column(normalized_columns, _PRICE_KEYS)
    timestamp_column = _find_column(normalized_columns, _TIMESTAMP_KEYS)
    date_column = _find_column(normalized_columns, _DATE_KEYS)
    time_column = _find_column(normalized_columns, _TIME_KEYS)

    if timestamp_column is None and date_column is None:
        raise ValueError(
            "CSV requires either a timestamp column or a date column (+ optional time column)"
        )
    if price_column is None:
        raise ValueError("CSV requires an entry/price column (e.g. Signal Price)")

    source_label = f"csv:{path.name}"
    signals: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        symbol = str(row.get(symbol_column, "")).strip().upper()
        if not symbol:
            continue

        entry_price = _parse_decimal(row.get(price_column))
        if entry_price is None or entry_price <= 0:
            continue

        timestamp, inferred_timeframe = _parse_timestamp(
            row,
            timestamp_column=timestamp_column,
            date_column=date_column,
            time_column=time_column,
        )
        if timestamp is None:
            continue

        action = _parse_action(row.get(action_column)) if action_column else "buy"
        if action is None:
            continue
        metadata: dict[str, Any] = {
            "csv_file": str(path),
            "csv_row": int(index) + 2,
        }
        for key, value in row.items():
            normalized_key = _normalize_header(key)
            if normalized_key in {
                _normalize_header(symbol_column),
                _normalize_header(price_column),
            }:
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            metadata[normalized_key] = text

        signals.append(
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "action": action,
                "entry_price": float(entry_price),
                "timeframe": inferred_timeframe,
                "source": source_label,
                "metadata": metadata,
            }
        )

    signals.sort(key=lambda item: item["timestamp"])
    return signals
