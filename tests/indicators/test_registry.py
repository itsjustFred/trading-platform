"""Skeleton-contract tests for ``shared.indicators.registry`` (P0)."""

from __future__ import annotations

import pytest

from shared.indicators import registry


def test_register_and_get_roundtrip():
    @registry.register_indicator(
        "test_indicator_p0", version=1, warmup_bars=3, display="overlay"
    )
    def compute(df, params):
        return {}

    spec = registry.get_indicator("test_indicator_p0")
    assert spec.version == 1
    assert spec.warmup_bars == 3
    assert spec.display == "overlay"
    assert spec.compute is compute
    registry.get_indicator("test_indicator_p0", version=1)


def test_duplicate_registration_fails_closed():
    registry.register_indicator("dup_p0", version=1)(lambda df, p: {})

    def other(df, params):
        return {}

    with pytest.raises(ValueError, match="already registered"):
        registry.register_indicator("dup_p0", version=1)(other)


def test_unknown_indicator_raises():
    with pytest.raises(KeyError):
        registry.get_indicator("does_not_exist_p0")


def test_invalid_display_and_version_rejected():
    with pytest.raises(ValueError, match="display"):
        registry.register_indicator("bad_display_p0", display="side")(
            lambda df, p: {}
        )
    with pytest.raises(ValueError, match="version"):
        registry.register_indicator("bad_version_p0", version=0)(
            lambda df, p: {}
        )


def test_list_indicators_sorted():
    names = [s.name for s in registry.list_indicators()]
    assert names == sorted(names)
