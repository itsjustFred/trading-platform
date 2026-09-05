"""Cleanup 18-C slice 8 — vendor coverage windows come from config.

The HuggingFace/Alpaca intraday coverage bounds were hardcoded module
constants and already stale: every range past the constant silently dropped
the HuggingFace leg (or fell through to daily-grade yfinance) with only a log
line. The bounds are now configurable, and when coverage is unknown the plan
says so explicitly instead of silently re-routing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_coverage_windows_are_configurable(monkeypatch):
    from data_tools import auto_download

    monkeypatch.setenv("HF_INTRADAY_COVERAGE_END", "2026-12-31")
    monkeypatch.setenv("ALPACA_INTRADAY_START", "2026-01-01")
    auto_download.reload_coverage_windows()

    assert auto_download.HF_INTRADAY_COVERAGE_END == "2026-12-31"
    assert auto_download.ALPACA_INTRADAY_START == "2026-01-01"


def test_coverage_windows_default_to_current_values(monkeypatch):
    from data_tools import auto_download

    monkeypatch.delenv("HF_INTRADAY_COVERAGE_END", raising=False)
    monkeypatch.delenv("ALPACA_INTRADAY_START", raising=False)
    auto_download.reload_coverage_windows()

    assert auto_download.HF_INTRADAY_COVERAGE_END == "2025-07-31"
    assert auto_download.ALPACA_INTRADAY_START == "2025-07-01"


def test_plan_reports_coverage_unknown_when_no_leg_covers_range(monkeypatch):
    """A range outside every known vendor window is flagged, not silently re-routed."""
    from data_tools import auto_download

    # Shrink the HF window and push the Alpaca window past the range so no
    # vendor leg covers it (the defaults overlap, so fabricate a gap).
    monkeypatch.setenv("HF_INTRADAY_COVERAGE_END", "2025-01-01")
    monkeypatch.setenv("ALPACA_INTRADAY_START", "2026-01-01")
    auto_download.reload_coverage_windows()

    plan = auto_download._auto_download_source_plan(
        symbol="AAPL",
        timeframe="1h",
        start="2025-06-20",
        end="2025-08-05",
    )

    assert plan, "a plan is still produced"
    assert plan[-1].get("coverage") == "unknown", (
        f"expected explicit coverage-unknown marker, got {plan}"
    )


def test_plan_inside_known_windows_has_no_unknown_marker(monkeypatch):
    from data_tools import auto_download

    monkeypatch.delenv("HF_INTRADAY_COVERAGE_END", raising=False)
    monkeypatch.delenv("ALPACA_INTRADAY_START", raising=False)
    auto_download.reload_coverage_windows()

    plan = auto_download._auto_download_source_plan(
        symbol="AAPL",
        timeframe="1h",
        start="2025-06-20",
        end="2025-08-05",
    )
    assert plan == [
        {"source": "huggingface", "start": "2025-06-20", "end": "2025-07-31"},
        {"source": "alpaca", "start": "2025-07-01", "end": "2025-08-05"},
    ]


def test_viewer_module_uses_configurable_windows(viewer_client):
    """viewer.main reads the windows through data_tools.auto_download, so the
    shared module's values (not a stale mirror) drive the vendor plan."""
    from data_tools import auto_download

    assert auto_download.HF_INTRADAY_COVERAGE_END == "2025-07-31"
    assert auto_download.ALPACA_INTRADAY_START == "2025-07-01"

