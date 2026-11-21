"""Regression coverage for viewer process-global cache isolation (#236)."""

import sys
import time


def _poison_viewer_state() -> None:
    """Fill every cache ``_reset_viewer_runtime_state`` covers with a marker."""
    import viewer.main as m
    from viewer import core as legacy

    far_future = time.monotonic() + 3600
    with m._STRATEGIES_CACHE_LOCK:
        m._STRATEGIES_CACHE["payload"] = [{"strategy": "poison"}]
        m._STRATEGIES_CACHE["expires_at"] = far_future
    with m._LIVE_ACCOUNTS_CACHE_LOCK:
        m._LIVE_ACCOUNTS_CACHE["expires_at"] = far_future
        m._LIVE_ACCOUNTS_CACHE["payload"] = [{"account": "poison"}]
        m._LIVE_ACCOUNTS_CACHE["provider_marker"] = "poison"
    with m._BACKTEST_META_CACHE_LOCK:
        m._BACKTEST_META_CACHE["poison"] = {"id": "poison"}
        m._BACKTEST_META_CACHE_SOURCE_MTIME_NS = 12345
    with m._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        m._BACKTEST_EQUITY_PREVIEW_CACHE[("poison", 50)] = [1.0, 2.0]
    with m._PORTFOLIO_META_CACHE_LOCK:
        m._PORTFOLIO_META_CACHE["poison"] = {"id": "poison"}
        m._PORTFOLIO_META_CACHE_WARMED = True
    with m._TASK_CACHE_HYDRATION_LOCK:
        m._TASK_CACHE_HYDRATED = True
    with m._DATA_VIEWS_CACHE_LOCK:
        m._DATA_VIEWS_CACHE["key"] = "poison"
        m._DATA_VIEWS_CACHE["overview_categories"] = ["poison"]
        m._DATA_VIEWS_PREWARM_DONE = True
    with m.DATA_TASKS_LOCK:
        m.DATA_TASKS["poison"] = {"status": "poison"}
    with m._COALESCED_CLEANUP_STATE_LOCK:
        m._COALESCED_CLEANUP_PENDING.add("poison")
        m._COALESCED_CLEANUP_ORDER.append("poison")
        m._COALESCED_CLEANUP_ATTEMPTS["poison"] = 3
        m._COALESCED_CLEANUP_EXHAUSTED.add("poison")
    with legacy._OHLCV_DF_CACHE_LOCK:
        legacy._OHLCV_DF_CACHE["poison"] = (None, 0, None)


def _assert_viewer_state_clean() -> None:
    """Assert every cache ``_reset_viewer_runtime_state`` covers is empty.

    ``expires_at`` values are on the ``time.monotonic()`` clock — the same
    clock the viewer caches use — so a reset must leave them at 0.0 (or at
    least not in the future).
    """
    import viewer.main as m
    from viewer import core as legacy

    now = time.monotonic()
    with m._STRATEGIES_CACHE_LOCK:
        assert m._STRATEGIES_CACHE["payload"] == []
        assert m._STRATEGIES_CACHE["expires_at"] <= now
    with m._LIVE_ACCOUNTS_CACHE_LOCK:
        assert m._LIVE_ACCOUNTS_CACHE["payload"] == []
        assert m._LIVE_ACCOUNTS_CACHE["provider_marker"] is None
        assert m._LIVE_ACCOUNTS_CACHE["expires_at"] <= now
    with m._BACKTEST_META_CACHE_LOCK:
        assert m._BACKTEST_META_CACHE == {}
        assert m._BACKTEST_META_CACHE_SOURCE_MTIME_NS == 0
    with m._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        assert m._BACKTEST_EQUITY_PREVIEW_CACHE == {}
    with m._PORTFOLIO_META_CACHE_LOCK:
        assert m._PORTFOLIO_META_CACHE == {}
        assert m._PORTFOLIO_META_CACHE_WARMED is False
    assert m._TASK_CACHE_HYDRATED is False
    with m._DATA_VIEWS_CACHE_LOCK:
        assert m._DATA_VIEWS_CACHE["key"] is None
        assert m._DATA_VIEWS_PREWARM_DONE is False
    with m.DATA_TASKS_LOCK:
        assert m.DATA_TASKS == {}
    with m._COALESCED_CLEANUP_STATE_LOCK:
        assert m._COALESCED_CLEANUP_PENDING == set()
        assert m._COALESCED_CLEANUP_ORDER == []
        assert m._COALESCED_CLEANUP_ATTEMPTS == {}
        assert m._COALESCED_CLEANUP_EXHAUSTED == set()
    with legacy._OHLCV_DF_CACHE_LOCK:
        assert legacy._OHLCV_DF_CACHE == {}


def test_reset_viewer_runtime_state_clears_every_cache():
    """Self-contained #236 guard: poison every covered cache, reset, assert.

    Unlike an ordering-dependent leak check, this fails whenever ANY covered
    cache survives ``_reset_viewer_runtime_state`` — in any test order and
    under xdist.
    """
    from tests.conftest import _reset_viewer_runtime_state

    _poison_viewer_state()
    # Sanity: the poison landed (otherwise the clean assertions prove nothing).
    import viewer.main as m

    assert m._STRATEGIES_CACHE["payload"] == [{"strategy": "poison"}]
    _reset_viewer_runtime_state()
    _assert_viewer_state_clean()


def test_reset_viewer_runtime_state_clears_core_only_session_state(monkeypatch):
    """``viewer.core`` state is reset even when ``viewer.main`` is absent
    from ``sys.modules`` — the legacy OHLCV cache and DATA_TASKS live in
    ``viewer.core``, so a session that imported only ``viewer.core`` must
    not leak them either."""
    import viewer.core as legacy
    from tests.conftest import _reset_viewer_runtime_state

    # Simulate a worker that never imported viewer.main: the reset must take
    # the viewer.core fallback path for DATA_TASKS and _OHLCV_DF_CACHE.
    monkeypatch.delitem(sys.modules, "viewer.main", raising=False)
    with legacy.DATA_TASKS_LOCK:
        legacy.DATA_TASKS["poison"] = {"status": "poison"}
    with legacy._OHLCV_DF_CACHE_LOCK:
        legacy._OHLCV_DF_CACHE["poison"] = (None, 0, None)

    _reset_viewer_runtime_state()

    with legacy.DATA_TASKS_LOCK:
        assert legacy.DATA_TASKS == {}
    with legacy._OHLCV_DF_CACHE_LOCK:
        assert legacy._OHLCV_DF_CACHE == {}
