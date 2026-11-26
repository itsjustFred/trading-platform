from __future__ import annotations

import pytest

from signal_bots.registry import get_signal_processor, list_signal_processor_options
from tests.signal_processor_helpers import sample_signal_market_data


def test_registry_options_expose_all_expected_ids() -> None:
    options = list_signal_processor_options()
    ids = {item["id"] for item in options}
    assert {"ramjet", "rocketfuel_longsonly", "rocketfuel_longandshort", "nitro"} == ids


def test_rocketfuel_variants_differ_on_sell_signal() -> None:
    market_data = sample_signal_market_data()
    long_only = get_signal_processor("rocketfuel_longsonly", {})
    long_short = get_signal_processor("rocketfuel_longandshort", {})
    signal_payload = {"symbol": "AMD", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"}

    long_only_signal = long_only.process_signal(signal_payload, market_data)
    long_short_signal = long_short.process_signal(signal_payload, market_data)

    assert long_only_signal.metadata.get("exit_only") is True
    assert long_only_signal.stop_loss is None
    assert long_short_signal.stop_loss is not None
    assert float(long_short_signal.stop_loss) > long_short_signal.entry_price


@pytest.mark.parametrize("name", ["../x", "doesnotexist", "ramjet/../nitro", ""])
def test_unknown_processor_ids_never_yield_a_filesystem_path(name: str) -> None:
    from signal_bots.registry import (
        get_signal_processor_config_path,
        get_signal_processor_module_path,
    )

    with pytest.raises(ValueError, match="Unknown signal processor"):
        get_signal_processor_module_path(name)
    with pytest.raises(ValueError, match="Unknown signal processor"):
        get_signal_processor_config_path(name)
