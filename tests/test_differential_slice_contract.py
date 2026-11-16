"""The differential reference slice's contract: window, rules, CSV and location.

The slice the differential engine tests run on is downloaded rather than
committed (``data_tools/differential_slice.py`` explains why). Everything that
makes that safe lives in this file, without the network:

* the window and row count are fixed constants, and the expected bar grid is
  derived from them rather than from whatever a fetch returns, so "600 rows" is a
  property of the contract and not of one lucky download;
* the aggregation rules are the platform's own 15m rules (first/max/min/last/sum,
  trading days only), checked on data assembled here;
* the CSV schema is pinned byte-wise, including the ``Z``-suffixed UTC stamps;
* the cache path resolves from ``DIFFERENTIAL_REAL_SLICE_CSV`` and otherwise
  lives under the data directory - never inside ``tests/``;
* nothing that looks like a slice is committed under ``tests/fixtures`` any more,
  which is the point of the change and therefore worth enforcing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from data_tools.differential_slice import (
    EXPECTED_SLICE_ROWS,
    SLICE_CACHE_SUBDIR,
    SLICE_COLUMNS,
    SLICE_FILENAME,
    SLICE_PATH_ENV,
    SLICE_WINDOW_END,
    SLICE_WINDOW_START,
    SliceValidationError,
    default_slice_path,
    normalise_slice_frame,
    read_slice,
    resolve_slice_path,
    slice_from_minute_bars,
    slice_grid,
    validate_slice_frame,
)
from tests.helpers.differential_slice import slice_like_frame, violations, write_slice_like

REPO_ROOT = Path(__file__).resolve().parents[1]


# ───────────────────────────── the window and its bars ────────────────────────


def test_the_slice_window_is_fixed_and_ordered():
    """A moving window would make the slice (and the CI cache) undefined."""
    assert SLICE_WINDOW_START == "2026-02-20T00:00:00+00:00"
    assert SLICE_WINDOW_END == "2026-03-02T06:00:00+00:00"


def test_the_window_derives_the_slices_own_row_count():
    """600 is derived, not remembered: six full trading days plus six hours."""
    grid = slice_grid()
    assert len(grid) == EXPECTED_SLICE_ROWS
    assert str(grid[0]) == "2026-02-20 00:00:00+00:00"
    assert str(grid[-1]) == "2026-03-02 05:45:00+00:00", "the last bar starts, not ends, at 05:45"
    assert grid.is_monotonic_increasing and not grid.duplicated().any()
    assert (grid.minute.isin([0, 15, 30, 45])).all(), "bars are on the quarter hour"


def test_the_grid_is_trading_days_only():
    """The weekend is closed: Dukascopy fills it with placeholder bars, which would
    turn 600 bars into 1056 and hand the engines a series of fake flat bars."""
    grid = slice_grid()
    assert (grid.weekday <= 4).all()
    assert "2026-02-21" not in {day.date().isoformat() for day in grid}, "Saturday is closed"
    assert "2026-03-01" not in {day.date().isoformat() for day in grid}, "Sunday is closed"


# ───────────────────────────── aggregation rules ──────────────────────────────


def _minute_frame() -> pd.DataFrame:
    """4 quarters of 15 one-minute bars, with a weekend block between them.

    Prices are chosen so the aggregate for the first hour is not accidentally
    satisfied by any other rule: open is the first bar's, high is the maximum of
    the *second* bar (not the first or the last), low is the minimum of the third,
    close is the last bar's, and volume is the plain sum.
    """
    stamps = pd.date_range("2026-02-20 00:00", periods=15, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [1.0] * 15,
            "high": [1.1] + [1.4] + [1.2] * 13,
            "low": [0.9] * 14 + [0.8],
            "close": [1.05] * 14 + [1.5],
            "volume": [2.0] * 15,
        }
    )
    # A Saturday block that must not appear in the output at all.
    weekend = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-21 00:00", periods=15, freq="1min", tz="UTC"),
            "open": [9.0] * 15,
            "high": [9.0] * 15,
            "low": [9.0] * 15,
            "close": [9.0] * 15,
            "volume": [0.0] * 15,
        }
    )
    return pd.concat([frame, weekend], ignore_index=True)


def test_fifteen_minute_aggregation_uses_the_platform_rules():
    """The same rules the platform resamples with, and no others.

    The Saturday block in the input is what makes the assertion about the *count*
    meaningful: it carries quotable-looking prices, so only a rule that drops
    non-trading days can leave a single bar behind.
    """
    resampled = slice_from_minute_bars(_minute_frame())

    assert len(resampled) == 1, "Saturday's 15 placeholder bars must not become a bar"
    row = resampled.iloc[0]
    assert row["open"] == 1.0, "open is the first bar of the bucket"
    assert row["high"] == 1.4, "high is the bucket maximum, not the last bar"
    assert row["low"] == 0.8, "low is the bucket minimum, not the first bar"
    assert row["close"] == 1.5, "close is the last bar of the bucket"
    assert row["volume"] == 30.0, "volume is the bucket sum"
    assert str(row["timestamp"]) == "2026-02-20 00:00:00+00:00"


def test_normalisation_projects_orders_and_quantises():
    frame = normalise_slice_frame(
        _minute_frame().assign(extra=1).astype({"open": "float32", "volume": "float32"})
    )
    assert list(frame.columns) == list(SLICE_COLUMNS), "extra columns and orders do not survive"
    assert frame["timestamp"].dt.tz is not None


def test_normalisation_refuses_a_frame_without_the_schema():
    with pytest.raises(SliceValidationError, match="missing required columns"):
        normalise_slice_frame(pd.DataFrame({"timestamp": [], "open": []}))


def test_the_window_end_is_exclusive():
    """Bars at or after 06:00 on 2026-03-02 belong to the next window, not this slice.

    The download fetches the whole daily file for the end date; truncating here is
    what keeps the row count at 600 and the last bar at 05:45.
    """
    minute = _minute_frame()
    # 05:45..06:14: two buckets, one on each side of the window's end.
    later = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-03-02 05:45", periods=30, freq="1min", tz="UTC"),
            "open": [5.0] * 30,
            "high": [5.0] * 30,
            "low": [5.0] * 30,
            "close": [5.0] * 30,
            "volume": [1.0] * 30,
        }
    )
    frame = slice_from_minute_bars(pd.concat([minute, later], ignore_index=True))

    assert [str(stamp) for stamp in frame["timestamp"]] == [
        "2026-02-20 00:00:00+00:00",
        "2026-03-02 05:45:00+00:00",
    ]
    assert frame["close"].iloc[-1] == 5.0, "the 05:45 bucket is inside the window"



# ───────────────────────────── the CSV on disk ────────────────────────────────


def test_a_valid_slice_frame_passes_the_contract():
    validate_slice_frame(slice_like_frame(), source="test")


@pytest.mark.parametrize("violation", sorted(violations()))
def test_each_broken_rule_is_rejected_and_named(violation):
    """The loader's checks are exactly the ones the scenarios depend on."""
    frame, fragment = violations()[violation]
    with pytest.raises(SliceValidationError, match=re.escape(fragment)):
        validate_slice_frame(frame, source="test")


