"""``shared.constants.normalize_symbols``: the one symbol-list normalizer."""

from shared.constants import normalize_symbols


def test_strips_uppercases_and_drops_blanks() -> None:
    assert normalize_symbols([" aapl ", "MSFT", "", "   ", "spy"]) == ["AAPL", "MSFT", "SPY"]


def test_preserves_order_and_does_not_dedupe() -> None:
    # Order is data (the daemon iterates it) and a repeat is an operator's choice, not a
    # normalizer's to collapse.
    assert normalize_symbols(["msft", "aapl", "msft"]) == ["MSFT", "AAPL", "MSFT"]


def test_absent_input_is_an_empty_list() -> None:
    assert normalize_symbols(None) == []
    assert normalize_symbols([]) == []
