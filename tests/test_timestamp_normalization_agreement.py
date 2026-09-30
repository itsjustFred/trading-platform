"""Downloaded data and consolidated data must lose the same rows, for the same reasons.

`Downloader._normalize_timestamps` in `data_tools/downloaders/base.py` and
`_normalize_frame` in `scripts/consolidate_parquets.py` are the same rule written
twice: copy, coerce the `timestamp` column to UTC datetimes, drop whatever will not
parse. Neither was named in a test file, on either side.

That rule decides which rows exist. `errors="coerce"` turns an unparseable
timestamp into NaT and the following `dropna` removes the row, so a dataset whose
timestamps are all unreadable is silently reduced to zero rows - no exception, no
warning, no count. A backtest then runs on whatever survived, and the gap between
what was fetched and what was used is invisible from the result.

Two copies of that rule is the risk this file closes. They agree today. If they
stopped, the download path and the consolidation path would disagree about which
rows are real, and a consolidated dataset would no longer be the data that was
downloaded - the reproducibility guarantee this platform rests on, failing quietly.

The invariant worth stating is the direction of the failure. Dropping an
unparseable row loses data loudly in the numbers, if nowhere else. *Keeping* it as
NaT puts a null into every downstream timestamp, resample and return calculation
while every count still looks healthy. So the assertion is that no NaT survives,
which fails if someone ever removes the `dropna` as a seemingly harmless
simplification.
"""

from __future__ import annotations

import pandas as pd
import pytest

from data_tools.downloaders.base import Downloader
from scripts.consolidate_parquets import _normalize_frame

# Both are reachable without an instance; the downloader copy is a staticmethod and
# the consolidation copy a plain function, and neither uses `self`.
NORMALIZERS = {
    "downloader": Downloader._normalize_timestamps,
    "consolidation": _normalize_frame,
}

FRAMES = {
    "no-timestamp-column": pd.DataFrame({"close": [1.0, 2.0], "open": [0.5, 0.75]}),
    "all-valid-iso": pd.DataFrame({"timestamp": ["2024-01-01", "2024-01-02"]}),
    "one-unparseable": pd.DataFrame({"timestamp": ["2024-01-01", "nope", "2024-01-03"]}),
    "all-unparseable": pd.DataFrame({"timestamp": ["nope", "also-nope"]}),
    "epoch-millis": pd.DataFrame({"timestamp": [1704164645000, 1704164700000]}),
    "mixed-offsets": pd.DataFrame(
        {"timestamp": ["2024-01-01T00:00:00+02:00", "2024-01-01T00:00:00Z"]}
    ),
    "explicit-null": pd.DataFrame({"timestamp": [None, "2024-01-02"]}),
    "empty": pd.DataFrame({"timestamp": []}),
    "all-null": pd.DataFrame({"timestamp": [None, None]}),
    "with-other-columns": pd.DataFrame(
        {"timestamp": ["2024-01-01", "bad"], "close": [1.5, 2.5], "volume": [10, 20]}
    ),
    "duplicate-timestamps": pd.DataFrame(
        {"timestamp": ["2024-01-01", "2024-01-01", "2024-01-02"]}
    ),
    # A null outside the timestamp must not cost the row: only the timestamp
    # decides whether a row is data. This is the frame that distinguishes a
    # `dropna(subset=["timestamp"])` from a bare `dropna()`.
    "null-in-other-column": pd.DataFrame(
        {"timestamp": ["2024-01-01", "2024-01-02"], "close": [1.5, None]}
    ),
    "unparseable-and-null-elsewhere": pd.DataFrame(
        {"timestamp": ["2024-01-01", "bad"], "close": [1.5, None]}
    ),
}


def _normalize(fn, frame):
    return fn(frame.copy())


@pytest.mark.parametrize("name", sorted(FRAMES), ids=sorted(FRAMES))
def test_both_paths_keep_exactly_the_same_rows(name) -> None:
    """The download path and the consolidation path must agree row for row.

    Compared as frames rather than as counts, so a disagreement about *which* rows
    survive is caught even when the totals happen to match.
    """
    frame = FRAMES[name]
    results = {label: _normalize(fn, frame) for label, fn in NORMALIZERS.items()}
    download, consolidation = results["downloader"], results["consolidation"]
    pd.testing.assert_frame_equal(
        download,
        consolidation,
        check_dtype=False,
        obj=f"{name}: the two normalisation paths disagree",
    )


