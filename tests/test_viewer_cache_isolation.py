"""Regression coverage for viewer process-global cache isolation (#236)."""

import time


def _strategy_cache_payload():
    import viewer.main as viewer_app_module

    with viewer_app_module._STRATEGIES_CACHE_LOCK:
        return list(viewer_app_module._STRATEGIES_CACHE["payload"])


def test_strategy_cache_can_be_poisoned_for_isolation_check():
    import viewer.main as viewer_app_module

    with viewer_app_module._STRATEGIES_CACHE_LOCK:
        viewer_app_module._STRATEGIES_CACHE["payload"] = [{"strategy": "poison"}]
        viewer_app_module._STRATEGIES_CACHE["expires_at"] = time.time() + 60

    assert _strategy_cache_payload() == [{"strategy": "poison"}]


def test_strategy_cache_is_reset_before_each_test():
    import viewer.main as viewer_app_module

    with viewer_app_module._STRATEGIES_CACHE_LOCK:
        assert viewer_app_module._STRATEGIES_CACHE["payload"] == []
        assert viewer_app_module._STRATEGIES_CACHE["expires_at"] <= time.time()
