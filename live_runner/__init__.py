"""Live trading runner package."""

from typing import Any

__all__ = ["AlpacaLiveAdapter", "BaseLiveAdapter", "CTraderLiveAdapter"]


def __getattr__(name: str) -> Any:
    if name == "AlpacaLiveAdapter":
        from live_runner.alpaca_adapter import AlpacaLiveAdapter

        return AlpacaLiveAdapter
    if name == "BaseLiveAdapter":
        from live_runner.base_adapter import BaseLiveAdapter

        return BaseLiveAdapter
    if name == "CTraderLiveAdapter":
        from live_runner.ctrader_adapter import CTraderLiveAdapter

        return CTraderLiveAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