@pytest.mark.parametrize("name", sorted(FRAMES), ids=sorted(FRAMES))
@pytest.mark.parametrize("path", sorted(NORMALIZERS))
def test_no_unparseable_timestamp_survives_normalization(name, path) -> None:
    """A row that could not be parsed is removed, not carried along as NaT.

    This is the direction that matters. Keeping the row would leave a null inside
    every timestamp, resample and return calculation downstream while every row
    count still looked healthy; dropping it at least makes the loss visible in the
    shape of the data.
    """
    result = _normalize(NORMALIZERS[path], FRAMES[name])
    if "timestamp" in result.columns and len(result):
        assert not result["timestamp"].isna().any(), (
            f"{path} left an unparsed timestamp in the data as NaT: {name}"
        )
        assert str(result["timestamp"].dtype).endswith("UTC]"), (
            f"{path} did not normalise to UTC: {name} is {result['timestamp'].dtype}"
        )


@pytest.mark.parametrize("name", sorted(FRAMES), ids=sorted(FRAMES))
@pytest.mark.parametrize("path", sorted(NORMALIZERS))
def test_the_caller_frame_is_not_modified_in_place(name, path) -> None:
    """Both copies promise a new frame; the downloader's own callers keep theirs.

    The `.copy()` is what makes that true, and without it a normalised result and a
    caller's original would be the same object - so a later mutation of the result
    would rewrite data the caller still believes it owns.

    The frame is passed to the function directly rather than through the copying
    helper the other tests use. Handing it a copy here would make this test pass
    whether or not the function copies, which is precisely the question.
    """
    frame = FRAMES[name].copy()
    before = frame.copy(deep=True)
    NORMALIZERS[path](frame)
    pd.testing.assert_frame_equal(
        frame,
        before,
        check_dtype=False,
        obj=f"{path} mutated the caller's frame in place: {name}",
    )


@pytest.mark.parametrize("name", sorted(FRAMES), ids=sorted(FRAMES))
@pytest.mark.parametrize("path", sorted(NORMALIZERS))
def test_rows_are_only_removed_never_altered(name, path) -> None:
    """Every surviving row must be one of the input's rows, value for value.

    Asserting the non-timestamp columns are unchanged would be wrong whenever a row
    is dropped, since they are then shorter. The property that actually matters is
    that normalisation only ever *removes* rows: each row that comes out must be
    byte-identical to a row that went in, on every column at once. That also rules
    out a `dropna` applied to the whole frame, which would discard rows because of
    an unrelated null in, say, a volume column.
    """
    frame = FRAMES[name]
    result = _normalize(NORMALIZERS[path], frame)
    assert list(result.columns) == list(frame.columns), (
        f"{path} changed the columns for {name}: {list(frame.columns)} -> {list(result.columns)}"
    )
    assert len(result) <= len(frame), f"{path} invented rows for {name}"

    others = [c for c in frame.columns if c != "timestamp"]
    if not others:
        return
    before = [tuple(str(v) for v in row) for row in frame[others].itertuples(index=False)]
    after = [tuple(str(v) for v in row) for row in result[others].itertuples(index=False)]
    extra = [row for row in after if row not in set(before)]
    assert not extra, (
        f"{path} altered a value while normalising {name}, or kept a row that was "
        f"not in the input: {extra}"
    )


def test_a_dataset_whose_timestamps_all_fail_is_reduced_to_nothing() -> None:
    """Documented consequence, so the silence is a stated decision and not an accident.

    Every row being unreadable yields an empty frame rather than an error, in both
    paths. This is the case that most deserves a loud failure somewhere upstream,
    and today the only evidence is the shape of the data, so it is pinned here: if
    a future change starts raising instead, or starts keeping the rows, this test
    says which of the two happened.
    """
    frame = pd.DataFrame({"timestamp": ["not-a-date", "still-not"], "close": [1.0, 2.0]})
    for path, fn in NORMALIZERS.items():
        result = _normalize(fn, frame)
        assert len(result) == 0, (
            f"{path} no longer drops rows whose timestamps cannot be parsed; "
            f"{len(result)} row(s) survived and will propagate as NaT"
        )
