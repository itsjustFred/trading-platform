"""Execution-model settings for the native engine.

Records the native engine's fill/trigger assumptions with each trade so
backtest results are auditable (what timing, which price side, which
same-bar priority). Metadata-only today; later branches wire these fields
into fill or trigger mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runner.engines.base import BacktestConfig


@dataclass(frozen=True)
class ExecutionModelSettings:
    """Native execution-model assumptions recorded with each trade.

    The native engine currently enforces ``same_bar_order``. The other fields
    document existing runtime behavior for auditability; they are metadata-only
    until a later branch wires them into fill or trigger mechanics.
    """

    market_order_timing: str = "next_bar_open"
    entry_price_side: str = "open"
    stop_trigger_side: str = "bar_low_high"
    take_profit_trigger_side: str = "bar_high_low"
    same_bar_order: str = "stop_first"
    #: Owner decision 2026-11-13: a bar that opens beyond a stop/target fills at
    #: its open (``"open"``), not at the level. Intrabar touches fill at the level.
    protective_gap_fill: str = "open"
    #: Where signal-driven exits fill (``BacktestConfig.signal_exit_fill``).
    signal_exit_fill: str = "next_open"
    intrabar_resolution: str | None = None
    lower_timeframe_replay: bool = False

    @classmethod
    def from_config(cls, config: BacktestConfig) -> "ExecutionModelSettings":
        params = dict(config.parameters or {})
        nested = params.get("execution_model")
        if isinstance(nested, dict):
            params = {**params, **nested}

        modeling_resolution = str(config.modeling_resolution or "").strip()
        intrabar_resolution = (
            modeling_resolution
            if modeling_resolution
            and modeling_resolution not in {"main_timeframe", "open_close", config.timeframe}
            else None
        )
        # Conformance (Cleanup 16-A): same-bar priority is a shared contract
        # resolved by intrabar chronology or the stop-first fallback — a
        # configured tp-first order is no longer honored by the native engine
        # (it stays visible in the audit metadata as ``stop_first``).
        same_bar_order = "stop_first"

        return cls(
            market_order_timing=str(params.get("market_order_timing") or "next_bar_open"),
            entry_price_side=str(params.get("entry_price_side") or "open"),
            stop_trigger_side=str(params.get("stop_trigger_side") or "bar_low_high"),
            take_profit_trigger_side=str(params.get("take_profit_trigger_side") or "bar_high_low"),
            same_bar_order=same_bar_order,
            signal_exit_fill=str(getattr(config, "signal_exit_fill", "next_open")),
            intrabar_resolution=intrabar_resolution,
            lower_timeframe_replay=intrabar_resolution is not None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_order_timing": self.market_order_timing,
            "entry_price_side": self.entry_price_side,
            "stop_trigger_side": self.stop_trigger_side,
            "take_profit_trigger_side": self.take_profit_trigger_side,
            "same_bar_order": self.same_bar_order,
            "protective_gap_fill": self.protective_gap_fill,
            "signal_exit_fill": self.signal_exit_fill,
            "intrabar_resolution": self.intrabar_resolution,
            "lower_timeframe_replay": self.lower_timeframe_replay,
        }
