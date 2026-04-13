"""Reusable signal processor packages."""

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.registry import (
    SIGNAL_PROCESSOR_OPTIONS,
    SIGNAL_PROCESSORS,
    get_signal_processor,
    list_signal_processor_options,
)
from signal_bots.rocketfuel import RocketFuelSignalProcessor

__all__ = [
    "RamJetSignalProcessor",
    "RocketFuelSignalProcessor",
    "NitroSignalProcessor",
    "SIGNAL_PROCESSORS",
    "SIGNAL_PROCESSOR_OPTIONS",
    "get_signal_processor",
    "list_signal_processor_options",
]
