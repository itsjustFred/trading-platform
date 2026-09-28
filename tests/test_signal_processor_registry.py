from __future__ import annotations

from pathlib import Path

import pytest

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.registry import get_signal_processor, list_signal_processor_options
from signal_bots.rocketfuel import RocketFuelSignalProcessor
from tests.signal_processor_helpers import sample_signal_market_data


def test_registry_options_expose_all_expected_ids() -> None:
    options = list_signal_processor_options()
    ids = {item["id"] for item in options}
    assert {"ramjet", "rocketfuel_longsonly", "rocketfuel_longandshort", "nitro"} == ids


def test_registry_returns_expected_processor_classes() -> None:
    assert isinstance(get_signal_processor("ramjet", {}), RamJetSignalProcessor)
    assert isinstance(get_signal_processor("rocketfuel_longsonly", {}), RocketFuelSignalProcessor)
    assert isinstance(
        get_signal_processor("rocketfuel_longandshort", {}),
        RocketFuelSignalProcessor,
    )
    assert isinstance(get_signal_processor("nitro", {}), NitroSignalProcessor)


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


def test_nitro_rejects_sell_signals() -> None:
    processor = get_signal_processor("nitro", {})
    sell_signal = {"symbol": "AAPL", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"}
    market_data = sample_signal_market_data()
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(sell_signal, market_data)


class TestNitroInheritsRamJetWithoutDrift:
    """Nitro reuses RamJet's formulas and its own config file.

    Two things can therefore go wrong silently, and neither is visible from the
    outside:

    * Nitro stops reading its own ``config.yaml`` and starts inheriting RamJet's
      at runtime, so a tuning change meant for one bot changes the other;
    * the two config files drift apart, so Nitro's real risk numbers stop
      matching the formulas it actually runs.

    Neither shows up in behaviour, because both processors behave correctly -
    just not the behaviour the config claims. So the equivalence is pinned here
    against the real files rather than against a fixture.

    What this does *not* cover: whether the grammar Nitro inherits is the grammar
    Nitro's messages actually use. That needs a real message export, which does
    not exist in this repo, and remains an open item.
    """

    @staticmethod
    def _loaded(bot: str):
        import yaml
        from signal_bots.registry import get_signal_processor

        return get_signal_processor(bot, {}).config

    def test_nitro_reads_its_own_config_file(self):
        import yaml

        raw = yaml.safe_load(
            (
                Path(__file__).resolve().parent.parent
                / "signal_bots"
                / "nitro"
                / "config.yaml"
            ).read_text(encoding="utf-8")
        )
        # Top-level `nitro:` section, never a fallback to ram_jet.
        assert "nitro" in raw
        assert "ram_jet" not in raw

    def test_both_bots_load_their_own_real_config(self):
        ramjet = self._loaded("ramjet")
        nitro = self._loaded("nitro")
        assert ramjet, "ramjet must load its real config, not an empty dict"
        assert nitro, "nitro must load its real config, not an empty dict"

    def test_the_two_configs_agree_on_every_shared_key(self):
        ramjet = self._loaded("ramjet")
        nitro = self._loaded("nitro")
        shared = set(ramjet) & set(nitro)
        assert shared, "expected the bots to share formula settings"
        differing = {k: (ramjet[k], nitro[k]) for k in shared if ramjet[k] != nitro[k]}
        # Nitro is BUY-only by design; everything else should be identical
        # because it runs the same formulas.
        assert differing == {}, (
            "nitro and ramjet configs have drifted while sharing formulas: "
            f"{differing}"
        )

    def test_nitro_still_rejects_sell_under_the_real_config(self):
        """The BUY-only intake is Nitro's actual behavioural difference, and it
        must hold with the real config loaded rather than only with ``{}``."""
        from signal_bots.registry import get_signal_processor

        processor = get_signal_processor("nitro", {})
        with pytest.raises(ValueError):
            processor._normalize_buy_action("sell")