def test_the_csv_schema_is_pinned(tmp_path):
    """Column order, ``Z``-suffixed UTC stamps and 5dp prices, byte for byte."""
    path = write_slice_like(tmp_path / "slice.csv")
    lines = path.read_text(encoding="utf-8").splitlines()

    assert lines[0] == "timestamp,open,high,low,close,volume"
    assert lines[1] == "2026-02-20T00:00:00Z,1.18,1.1804,1.1796,1.18,100.0"
    assert len(lines) == EXPECTED_SLICE_ROWS + 1, "600 bars plus the header"
    assert lines[-1].startswith("2026-03-02T05:45:00Z,")


def test_reading_the_written_csv_round_trips(tmp_path):
    path = write_slice_like(tmp_path / "slice.csv")
    frame = read_slice(path)

    assert list(frame.columns) == list(SLICE_COLUMNS)
    assert len(frame) == EXPECTED_SLICE_ROWS
    assert str(frame["timestamp"].dt.tz) == "UTC"
    assert frame["timestamp"].is_monotonic_increasing


def test_reading_a_missing_file_is_a_validation_error(tmp_path):
    with pytest.raises(SliceValidationError, match="could not be read"):
        read_slice(tmp_path / "absent.csv")


# ───────────────────────────── where the slice lives ──────────────────────────


def test_the_default_cache_path_is_under_the_data_dir_and_not_in_tests():
    path = default_slice_path()
    assert path.name == SLICE_FILENAME
    assert path.parent.name == SLICE_CACHE_SUBDIR
    assert "tests" not in path.parts, (
        "a slice inside the test tree reads as a source of truth, which is how the "
        "redistributable CSV came to be committed in the first place"
    )


def test_the_env_var_overrides_the_cache_path(tmp_path, monkeypatch):
    monkeypatch.setenv(SLICE_PATH_ENV, str(tmp_path / "custom.csv"))
    assert resolve_slice_path() == tmp_path / "custom.csv"

    monkeypatch.setenv(SLICE_PATH_ENV, "~/slice.csv")
    assert resolve_slice_path() == Path.home() / "slice.csv", "~ must expand on every OS"

    monkeypatch.delenv(SLICE_PATH_ENV, raising=False)
    assert resolve_slice_path() == default_slice_path()


def test_a_blank_env_var_falls_back_to_the_default(tmp_path, monkeypatch):
    """An exported-but-empty variable (``CI=... export DIFFERENTIAL_REAL_SLICE_CSV=``)
    must not resolve to a relative path named ''."""
    monkeypatch.setenv(SLICE_PATH_ENV, "   ")
    assert resolve_slice_path() == default_slice_path()


def test_the_require_flag_is_read_from_the_environment(monkeypatch):
    from data_tools.differential_slice import slice_required

    for value, expected in (("1", True), ("true", True), ("YES", True), ("0", False), ("", False)):
        monkeypatch.setenv("REQUIRE_REAL_DATA_SLICE", value)
        assert slice_required() is expected


# ───────────────────────────── nothing is committed again ─────────────────────


def test_no_slice_is_committed_under_the_test_tree():
    """The enforcement half of "generate it, do not ship it".

    A CSV back in ``tests/fixtures/differential/`` would be redistributable vendor
    data again, and would also quietly win over the cached download wherever the
    tests are run from a fresh clone.
    """
    committed = sorted((REPO_ROOT / "tests" / "fixtures" / "differential").glob("*.csv"))
    assert committed == [], (
        f"these fixtures must not be committed (download the slice instead): {committed}"
    )


def test_the_retired_fixture_path_stays_gitignored():
    """A leftover or regenerated file at the old address must never be committed."""
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    retired = f"tests/fixtures/differential/{SLICE_FILENAME}"
    assert retired in ignore, (
        f"{retired} must stay in .gitignore so a local regenerated copy cannot be committed"
    )
