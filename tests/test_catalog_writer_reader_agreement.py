"""The catalog writer and the catalog reader must understand filenames the same way.

`data_tools/catalog_builder.py` writes `catalog.json`; `viewer/main.py` reads it.
Between them the same five helpers are implemented twice with identical
signatures: `_extract_timeframe_and_segment`, `_extract_symbol_timeframe_and_segment`,
`_format_catalog_date`, `_catalog_timestamp_to_epoch` and `_read_row_group_stat`.
They agree today. Nothing in the suite asserts that they do, and `_read_row_group_stat`
was not exercised on either side.

The failure mode is worse than a raised error. A catalog entry is a JSON object,
so if the two sides ever disagree about how a stem splits into symbol, timeframe
and segment, or about how a date is formatted, the JSON stays perfectly valid and
the viewer labels real data with the wrong values. Nothing errors; a chart is just
attributed to the wrong symbol and the wrong window. That is the same
guard-does-not-guard shape as the ctrader credential resolver, with a wider blast
radius, and the duplication is invisible to a test that only ever exercises one
side.

So this pins the two implementations to each other, and then pins the contract
that actually matters: what the writer writes, the reader recovers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_tools import catalog_builder as builder


@pytest.fixture
def writer_reader(monkeypatch, tmp_path, isolated_db_sandbox):
    """Both implementations, with the viewer's storage paths pointed at a sandbox.

    `viewer.main` is imported inside the fixture rather than at module scope to
    match the existing viewer tests, which keep the FastAPI app and its background
    machinery out of collection.
    """
    import viewer.main as viewer_app_module

    for module in (viewer_app_module, viewer_app_module.legacy_viewer):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
    viewer_app_module.DATA_TASKS.clear()
    return builder, viewer_app_module


STEMS = [
    "AAPL_1min_20240101_20240201",
    "SPY_5m_20230101_20230131",
    "BTCUSDT_1h_20240101_20240102",
    "ES_1min_20240101",
    "ETHUSD_15min_20240101_20240201_20240301",
    "NQ_1min_20240101_20240201_extra",
    "EURUSD_1min_2024-01-01_2024-02-01",
    "AAPL_1MIN_20240101_20240201",
    "aapl_1min_20240101_20240201",
    "AAPL_daily_20240101_20240201",
    "AAPL_1min",
    "AAPL",
    "",
    "___",
]

DATES = [
    datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    "2024-01-02T03:04:05Z",
    "2024-01-02T03:04:05+00:00",
    "2024-01-02 03:04:05",
    1704164645,
    1704164645.5,
    0,
    None,
    "",
    "not-a-date",
]


def _call(module, name, *args, **kwargs):
    """Compare outcomes, not just return values.

    A refusal is a legitimate answer from either side, and "the reader accepts a
    stem the writer rejected" is exactly the divergence worth catching, so an
    exception is part of the result rather than something the comparison hides.
    """
    try:
        return ("ok", getattr(module, name)(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - the type itself is the observation
        return ("raised", type(exc).__name__)


@pytest.mark.parametrize("stem", STEMS)
def test_both_sides_split_a_stem_the_same_way(writer_reader, stem):
    b, m = writer_reader
    for name in ("_extract_timeframe_and_segment", "_extract_symbol_timeframe_and_segment"):
        assert _call(b, name, stem) == _call(m, name, stem), (
            f"{name} disagrees about {stem!r}: the writer and the reader would "
            "label the same file differently"
        )


@pytest.mark.parametrize("value", DATES, ids=range(len(DATES)))
def test_both_sides_read_a_timestamp_the_same_way(writer_reader, value):
    b, m = writer_reader
    for name in ("_format_catalog_date", "_catalog_timestamp_to_epoch"):
        assert _call(b, name, value) == _call(m, name, value), (
            f"{name} disagrees about {value!r}: the writer's idea of this "
            "timestamp is not the reader's"
        )


@pytest.fixture
def parquet_metadata(tmp_path):
    """Real parquet metadata, with and without column statistics.

    A hand-rolled stand-in for `FileMetaData` would test the stand-in: the point
    here is that both copies walk the same statistics object, so the object has to
    be the one pyarrow actually produces.

    Row groups hold two distinct values each, not one. A single-row group has
    min == max, which makes every `boundary="min"` case indistinguishable from
    `boundary="max"` and silently empties the one assertion this fixture exists to
    support - the pin passes on a reader that ignores the boundary entirely.
    """
    table = pa.table(
        {
            "ts": pa.array(
                [1704164645000, 1704164700000, 1704164755000, 1704164810000], pa.int64()
            ),
            "close": pa.array([1.0, 2.5, 3.5, 9.25]),
        }
    )
    with_stats = tmp_path / "with_stats.parquet"
    pq.write_table(table, with_stats, row_group_size=2, write_statistics=True)
    without = tmp_path / "without_stats.parquet"
    pq.write_table(table, without, row_group_size=2, write_statistics=False)
    return (
        pq.ParquetFile(with_stats).metadata,
        pq.ParquetFile(without).metadata,
    )


@pytest.mark.parametrize("row_group", [0, 1, 2])
@pytest.mark.parametrize("boundary", ["min", "max"])
def test_both_sides_read_row_group_stats_the_same_way(
    writer_reader, parquet_metadata, row_group, boundary
):
    b, m = writer_reader
    with_stats, _ = parquet_metadata
    for column_index in (0, 1):
        assert _call(
            b, "_read_row_group_stat", with_stats, column_index, row_group, boundary=boundary
        ) == _call(
            m, "_read_row_group_stat", with_stats, column_index, row_group, boundary=boundary
        ), (
            f"row-group stat for column {column_index}, group {row_group}, "
            f"boundary {boundary} disagrees between writer and reader"
        )


def test_a_column_without_statistics_yields_none_on_both_sides(writer_reader, parquet_metadata):
    """No statistics is a real case, and it must be a quiet None on both sides.

    The alternative - a reader that treats absent statistics as a value - is how a
    segment silently acquires a wrong start date.
    """
    b, m = writer_reader
    _, without_stats = parquet_metadata
    for boundary in ("min", "max"):
        assert _call(b, "_read_row_group_stat", without_stats, 0, 0, boundary=boundary) == (
            "ok",
            None,
        ), "the writer must report no statistics as None"
        assert _call(m, "_read_row_group_stat", without_stats, 0, 0, boundary=boundary) == (
            "ok",
            None,
        ), "the reader must report no statistics as None"


class _Statistics:
    """The three attributes the reader touches, and nothing else."""

    def __init__(self, has_min_max: bool, minimum, maximum):
        self.has_min_max = has_min_max
        self.min = minimum
        self.max = maximum


class _Metadata:
    """Mirrors the `metadata.row_group(i).column(j).statistics` walk."""

    def __init__(self, statistics):
        self._statistics = statistics

    def row_group(self, index):
        return self

    def column(self, index):
        return type("_Column", (), {"statistics": self._statistics})()


@pytest.mark.parametrize("boundary", ["min", "max"])
@pytest.mark.parametrize("where", ["writer", "reader"])
def test_statistics_without_bounds_yield_none_on_both_sides(writer_reader, boundary, where):
    """A statistics object that exists but carries no bounds must be a quiet None.

    Parquet cannot easily produce this from a written file - disabling statistics
    leaves `statistics` as `None` altogether, which the earlier guard already
    handles - so the branch is reached with a stand-in that matches what the reader
    actually reads. It matters because treating absent bounds as values is how a
    segment acquires a wrong start or end date rather than none at all.
    """
    b, m = writer_reader
    module = b if where == "writer" else m
    metadata = _Metadata(_Statistics(has_min_max=False, minimum=1, maximum=2))
    assert _call(module, "_read_row_group_stat", metadata, 0, 0, boundary=boundary) == (
        "ok",
        None,
    ), f"the {where} used bounds from a statistics object that has none"


def test_what_the_writer_writes_the_reader_recovers(writer_reader, tmp_path):
    """The contract, end to end: labels survive the trip through the JSON file.

    The differential tests above compare two functions. This one exercises the
    thing the user actually sees - a catalog written to disk and read back - so a
    divergence in the entry shape itself is caught and not just a difference in
    how a single argument is split.
    """
    b, m = writer_reader
    path = tmp_path / "catalog.json"

    def labels(module, stem):
        """Both sides must be asked the same question to get a fair answer.

        `_extract_timeframe_and_segment` and `_extract_symbol_timeframe_and_segment`
        parse different conventions - a bare `1min_seg` stem versus a symbol-prefixed
        `AAPL_1min_seg` - and disagree about `AAPL_1min` for that reason, correctly.
        Comparing one against the other would report a real convention difference as
        a writer/reader split, so the round trip uses the symbol-prefixed parse on
        both sides, which is the one that yields all three labels.
        """
        symbol, timeframe, segment = module._extract_symbol_timeframe_and_segment(stem)
        return symbol, timeframe, segment

    entries = [
        {
            "path": stem,
            "symbol": labels(b, stem)[0],
            "timeframe": labels(b, stem)[1],
            "segment": labels(b, stem)[2],
            "start": b._format_catalog_date(DATES[0]),
            "end": b._format_catalog_date(datetime(2024, 2, 1, tzinfo=timezone.utc)),
        }
        for stem in STEMS
        if stem.strip()
    ]
    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(tmp_path),
        "summary": {
            "total_datasets": len(entries),
            "symbols": len({e["symbol"] for e in entries}),
            "sources": ["test"],
            "timeframes": sorted({str(e["timeframe"] or "") for e in entries}),
            "total_size_bytes": 0,
        },
        "ohlcv": entries,
        "fundamentals": [],
        "universes": {},
    }
    b.write_catalog_json(path, catalog)

    read_back = json.loads(Path(path).read_text(encoding="utf-8"))
    assert [e["path"] for e in read_back["ohlcv"]] == [e["path"] for e in entries]
    for entry in read_back["ohlcv"]:
        assert labels(m, entry["path"]) == (
            entry["symbol"],
            entry["timeframe"],
            entry["segment"],
        ), (
            f"the reader recovers {labels(m, entry['path'])!r} for {entry['path']!r}, "
            f"but the writer wrote {(entry['symbol'], entry['timeframe'], entry['segment'])!r}"
        )
        assert m._catalog_timestamp_to_epoch(entry["start"]) == m._catalog_timestamp_to_epoch(
            b._format_catalog_date(DATES[0])
        ), "a date that made the round trip no longer reads back to the same instant"
