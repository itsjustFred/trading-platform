"""Live trading runner package."""

from typing import Any

# The names below are provided lazily by __getattr__, so F822 (preview) cannot see them.
__all__ = ["AlpacaLiveAdapter", "BaseLiveAdapter", "CTraderLiveAdapter"]  # noqa: F822


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
