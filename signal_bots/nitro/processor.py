from __future__ import annotations

from typing import Any

from risk.models import TradeSignal
from signal_bots.ramjet.processor import RamJetSignalProcessor


class NitroSignalProcessor(RamJetSignalProcessor):
    """Nitro processor: RamJet formulas, but BUY-only signal intake."""

    @staticmethod
    def _normalize_buy_action(raw_action: Any) -> str:
        action = str(raw_action or "").strip().lower()
        if action in {"buy", "long"}:
            return "buy"
        raise ValueError("NitroSignalProcessor only supports BUY/LONG entry signals")

    def process_signal(
        self,
        raw_signal: dict[str, Any],
        market_data,
    ) -> TradeSignal:
        signal = super().process_signal(raw_signal=raw_signal, market_data=market_data)
        metadata = dict(signal.metadata)
        metadata["source"] = "nitro"
        metadata["signal_bot"] = "nitro"
        metadata["derived_from"] = "ramjet"
        return TradeSignal(
            action=signal.action,
            symbol=signal.symbol,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            timeframe=signal.timeframe,
            metadata=metadata,
        )
