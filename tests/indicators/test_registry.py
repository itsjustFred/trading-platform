"""Skeleton-contract tests for ``shared.indicators.registry`` (P0)."""

from __future__ import annotations

import pytest

from shared.indicators import registry


@pytest.fixture(autouse=True)
def _restore_registry():
    snapshot = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(snapshot)


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

    re_register = registry.register_indicator("dup_p0", version=1)
    with pytest.raises(ValueError, match="already registered"):
        re_register(other)


def test_same_spec_reregistration_is_idempotent():
    def compute(df, params):
        return {}

    params = {"window": {"type": "int", "default": 14}}
    registry.register_indicator(
        "idem_p0", version=1, params=params, warmup_bars=2
    )(compute)
    registry.register_indicator(
        "idem_p0", version=1, params=params, warmup_bars=2
    )(compute)
    assert registry.get_indicator("idem_p0").compute is compute


def test_same_function_with_different_spec_fails_closed():
    def compute(df, params):
        return {}

    registry.register_indicator("spec_p0", version=1, warmup_bars=1)(compute)
    re_register = registry.register_indicator("spec_p0", version=1, warmup_bars=2)
    with pytest.raises(ValueError, match="already registered"):
        re_register(compute)


def test_params_are_deep_copied_and_frozen():
    params = {"window": {"type": "int", "default": 14, "choices": [5, 14]}}
    registry.register_indicator("frozen_p0", version=1, params=params)(
        lambda df, p: {}
    )
    params["window"]["default"] = 99
    params["window"]["choices"].append(99)
    spec = registry.get_indicator("frozen_p0")
    assert spec.params["window"]["default"] == 14
    assert spec.params["window"]["choices"] == (5, 14)
    with pytest.raises(TypeError):
        spec.params["window"]["default"] = 100


def test_unknown_indicator_raises():
    with pytest.raises(KeyError):
        registry.get_indicator("does_not_exist_p0")


def test_invalid_display_and_version_rejected():
    bad_display = registry.register_indicator("bad_display_p0", display="side")
    with pytest.raises(ValueError, match="display"):
        bad_display(lambda df, p: {})

    bad_version = registry.register_indicator("bad_version_p0", version=0)
    with pytest.raises(ValueError, match="version"):
        bad_version(lambda df, p: {})


def test_invalid_name_and_bool_version_rejected():
    bad_name = registry.register_indicator("", version=1)
    with pytest.raises(ValueError, match="name"):
        bad_name(lambda df, p: {})

    bool_version = registry.register_indicator("bool_version_p0", version=True)
    with pytest.raises(ValueError, match="version"):
        bool_version(lambda df, p: {})


def test_list_indicators_sorted():
    names = [s.name for s in registry.list_indicators()]
    assert names == sorted(names)
