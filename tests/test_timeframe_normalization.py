from shared.constants import normalize_timeframe


def test_normalize_timeframe_aliases_to_canonical_values():
    assert normalize_timeframe("m1") == "1m"
    assert normalize_timeframe("h1") == "1h"
    assert normalize_timeframe("d1") == "1d"


def test_normalize_timeframe_preserves_canonical_and_unknown_values():
    assert normalize_timeframe("1m") == "1m"
    assert normalize_timeframe("4h") == "4h"
    assert normalize_timeframe("unknown-tf") == "unknown-tf"
    assert normalize_timeframe("") == ""
