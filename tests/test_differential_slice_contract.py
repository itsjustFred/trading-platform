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

import asyncio
import re
import subprocess
import sys
from datetime import datetime, timezone
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
    SliceDownloadError,
    SliceValidationError,
    default_slice_path,
    fetch_slice_frame,
    normalise_slice_frame,
    read_slice,
    resolve_slice_path,
    slice_from_minute_bars,
    slice_grid,
    validate_slice_frame,
    write_slice,
)
from data_tools.downloaders import dukascopy_native
from scripts import fetch_differential_slice as fetch_script
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
    assert grid.is_monotonic_increasing
    assert not grid.duplicated().any()
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


def test_a_high_exactly_at_the_body_top_is_valid():
    """The containment rule is inclusive: a high equal to max(open, close) is a valid bar.
    Guards the rule against being tightened into a rejection of every bar with a body."""
    frame = slice_like_frame()
    close = float(frame.loc[6, "close"])
    frame.loc[6, ["open", "high", "low"]] = [
        round(close - 0.0008, 5),
        round(close, 5),
        round(close - 0.0012, 5),
    ]
    validate_slice_frame(frame, source="test")


def test_write_slice_refuses_an_invalid_frame_and_writes_nothing(tmp_path):
    """A truncated frame must not reach the cache: the write path has its own guard, so a
    bad download cannot be logged as written and only caught on the next read."""
    frame, fragment = violations()["truncated"]
    path = tmp_path / "cache" / "slice.csv"

    with pytest.raises(SliceValidationError, match=re.escape(fragment)):
        write_slice(path, frame)

    assert not path.exists()


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


# ───────────────────────────── the download entry point ────────────────────────


def _minute_bars_for(grid: pd.DatetimeIndex) -> pd.DataFrame:
    """The 1m bars that resample into ``grid``'s 15m buckets."""
    stamps = [
        stamp + pd.Timedelta(minutes=minute)
        for stamp in grid
        for minute in range(15)
    ]
    return pd.DataFrame(
        {
            "timestamp": pd.DatetimeIndex(stamps),
            "open": [1.18] * len(stamps),
            "high": [1.1805] * len(stamps),
            "low": [1.1795] * len(stamps),
            "close": [1.18] * len(stamps),
            "volume": [1.0] * len(stamps),
        }
    )


def _minute_grid_frame() -> pd.DataFrame:
    """A complete minute-bar window: enough bars to yield the whole slice."""
    return _minute_bars_for(slice_grid())


def _partial_minute_frame() -> pd.DataFrame:
    """Only the first 15m bucket's minute bars: a truncated download."""
    return _minute_bars_for(slice_grid()[:1])


def test_the_minute_candle_entry_point_refuses_a_both_sided_fetch():
    """The two sides live in different daily URLs, so there is nothing to split afterwards.

    Refused here rather than requested as a URL that cannot exist and come back as an
    unexplained empty frame, which the caller would then have to diagnose.
    """
    start = datetime(2026, 2, 20, tzinfo=timezone.utc)
    with pytest.raises(NotImplementedError, match="one offer side"):
        asyncio.run(
            dukascopy_native.download_minute_candles("eurusd", start, start, side="both")
        )


# ───────────────────────── the bounded retry around a throttled download ───────


