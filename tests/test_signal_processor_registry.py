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


def test_every_processor_acts_on_the_side_policy_it_declares() -> None:
    """Each bot declares which sides it trades; the code must enforce its own.

    The BUY-only restriction is stated three times across two processors with the
    same wording - "only supports BUY/LONG entry signals" - and only one of them
    was tested. `_normalize_buy_action` was pinned for Nitro. Removing the
    equivalent guard from **RamJet** passes all 106 tests in the registry and
    daemon files.

    That is the same shape as the optimizer embargo: the same rule implemented in
    more than one place, with a guard on one of them. And the consequence is
    directional, not cosmetic. These processors turn Discord messages into trades,
    so a "sell" that is no longer rejected is not ignored - it is normalised to
    "buy" and the position goes the wrong way. It is the same class as the negated
    buy that opened a long.

    RocketFuel is the counter-case that makes this worth deriving rather than
    hand-listing: it is deliberately two-way ("supports BUY/LONG and SELL/SHORT"),
    so the rule is not "all bots are BUY-only". The rule is that a processor's
    declared policy and its accepted actions agree - in both directions, so neither
    deleting a guard nor adding one to a two-way bot passes.
    """
    import inspect

    from signal_bots.registry import _BOT_DIRS, get_signal_processor

    def _normaliser(processor):
        """Find the action normaliser, whatever it is called.

        Nitro and RamJet name theirs `_normalize_buy_action`; RocketFuel names its
        `_normalize_action` and handles both sides. Assuming one name is how the
        first version of this test ended up not covering RocketFuel at all - its
        own precondition caught that, which is why the precondition is there.
        """
        for name in ("_normalize_buy_action", "_normalize_action"):
            if hasattr(processor, name):
                return name, getattr(processor, name)
        return None, None

    checked: dict[str, str] = {}
    for bot in sorted(_BOT_DIRS):
        try:
            processor = get_signal_processor(bot, {})
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            pytest.fail(f"could not build a processor for {bot!r}: {exc!r}")
        name, normalise = _normaliser(processor)
        if normalise is None:
            continue

        source = inspect.getsource(type(processor))
        declared_buy_only = "only supports BUY/LONG" in source

        accepts_buy = normalise("buy") == "buy"
        assert accepts_buy, f"{bot} no longer accepts a plain buy signal"

        try:
            result = normalise("sell")
        except ValueError:
            rejected_sell = True
            result = None
        else:
            rejected_sell = False
            # Three outcomes, not two. "Rejects a sell" and "accepts a sell" are
            # opposites, but a *third* case - returning "buy" for a sell input -
            # is neither. That is a sell signal silently becoming a long, which is
            # the exact defect class this branch has spent the session fixing
            # (a negated buy that opened a long; a negated momentum reversal that
            # stopped being an exit). It is wrong for every bot, BUY-only or not,
            # and an earlier version of this test scored it the same as a correct
            # two-way bot, so a mutation that forced RocketFuel to map sells onto
            # buys passed it.
            assert str(result).strip().lower() not in {"buy", "long"}, (
                f"{bot} maps a sell signal to {result!r} instead of rejecting it or "
                "returning a sell. A sell that silently becomes a buy opens a "
                "position in the opposite direction to the one the signal asked "
                "for, and no further test in this file would notice."
            )

        checked[bot] = f"buy-only via {name}" if declared_buy_only else f"two-way via {name}"
        assert declared_buy_only == rejected_sell, (
            f"{bot} declares {'BUY-only' if declared_buy_only else 'two-way'} in its "
            f"source, but it {'rejects' if rejected_sell else 'accepts'} a sell "
            f"signal (returned {result!r}). The declaration and the behaviour "
            "disagree, so either a guard was deleted or one was added to a bot "
            "that is meant to trade both ways."
        )

    # Precondition on the test's own reach: if the registry ever stops exposing
    # both kinds of processor, this would pass vacuously. It earned its keep
    # immediately - the first version of this test skipped RocketFuel entirely
    # because it assumed the normaliser was called `_normalize_buy_action`, and
    # this assertion is what noticed that the two-way arm was never exercised.
    policies = {value.split(" via ")[0] for value in checked.values()}
    assert "buy-only" in policies, (
        f"expected at least one BUY-only processor, found {checked} - the rule "
        "under test is no longer being exercised"
    )
    assert "two-way" in policies, (
        f"expected at least one two-way processor, found {checked} - the "
        "buy-only arm alone would not prove the check is two-sided"
    )
