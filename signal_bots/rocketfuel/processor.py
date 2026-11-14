from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from live_runner.base_adapter import UNBOUNDED_TP_POLICY
from risk.models import TradeSignal
from shared.constants import normalize_timeframe

_TIMEFRAME_ALIASES: dict[str, str] = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "60min": "1h",
}

_ROCKETFUEL_VARIANTS = {"longsonly", "longandshort"}


def _resolve_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = normalize_timeframe(text)
    if normalized:
        return _TIMEFRAME_ALIASES.get(normalized, normalized)
    return _TIMEFRAME_ALIASES.get(text, text)


class RocketFuelSignalProcessor:
    """RocketFuel RSI strategy processor (ATR hard-stop, optional short entry)."""

    def __init__(self, config: dict[str, Any]):
        loaded = dict(config or {})
        if isinstance(loaded.get("rocket_fuel"), dict):
            loaded = dict(loaded["rocket_fuel"])
        self.config = loaded

        self.atr_period = int(self.config.get("atr_period", 14))
        self.sl_multiplier = float(self.config.get("sl_multiplier", 3.0))
        self.default_timeframe = (
            _resolve_timeframe(self.config.get("candle_timeframe", "5m")) or "5m"
        )

        configured_variant = str(self.config.get("variant") or "longsonly").strip().lower()
        if configured_variant not in _ROCKETFUEL_VARIANTS:
            raise ValueError(
                f"Unsupported RocketFuel variant '{configured_variant}'. "
                "Use 'longsonly' or 'longandshort'."
            )
        self.variant = configured_variant

    @classmethod
    def from_config_file(cls, config_path: str | Path) -> "RocketFuelSignalProcessor":
        path = Path(config_path)
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Config file {path} must decode to a mapping")
        return cls(payload)

    @staticmethod
    def _prepare_market_data(market_data: pd.DataFrame) -> pd.DataFrame:
        if market_data is None or market_data.empty:
            raise ValueError("market_data must contain OHLCV rows")
        frame = market_data.copy()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        required = {"open", "high", "low", "close"}
        missing = required.difference(frame.columns)
        if missing:
            missing_cols = ", ".join(sorted(missing))
            raise ValueError(f"market_data missing required columns: {missing_cols}")
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=sorted(required))
        if frame.empty:
            raise ValueError("market_data has no valid OHLC rows after cleaning")
        return frame

    @staticmethod
    def _calculate_atr(df: pd.DataFrame, period: int) -> pd.DataFrame:
        frame = df.copy()
        frame["h_l"] = frame["high"] - frame["low"]
        frame["h_pc"] = (frame["high"] - frame["close"].shift(1)).abs()
        frame["l_pc"] = (frame["low"] - frame["close"].shift(1)).abs()
        frame["tr"] = frame[["h_l", "h_pc", "l_pc"]].max(axis=1)
        frame["atr"] = frame["tr"].ewm(span=period, adjust=False).mean()
        return frame.drop(columns=["h_l", "h_pc", "l_pc", "tr"])

    @staticmethod
    def _normalize_action(raw_action: Any) -> Literal["buy", "sell"]:
        action = str(raw_action or "").strip().lower()
        if action in {"buy", "long"}:
            return "buy"
        if action in {"sell", "short"}:
            return "sell"
        raise ValueError("RocketFuelSignalProcessor supports BUY/LONG and SELL/SHORT actions")

    def process_signal(
        self,
        raw_signal: dict[str, Any],
        market_data: pd.DataFrame,
    ) -> TradeSignal:
        symbol = str(raw_signal.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("raw_signal must include symbol")

        action = self._normalize_action(raw_signal.get("action"))
        frame = self._prepare_market_data(market_data)
        frame = self._calculate_atr(frame, self.atr_period)

        entry_price = float(frame["close"].iloc[-1])
        atr_value = float(frame["atr"].iloc[-1])
        if not np.isfinite(atr_value) or atr_value <= 0:
            raise ValueError("Latest ATR value is invalid for RocketFuel SL calculation")

        stop_loss: float | None
        raw_metadata = raw_signal.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata if isinstance(raw_metadata, dict) else {})
        if action == "buy":
            stop_loss = entry_price - (atr_value * self.sl_multiplier)
        elif self.variant == "longandshort":
            stop_loss = entry_price + (atr_value * self.sl_multiplier)
        else:
            stop_loss = None
            metadata["exit_only"] = True

        timeframe = _resolve_timeframe(raw_signal.get("timeframe")) or self.default_timeframe
        metadata.update(
            {
                "source": "rocketfuel",
                "signal_bot": "rocketfuel",
                "rocketfuel_variant": self.variant,
                "atr": atr_value,
                "sl_multiplier": self.sl_multiplier,
                "processor_mode": (
                    "long-and-short"
                    if self.variant == "longandshort"
                    else "long-only-exit-on-sell"
                ),
            }
        )
        if "timestamp" in raw_signal and raw_signal.get("timestamp") is not None:
            metadata["signal_timestamp"] = str(raw_signal.get("timestamp"))
        # C1: signal-vs-fill attribution, same contract as RamJet.
        incoming_price = raw_signal.get("signal_price")
        try:
            signal_price = float(incoming_price) if incoming_price is not None else entry_price
        except (TypeError, ValueError):
            signal_price = entry_price
        metadata["signal_price"] = signal_price
        metadata["slippage_vs_signal"] = entry_price - signal_price
        # C1 (owner decision): RocketFuel exits on the opposite signal, so it
        # carries no bounded take profit. That is an EXPLICIT unbounded flag,
        # never a silent None the daemon would invent a bracket for.
        metadata["take_profit_policy"] = UNBOUNDED_TP_POLICY

        return TradeSignal(
            action=action,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=None,
            timeframe=timeframe,
            metadata=metadata,
        )