def _throttled_downloader(failures: int, calls: list[int]):
    """A stand-in for the download call that raises ``failures`` times, then succeeds."""

    async def _download(*_args, **_kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise SliceValidationError("no bar falls inside the window (HTTP 429 storm)")
        return _minute_grid_frame()

    return _download


def test_the_outer_retry_recovers_from_a_throttled_window(monkeypatch):
    """Dukascopy's rate limit is per-address and outlasts the per-file retries, so the
    whole window is retried - and a recovery must be used, not reported as a failure."""
    calls: list[int] = []
    monkeypatch.setattr(
        dukascopy_native, "download_minute_candles", _throttled_downloader(2, calls)
    )

    frame = asyncio.run(fetch_slice_frame(attempts=4, retry_pause_seconds=0))

    assert len(calls) == 3, "two throttled attempts, then the recovery"
    assert len(frame) == EXPECTED_SLICE_ROWS


def test_a_partial_download_is_retried_before_it_can_fail(monkeypatch):
    """A truncated window is a failed attempt, not a slice: validate inside the loop."""
    calls: list[int] = []

    async def _download(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _partial_minute_frame()
        return _minute_grid_frame()

    monkeypatch.setattr(dukascopy_native, "download_minute_candles", _download)

    frame = asyncio.run(fetch_slice_frame(attempts=2, retry_pause_seconds=0))

    assert len(calls) == 2, "the partial frame must be retried, not returned"
    assert len(frame) == EXPECTED_SLICE_ROWS


def test_the_outer_retry_is_bounded_and_raises(monkeypatch):
    """Patient, but not unbounded: a slice that cannot be produced must fail the run
    rather than loop, because a run that never finishes is the same as a red build."""
    calls: list[int] = []
    monkeypatch.setattr(
        dukascopy_native, "download_minute_candles", _throttled_downloader(99, calls)
    )

    with pytest.raises(SliceDownloadError, match="could not generate the slice"):
        asyncio.run(fetch_slice_frame(attempts=3, retry_pause_seconds=0))

    assert len(calls) == 3, "exactly three attempts, no more"


def test_a_complete_but_invalid_slice_fails_instead_of_becoming_a_download_error(
    monkeypatch,
):
    """A full row count with broken quotes is corruption, not an outage.

    Retrying it into ``SliceDownloadError`` would turn a real validation failure
    into CI's warn-and-skip path, which is exactly the confusion this contract
    exists to prevent.
    """
    calls: list[int] = []

    async def _download(*_args, **_kwargs):
        calls.append(1)
        return _minute_grid_frame()

    broken, fragment = violations()["zero_close"]
    assert len(broken) == EXPECTED_SLICE_ROWS
    monkeypatch.setattr(dukascopy_native, "download_minute_candles", _download)
    monkeypatch.setattr(
        "data_tools.differential_slice.slice_from_minute_bars", lambda _minute: broken
    )

    with pytest.raises(SliceValidationError, match=re.escape(fragment)) as excinfo:
        asyncio.run(fetch_slice_frame(attempts=3, retry_pause_seconds=0))

    assert not isinstance(excinfo.value, SliceDownloadError)
    assert len(calls) == 1, "a contract violation is not a retryable download failure"


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


# ───────────────────────────── the fetch script itself ─────────────────────────


def test_the_fetch_script_reuses_a_valid_cached_slice(tmp_path):
    """CI's ``--skip-if-valid`` path, without the network.

    On a cache hit the script must exit 0 having downloaded nothing: the cache key is
    the hash of the script and of ``data_tools/``, so a hit means unchanged rules. A
    miss is a fresh download, which is the one case this test cannot exercise without
    reaching Dukascopy.
    """
    path = write_slice_like(tmp_path / "cached.csv")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.fetch_differential_slice",
            "--out",
            str(path),
            "--skip-if-valid",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reusing the valid cached slice" in result.stdout
    # And it must not have rewritten the file it was told to reuse.
    assert len(read_slice(path)) == EXPECTED_SLICE_ROWS


def test_a_valid_cache_is_reused_only_when_the_flag_is_given(tmp_path):
    path = write_slice_like(tmp_path / "cached.csv")

    reuse, message = fetch_script._plan(path, skip_if_valid=True)
    assert reuse is True
    assert "reusing the valid cached slice" in message

    assert fetch_script._plan(path, skip_if_valid=False) == (False, ""), (
        "without --skip-if-valid a cache is never reused, even a valid one"
    )


def test_an_invalid_cache_is_never_reported_as_reusable(tmp_path):
    """CI's cache-hit path must not go green on a broken slice."""
    path = write_slice_like(
        tmp_path / "cached.csv", slice_like_frame(rows=EXPECTED_SLICE_ROWS - 1)
    )

    reuse, message = fetch_script._plan(path, skip_if_valid=True)

    assert reuse is False
    assert "is not usable" in message and "downloading again" in message


def test_an_invalid_cache_is_replaced_by_a_fresh_download(tmp_path, monkeypatch, capsys):
    path = write_slice_like(
        tmp_path / "cached.csv", slice_like_frame(rows=EXPECTED_SLICE_ROWS - 1)
    )
    downloads: list[int] = []

    async def _fresh_download(**_kwargs):
        downloads.append(1)
        return slice_like_frame()

    monkeypatch.setattr(fetch_script, "fetch_slice_frame", _fresh_download)

    assert fetch_script.main(["--out", str(path), "--skip-if-valid", "--quiet"]) == 0

    assert len(downloads) == 1, "an unusable cache must trigger a download, not a reuse"
    assert "reusing" not in capsys.readouterr().out
    assert len(read_slice(path)) == EXPECTED_SLICE_ROWS


def test_a_failed_download_exits_three_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """Exit 3 is the CI contract for "Dukascopy failed, no usable cache"."""
    path = tmp_path / "out.csv"

    async def _failing_download(**_kwargs):
        raise SliceDownloadError("could not generate the slice: HTTP 429 storm")

    monkeypatch.setattr(fetch_script, "fetch_slice_frame", _failing_download)

    assert fetch_script.main(["--out", str(path), "--quiet"]) == 3
    assert not path.exists()
    err = capsys.readouterr().err
    assert "Dukascopy" in err and "skip" in err


def test_an_invalid_generated_slice_exits_one_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    """A frame that fails the write-side contract is not a download outage."""
    path = tmp_path / "out.csv"

    async def _invalid_download(**_kwargs):
        return slice_like_frame(rows=EXPECTED_SLICE_ROWS - 1)

    monkeypatch.setattr(fetch_script, "fetch_slice_frame", _invalid_download)

    assert fetch_script.main(["--out", str(path), "--quiet"]) == 1
    assert not path.exists()
    assert "599 rows, expected 600" in capsys.readouterr().err


def test_ci_requires_the_slice_only_when_the_fetch_succeeded():
    """A Dukascopy outage may skip the real-data scenarios on PRs; on main it may not.

    Pull requests warn and skip a failed download, but a push to main must fail
    the fetch step itself so the real-data slice is always required there and a
    permanent outage is noticed.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "id: fetch-slice" in workflow
    assert '"$status" -eq 3' in workflow
    assert "download_failed=true" in workflow
    assert "steps.fetch-slice.outputs.download_failed" in workflow
    assert "REQUIRE_REAL_DATA_SLICE" in workflow
    assert 'REQUIRE_REAL_DATA_SLICE: "1"' not in workflow
    # The skip is conditional on the event: only non-push runs may swallow exit 3.
    assert 'github.event_name' in workflow
    assert '!= "push"' in workflow
