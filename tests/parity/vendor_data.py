"""On-demand vendor market data for the parity suites.

The parity fixtures used to be committed gzip CSVs under
``tests/parity/fixtures/``. Vendor market data is licensed to be queried, not
redistributed through a cloneable git history, so the files now live in a
local cache that the ``scripts/fetch_parity_*`` entry points write and these
helpers read — the same policy ``data_tools/differential_slice.py`` applies to
the differential real-data slice: never inside ``tests/``, overridable by an
environment variable, and a missing file skips on pull requests while a run
that requires the data fails instead.

Layout: ``<vendor dir>/<suite>/<fixture file>``, e.g.
``parity_vendor/web/spy_1d.csv.gz`` or ``parity_vendor/published/agg_1d.csv.gz``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

from shared.constants import DATA_DIR

#: Directory that holds the cached vendor files when nothing overrides it.
VENDOR_CACHE_SUBDIR = "parity_vendor"

#: Environment variable naming the cache root (a directory, not a file).
VENDOR_DATA_DIR_ENV = "PARITY_VENDOR_DATA_DIR"

#: Environment variable that turns a missing fixture into a failure.
REQUIRE_VENDOR_DATA_ENV = "REQUIRE_PARITY_VENDOR_DATA"


class VendorDataError(RuntimeError):
    """A present vendor file is unreadable or violates the fixture contract."""


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def vendor_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The cache root: ``VENDOR_DATA_DIR_ENV`` when set, else ``DATA_DIR``.

    Under pytest ``DATA_DIR`` is a per-process temp dir, so the default almost
    always misses and the tests skip unless the operator (or CI) points
    ``VENDOR_DATA_DIR_ENV`` at a real cache — the same contract the
    differential slice uses with ``DIFFERENTIAL_REAL_SLICE_CSV``.
    """
    source = os.environ if env is None else env
    override = (source.get(VENDOR_DATA_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return DATA_DIR / VENDOR_CACHE_SUBDIR


def vendor_data_required(env: Mapping[str, str] | None = None) -> bool:
    """Whether a missing fixture must fail the run instead of skipping it."""
    source = os.environ if env is None else env
    return _is_true(source.get(REQUIRE_VENDOR_DATA_ENV))


def vendor_fixture_path(
    suite: str, filename: str, env: Mapping[str, str] | None = None
) -> Path:
    """The cache path for ``filename`` in ``suite`` (e.g. ``web``, ``published``).

    ``filename`` is the fixture stem without compression (``spy_1d``); the
    ``.csv.gz`` suffix is implied, matching the committed fixtures' format.
    """
    return vendor_data_dir(env) / suite / f"{filename}.csv.gz"


@dataclass(frozen=True, slots=True)
class _FixtureDecision:
    path: Path
    #: "load" (present), "skip" (absent and optional) or "fail" (absent, required)
    action: str
    reason: str


def _fixture_decision(
    suite: str, filename: str, fetch_hint: str, env: Mapping[str, str] | None
) -> _FixtureDecision:
    path = vendor_fixture_path(suite, filename, env)
    if path.is_file():
        return _FixtureDecision(path, "load", "")
    reason = (
        f"the parity vendor fixture {filename} is missing at {path}. Vendor "
        f"market data is cached, not committed — fetch it with `{fetch_hint}`, "
        f"or point {VENDOR_DATA_DIR_ENV} at a cache that already has it."
    )
    if vendor_data_required(env):
        reason += f" {REQUIRE_VENDOR_DATA_ENV} is set, so this run must not skip it silently."
        return _FixtureDecision(path, "fail", reason)
    return _FixtureDecision(path, "skip", reason)


def load_vendor_frame(
    suite: str,
    filename: str,
    *,
    fetch_hint: str,
    env: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Read a cached fixture as a DataFrame, or skip/fail with the reason why.

    ``filename`` is the fixture name without compression, e.g. ``spy_1d`` —
    the ``.csv.gz`` suffix is implied. A missing file skips (or fails when
    ``REQUIRE_PARITY_VENDOR_DATA`` is set); a present but malformed file always
    fails, because a quietly narrower parity suite is the failure mode this
    policy exists to prevent.
    """
    decision = _fixture_decision(suite, filename, fetch_hint, env)
    # pytest.skip/fail only exist inside a test run; the same loaders are used
    # by report scripts, where a missing fixture must be a plain error.
    in_pytest = os.environ.get("PYTEST_CURRENT_TEST") is not None
    if decision.action == "skip":
        if in_pytest:
            pytest.skip(decision.reason)
        raise VendorDataError(decision.reason)
    if decision.action == "fail":
        if in_pytest:
            pytest.fail(decision.reason)
        raise VendorDataError(decision.reason)
    frame = pd.read_csv(decision.path, parse_dates=["timestamp"])
    if frame.empty or "timestamp" not in frame.columns or "close" not in frame.columns:
        raise VendorDataError(
            f"{decision.path} is not a usable parity fixture: expected a "
            "non-empty CSV with 'timestamp' and 'close' columns"
        )
    stamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if stamps.isna().any():
        raise VendorDataError(f"{decision.path} has unparseable timestamps")
    return frame
