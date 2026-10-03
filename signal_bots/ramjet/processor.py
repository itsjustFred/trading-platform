from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from risk.models import TradeSignal
from shared.constants import normalize_timeframe

_TIMEFRAME_ALIASES: dict[str, str] = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "60min": "1h",
}


def _resolve_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = normalize_timeframe(text)
    if normalized:
        return _TIMEFRAME_ALIASES.get(normalized, normalized)
    return _TIMEFRAME_ALIASES.get(text, text)


class RamJetSignalProcessor:
    """RamJet signal processor with formula-identical SL/TP logic.

    Ported from `/home/ubuntu/Desktop/stockkit_ai_bot`:
    - ATR via EMA(TR, span=atr_period)
    - SL: entry - ATR * sl_multiplier
    - TP1: entry + ATR * tp1_multiplier
    - Trailing model: breakeven after TP1 + two-phase Supertrend flip workflow
    """

    def __init__(self, config: dict[str, Any]):
        loaded = dict(config or {})
        if isinstance(loaded.get("ram_jet"), dict):
            loaded = dict(loaded["ram_jet"])
        self.config = loaded

        self.atr_period = int(self.config.get("atr_period", 14))
        self.sl_multiplier = float(self.config.get("sl_multiplier", 2.0))
        self.tp1_multiplier = float(self.config.get("tp1_multiplier", 3.0))
        self.tp1_exit_percent = float(self.config.get("tp1_exit_percent", 0.50))
        self.supertrend_period = int(self.config.get("supertrend_period", 10))
        self.supertrend_multiplier = float(self.config.get("supertrend_multiplier", 3.0))
        self.default_timeframe = (
            _resolve_timeframe(self.config.get("candle_timeframe", "5m")) or "5m"
        )

    @classmethod
    def from_config_file(cls, config_path: str | Path) -> "RamJetSignalProcessor":
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
        # `min_periods` is the strategies' own contract, not a new one: every ATR in
        # `strategies/` is `tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False)`,
        # which is NaN until `period` bars exist, so a signal built on it is impossible
        # rather than merely unreliable. Omitting it here produced a finite positive ATR
        # from the *first* bar, which the guard below cannot see - it only rejects a
        # non-finite or non-positive value, and an under-warmed one is neither. Measured on
        # a growing-range series at period 14: the 3-bar ATR is 89.6% of the warmed 14-bar
        # ATR, so the stop was materially tighter, from the same signal, in exactly the
        # regime where the backtest declines to produce a number at all.
        #
        # The alpha is deliberately left alone. Span-based smoothing and Wilder's differ by
        # a few percent with an unstable sign, and no backtest characterised it - adopting
        # Wilder's would change sizing on the strength of a ratio measurement has already
        # shown is noise. Backlog item 5, option 1 only.
        #
        # Confined by construction: `min_periods` only gates *whether* a value is produced,
        # not which observations feed the recursion, so every bar from `period - 1` onward
        # is bit-identical to before. Only the warm-up window changes, and it now lands in
        # the guard's existing fail-closed path rather than inventing a new one.
        frame["atr"] = frame["tr"].ewm(span=period, min_periods=period, adjust=False).mean()
        frame = frame.drop(columns=["h_l", "h_pc", "l_pc", "tr"])
        return frame

    @staticmethod
    def _calculate_supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
        frame = df.copy()
        st_dir_found = False

        if hasattr(frame, "ta"):
            st = frame.ta.supertrend(length=period, multiplier=multiplier)
            if isinstance(st, pd.DataFrame):
                st_dir_col = f"SUPERTd_{period}_{float(multiplier)}"
                if st_dir_col not in st.columns:
                    for column in st.columns:
                        if str(column).startswith("SUPERTd"):
                            st_dir_col = str(column)
                            break
                if st_dir_col in st.columns:
                    frame["supertrend"] = st[st_dir_col] == 1
                    # Same warm-up contract as the fallback below: pandas_ta
                    # emits a direction for every bar, including the early ones
                    # whose ATR is still converging, and process_signal reads
                    # supertrend_ready as ready when the column is missing —
                    # so the marker must be set here too, or the warm-up bar
                    # would be reported as a genuine direction.
                    frame["supertrend_ready"] = np.arange(len(frame)) >= int(period)
                    st_dir_found = True

        if not st_dir_found:
            hl2 = (frame["high"] + frame["low"]) / 2

            frame["h_l_st"] = frame["high"] - frame["low"]
            frame["h_pc_st"] = (frame["high"] - frame["close"].shift(1)).abs()
            frame["l_pc_st"] = (frame["low"] - frame["close"].shift(1)).abs()
            frame["tr_st"] = frame[["h_l_st", "h_pc_st", "l_pc_st"]].max(axis=1)
            # First bar has no previous close: seed its TR with high - low so
            # the ATR recursion starts from a real range, not 0.
            frame["tr_st"] = frame["tr_st"].fillna(frame["h_l_st"])
            frame["atr_st"] = frame["tr_st"].ewm(alpha=1 / period, adjust=False).mean()

            basic_upper = (hl2 + (multiplier * frame["atr_st"])).to_numpy()
            basic_lower = (hl2 - (multiplier * frame["atr_st"])).to_numpy()
            close = frame["close"].to_numpy()

            n_rows = len(frame)
            final_upper = np.full(n_rows, np.nan)
            final_lower = np.full(n_rows, np.nan)
            # Fail-closed seeding (Cleanup #35): before the ATR has enough
            # data the Supertrend direction is unknown and is reported as
            # bearish/False. The old code seeded an all-True array, so early
            # bars compared close against uninitialized zero bands and
            # reported a green Supertrend derived from nothing.
            supertrend = np.zeros(n_rows, dtype=bool)

            first_valid_idx = 0
            for i in range(n_rows):
                if not np.isnan(basic_upper[i]):
                    first_valid_idx = i
                    break

            if n_rows > first_valid_idx:
                final_upper[first_valid_idx] = basic_upper[first_valid_idx]
                final_lower[first_valid_idx] = basic_lower[first_valid_idx]
                supertrend[first_valid_idx] = bool(
                    close[first_valid_idx] >= final_lower[first_valid_idx]
                )

            for i in range(first_valid_idx + 1, n_rows):
                if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
                    final_upper[i] = basic_upper[i]
                else:
                    final_upper[i] = final_upper[i - 1]

                if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
                    final_lower[i] = basic_lower[i]
                else:
                    final_lower[i] = final_lower[i - 1]

                prev_st = supertrend[i - 1]
                if prev_st:
                    supertrend[i] = close[i] > final_lower[i]
                else:
                    supertrend[i] = close[i] >= final_upper[i]

            frame["supertrend"] = supertrend
            # Warm-up marker (Pre-mortem strategies #3, PR #61): with the
            # fail-closed zero seed, "not yet computable" and "bearish" are
            # indistinguishable in the bool column. supertrend_ready is False
            # until the recursion has period bars of history after the first
            # computable bar; consumers (e.g. the trailing-stop flip workflow)
            # must not treat pre-warmup values as a genuine direction.
            warmup_bars = max(int(period), 1)
            frame["supertrend_ready"] = (
                np.arange(n_rows) >= first_valid_idx + warmup_bars
            )
            frame = frame.drop(
                columns=["h_l_st", "h_pc_st", "l_pc_st", "tr_st", "atr_st"],
                errors="ignore",
            )

        return frame

    @staticmethod
    def _normalize_buy_action(raw_action: Any) -> str:
        action = str(raw_action or "").strip().lower()
        if action in {"buy", "long"}:
            return "buy"
        raise ValueError("RamJetSignalProcessor only supports BUY/LONG entry signals")

    def process_signal(
        self,
        raw_signal: dict[str, Any],
        market_data: pd.DataFrame,
    ) -> TradeSignal:
        symbol = str(raw_signal.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("raw_signal must include symbol")

        action = self._normalize_buy_action(raw_signal.get("action"))

        frame = self._prepare_market_data(market_data)
        frame = self._calculate_atr(frame, self.atr_period)
        frame = self._calculate_supertrend(
            frame,
            self.supertrend_period,
            self.supertrend_multiplier,
        )

        entry_price = float(frame["close"].iloc[-1])
        atr_value = float(frame["atr"].iloc[-1])
        if not np.isfinite(atr_value) or atr_value <= 0:
            raise ValueError("Latest ATR value is invalid for SL/TP calculation")

        stop_loss = entry_price - (atr_value * self.sl_multiplier)
        take_profit = entry_price + (atr_value * self.tp1_multiplier)
        latest_supertrend_green = bool(frame["supertrend"].iloc[-1])
        # False while the frame is shorter than the Supertrend warm-up: in that
        # regime supertrend=False means "unknown", not "bearish", so consumers
        # of supertrend_green_at_entry / the trailing flip workflow must not
        # read a direction into it. Both Supertrend paths above set the marker
        # (the pandas_ta branch for the library's own warm-up, the fallback for
        # the fail-closed zero seed), so a missing column is unexpected and
        # stays fail-closed instead of silently reporting ready.
        supertrend_ready = (
            bool(frame["supertrend_ready"].iloc[-1])
            if "supertrend_ready" in frame.columns
            else False
        )

        timeframe = _resolve_timeframe(raw_signal.get("timeframe")) or self.default_timeframe

        base_metadata = raw_signal.get("metadata", {})
        metadata = dict(base_metadata) if isinstance(base_metadata, dict) else {}
        metadata.update(
            {
                "source": "ramjet",
                "signal_bot": "ramjet",
                "ramjet_config": {
                    "atr_period": self.atr_period,
                    "sl_multiplier": self.sl_multiplier,
                    "tp1_multiplier": self.tp1_multiplier,
                    "tp1_exit_percent": self.tp1_exit_percent,
                    "supertrend_period": self.supertrend_period,
                    "supertrend_multiplier": self.supertrend_multiplier,
                },
                "atr": atr_value,
                "tp1_price": take_profit,
                "tp1_exit_percent": self.tp1_exit_percent,
                "supertrend_green_at_entry": latest_supertrend_green,
                "supertrend_ready": supertrend_ready,
                "trailing_sl_config": {
                    "mode": "supertrend_flip",
                    "breakeven_after_tp1": True,
                    "activation_phase": "wait_for_first_green_after_tp1",
                    "exit_phase": "exit_on_first_red_after_activation",
                    "supertrend_period": self.supertrend_period,
                    "supertrend_multiplier": self.supertrend_multiplier,
                },
            }
        )

        if "timestamp" in raw_signal and raw_signal.get("timestamp") is not None:
            metadata["signal_timestamp"] = str(raw_signal.get("timestamp"))
        # C1: signal-vs-fill attribution is never dropped and slippage is
        # visible. The fill (latest close) overwrites the entry by design;
        # signal_price falls back to the fill itself (slippage 0, explicit).
        incoming_price = raw_signal.get("signal_price")
        try:
            signal_price = float(incoming_price) if incoming_price is not None else entry_price
        except (TypeError, ValueError):
            signal_price = entry_price
        metadata["signal_price"] = signal_price
        metadata["slippage_vs_signal"] = entry_price - signal_price

        return TradeSignal(
            action=action,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timeframe=timeframe,
            metadata=metadata,
        )
