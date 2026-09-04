"""Data gate for the strategy regression block under tests/strategies/.

Local strategy smoke/regression tests need real OHLCV parquet datasets that
are not part of the repository. The gate below skips a module when its
dataset is missing locally — but the skip is never silent:

- every skip reason is surfaced (and counted in a session-end summary);
- ``FAIL_ON_MISSING_DATA=1`` (opt-in, e.g. CI) turns those skips into hard
  failures, so a dataset loss cannot quietly zero out the regression block
  (Decision Record 2026-08-31);
- coverage is verified per candidate file (no union of shard extents) and
  unreadable/corrupt parquet files are reported instead of swallowed.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow.compute as pc
import pytest

from shared.constants import DATA_DIR

# Opt-in strict mode: missing local market data fails instead of skipping.
FAIL_ON_MISSING_DATA = str(os.environ.get("FAIL_ON_MISSING_DATA", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_DATA_SKIP_REASONS: list[str] = []
# Outcomes of the gate under FAIL_ON_MISSING_DATA=1. Kept separate from the
# skips so the session summary cannot describe a hard failure as a skip.
_DATA_GATE_FAILURES: list[str] = []


def record_data_skip(reason: str) -> None:
    """Register a data-gate skip so the session summary can surface it."""
    _DATA_SKIP_REASONS.append(str(reason))


def record_data_gate_failure(reason: str) -> None:
    """Register a data-gate failure raised under FAIL_ON_MISSING_DATA=1."""
    _DATA_GATE_FAILURES.append(str(reason))


def _emit_summary_lines(lines: list[str], config: pytest.Config) -> None:
    terminalreporter = config.pluginmanager.get_plugin("terminalreporter")
    write_line = getattr(terminalreporter, "write_line", None)
    if callable(write_line):
        for line in lines:
            write_line(line)
        return
    for line in lines:
        print(line)


def summarize_data_skips(config: pytest.Config) -> None:
    """Print one aggregated summary line for all data-gate skips/failures."""
    if not _DATA_SKIP_REASONS and not _DATA_GATE_FAILURES:
        return
    lines: list[str] = []
    if _DATA_SKIP_REASONS:
        lines.append(
            f"{len(_DATA_SKIP_REASONS)} strategy test(s) skipped on missing/unreadable "
            "local market data (set FAIL_ON_MISSING_DATA=1 to fail instead):"
        )
        lines.extend(f"  - {reason}" for reason in _DATA_SKIP_REASONS)
    if _DATA_GATE_FAILURES:
        lines.append(
            f"{len(_DATA_GATE_FAILURES)} strategy test(s) FAILED by the data gate "
            "(FAIL_ON_MISSING_DATA=1):"
        )
        lines.extend(f"  - {reason}" for reason in _DATA_GATE_FAILURES)
    _emit_summary_lines(lines, config)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    summarize_data_skips(session.config)


def _data_gate_result(reason: str) -> None:
    """Skip or fail with the given reason, depending on strict mode.

    The outcome is recorded *after* the branch: a strict-mode failure must not
    land in the skip summary (it is a failure, and the summary has to agree
    with the exit status).
    """
    if FAIL_ON_MISSING_DATA:
        record_data_gate_failure(reason)
        pytest.fail(f"{reason} (FAIL_ON_MISSING_DATA=1)")
    record_data_skip(reason)
    pytest.skip(reason)


def _candidate_ohlcv_paths(symbol: str, timeframe: str) -> list[Path]:
    root = DATA_DIR / "ohlcv"
    if not root.exists():
        return []

    symbol = str(symbol or "").strip().upper()
    timeframe = str(timeframe or "").strip().lower()
    if not symbol or not timeframe:
        return []

    paths = list(root.rglob(f"{symbol}/**/{timeframe}.parquet"))

    # Canonical layout: .../<symbol>/<timeframe>.parquet
    if timeframe == "1d":
        # Some datasets are exported as .../<symbol>.parquet (typically 1d).
        paths.extend(root.rglob(f"{symbol}.parquet"))

    return sorted({path.resolve() for path in paths})


def _has_ohlcv_data(symbol: str, timeframe: str) -> bool:
    return bool(_candidate_ohlcv_paths(symbol, timeframe))


def _to_utc_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _parse_date_bound(value: str, *, is_end: bool) -> datetime:
    parsed = _to_utc_datetime(str(value or "").strip())
    if parsed is None:
        raise ValueError(f"Invalid date filter value: {value!r}")
    raw = str(value or "").strip()
    if is_end and "T" not in raw and ":" not in raw:
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _path_time_coverage(path: Path) -> tuple[datetime | None, datetime | None, str | None]:
    """Return (start, end, error) for one parquet file.

    ``error`` is set when the file cannot be read — the caller must surface
    that instead of silently treating the shard as "no coverage".
    """
    try:
        table = pq.read_table(path, columns=["timestamp"])
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    if table.num_rows == 0:
        return None, None, "file contains no rows"

    column = table["timestamp"]
    try:
        min_scalar = pc.min(column)
        max_scalar = pc.max(column)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    start = _to_utc_datetime(min_scalar.as_py() if min_scalar is not None else None)
    end = _to_utc_datetime(max_scalar.as_py() if max_scalar is not None else None)
    if start is None or end is None:
        return None, None, "timestamp column has no parseable values"
    return start, end, None


def _covers_requested_period(symbol: str, timeframe: str, start: str, end: str) -> bool:
    """True when the shards provide gapless coverage of the requested window.

    Neither a bare union of extents (which hid holes between shards) nor a
    "every file must cover everything" rule (which rejected legitimate
    year-sharded datasets): the shard extents are sorted and must chain
    together without a hole across the requested window.

    This is the boolean twin of :func:`_require_ohlcv_coverage` — the same
    coverage policy, expressed as a predicate for tests. If you change the
    tolerance, the shard-chaining rule or the bound handling here, change it
    there too (they must never disagree).
    """
    start_bound = _parse_date_bound(start, is_end=False) if start else None
    end_bound = _parse_date_bound(end, is_end=True) if end else None

    extents: list[tuple[datetime, datetime]] = []
    for path in _candidate_ohlcv_paths(symbol, timeframe):
        path_start, path_end, error = _path_time_coverage(path)
        if error is not None or path_start is None or path_end is None:
            return False
        extents.append((path_start, path_end))

    if not extents:
        return False

    extents.sort()
    if start_bound is not None and extents[0][0] > start_bound:
        return False
    if end_bound is not None and extents[-1][1] < end_bound:
        return False

    # No hole between consecutive shards (a 1µs overlap tolerance absorbs
    # inclusive end bounds). The running maximum end guards against a shard
    # that is fully contained in an earlier one, where a naive
    # "previous end" comparison would accept a gap after the contained shard.
    reach = extents[0][1]
    for next_start, next_end in extents[1:]:
        if next_start > reach + timedelta(microseconds=1):
            return False
        reach = max(reach, next_end)
    return True


def _require_ohlcv_coverage(symbol: str, timeframe: str, start: str, end: str) -> None:
    """Skip or fail when the local dataset cannot serve the requested window.

    Enforcement twin of :func:`_covers_requested_period` (same policy: sorted
    shard extents must chain without a hole across the window). Keep the two
    in sync — the gate's own tests assert through the boolean twin.
    """
    paths = _candidate_ohlcv_paths(symbol, timeframe)
    if not paths:
        _data_gate_result(
            f"missing OHLCV dataset for {symbol}/{timeframe} under {Path(DATA_DIR) / 'ohlcv'}"
        )
        return

    start_bound = _parse_date_bound(start, is_end=False) if start else None
    end_bound = _parse_date_bound(end, is_end=True) if end else None

    unreadable: list[str] = []
    extents: list[tuple[datetime, datetime, str]] = []
    for path in paths:
        relative = _relative_candidate_label(path)
        path_start, path_end, error = _path_time_coverage(path)
        if error is not None or path_start is None or path_end is None:
            unreadable.append(f"{relative} ({error or 'no coverage'})")
            continue
        extents.append((path_start, path_end, relative))

    if unreadable:
        _data_gate_result(
            f"unreadable OHLCV dataset(s) for {symbol}/{timeframe}: "
            + "; ".join(unreadable)
        )
        return

    extents.sort()
    uncovered: list[str] = []
    if start_bound is not None and extents and extents[0][0] > start_bound:
        uncovered.append(f"dataset starts {extents[0][0].isoformat()}")
    if end_bound is not None and extents and extents[-1][1] < end_bound:
        uncovered.append(f"dataset ends {extents[-1][1].isoformat()}")
    # Running-maximum reach (same rule as _covers_requested_period): guards
    # against a shard fully contained in an earlier one, where a naive
    # "previous end" comparison would accept a gap after the contained shard.
    reach = extents[0][1]
    reach_label = extents[0][2]
    for next_start, next_end, next_label in extents[1:]:
        if next_start > reach + timedelta(microseconds=1):
            uncovered.append(
                f"gap between {reach_label} (ends {reach.isoformat()}) and "
                f"{next_label} (starts {next_start.isoformat()})"
            )
        reach = max(reach, next_end)
        reach_label = next_label

    if uncovered:
        _data_gate_result(
            f"insufficient OHLCV coverage for {symbol}/{timeframe} "
            f"within requested range {start}..{end}: " + "; ".join(uncovered)
        )


def _relative_candidate_label(path: Path) -> str:
    try:
        return str(path.relative_to(Path(DATA_DIR)))
    except ValueError:
        return str(path)


@pytest.fixture(autouse=True, scope="module")
def skip_when_required_data_or_dependency_missing(request: pytest.FixtureRequest):
    module = request.module
    module_name = str(getattr(module, "__name__", "") or "").lower()

    if "pynecore" in module_name and importlib.util.find_spec("pynecore") is None:
        record_data_skip("pynecore is not installed in this environment")
        pytest.skip("pynecore is not installed in this environment")

    symbol = getattr(module, "TEST_SYMBOL", None)
    timeframe = getattr(module, "TEST_TIMEFRAME", None)
    if symbol and timeframe:
        start = str(getattr(module, "TEST_START", "") or "")
        end = str(getattr(module, "TEST_END", "") or "")
        _require_ohlcv_coverage(str(symbol), str(timeframe), start, end)
