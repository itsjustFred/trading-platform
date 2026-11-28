"""Tests for the in-repo incremental Dukascopy dataset pipeline.

All fixtures are network-free: bi5 payloads are built in memory with the same
``>5if`` record layout Dukascopy serves, lzma-compressed, and a fake fetcher
writes them into the raw cache exactly like ``fetch.fetch_bi5_range`` would.
"""

from __future__ import annotations

import json
import lzma
import struct
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data_tools.dukascopy import convert, pipeline, upload, validate
from data_tools.dukascopy.fetch import FetchStats

UTC = timezone.utc
FACTOR = 100000
PRICE = 1.10000


def _encode_bi5(records: list[tuple[int, int, int, int, int, float]]) -> bytes:
    """Build a compressed daily m1 candle payload.

    Record layout ``>5if`` = (seconds_into_day, open, close, low, high, volume)
    with prices as raw integer ticks (price * FACTOR).
    """
    buf = b"".join(struct.pack(">5if", *r) for r in records)
    return lzma.compress(buf)


def _minute_records(
    day: date, minutes: list[int], base: float = PRICE
) -> list[tuple[int, int, int, int, int, float]]:
    """One record per minute offset; deterministic OHLC around ``base``."""
    raw = round(base * FACTOR)
    out: list[tuple[int, int, int, int, int, float]] = []
    for m in minutes:
        o = raw + m
        c = raw + m + 1
        out.append((m * 60, o, c, o - 1, c + 1, 10.0 + m))
    return out


def _write_day(
    raw_dir: Path,
    duk: str,
    side: str,
    day: date,
    payload: bytes | None,
) -> None:
    path = raw_dir / duk / side / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}.bi5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if payload is not None else b"")


def _side_frame(day: date, minutes: list[int], *, ask: bool) -> pd.DataFrame:
    """Expected per-side M1 frame for the same synthetic records."""
    rows = []
    for m in minutes:
        ts = pd.Timestamp(datetime.combine(day, time.min, UTC)) + pd.Timedelta(minutes=m)
        o = (PRICE + (m + (1 if ask else 0)) / FACTOR)
        rows.append(
            {
                "timestamp": ts,
                "open": o,
                "high": o + 2 / FACTOR,
                "low": o - 1 / FACTOR,
                "close": o + 1 / FACTOR,
                "volume": 10.0 + m,
            }
        )
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


class FakeFetcher:
    """Fetcher that materialises fixture days into the raw bi5 cache."""

    def __init__(self, days: dict[date, dict[str, bytes | None]]):
        # days[day][side] -> compressed payload or None (empty day file)
        self.days = days
        self.calls: list[tuple[str, date, date]] = []
        self.fail_urls: list[str] = []

    def __call__(self, raw_dir: Path, symbol: str, start: date, end: date) -> FetchStats:
        self.calls.append((symbol, start, end))
        st = FetchStats()
        day = start
        while day < end:
            payloads = self.days.get(day, {})
            for side in ("BID", "ASK"):
                if side not in payloads:
                    continue
                _write_day(raw_dir, symbol, side, day, payloads[side])
                if payloads[side]:
                    st.ok += 1
                else:
                    st.empty += 1
            day += timedelta(days=1)
        st.failed = list(self.fail_urls)
        return st


def _trading_minutes(day: date) -> list[int]:
    """FX-like session: 22:00-23:59 plus, for weekdays, 00:00-20:59."""
    minutes = list(range(22 * 60, 24 * 60))
    if day.weekday() < 5:
        minutes = list(range(0, 21 * 60)) + minutes
    return minutes


def _fixture_days(start: date, ndays: int, *, duk: str = "EURUSD") -> dict:
    """Synthetic two-sided fixture: ``ndays`` consecutive days from ``start``."""
    days: dict[date, dict[str, bytes | None]] = {}
    for i in range(ndays):
        day = start + timedelta(days=i)
        minutes = _trading_minutes(day)
        days[day] = {
            "BID": _encode_bi5(_minute_records(day, minutes)),
            "ASK": _encode_bi5(
                [
                    (s, o + FACTOR // 10, c + FACTOR // 10, lw + FACTOR // 10, h + FACTOR // 10, v)
                    for (s, o, c, lw, h, v) in _minute_records(day, minutes)
                ]
            ),
        }
    return days


def _expected_frames(days: dict[date, dict[str, bytes | None]]) -> dict[str, pd.DataFrame]:
    """Ground truth: decode every fixture day and build all TF frames."""
    bid_parts, ask_parts = [], []
    for day in sorted(days):
        payloads = days[day]
        for side, parts in (("BID", bid_parts), ("ASK", ask_parts)):
            raw = lzma.decompress(payloads[side]) if payloads[side] else b""
            parts.append(convert.decode_bi5_day(raw, day, FACTOR))
    bid = pd.concat(bid_parts, ignore_index=True)
    ask = pd.concat(ask_parts, ignore_index=True)
    return convert.frames_for(bid, ask)


def _read(root: Path, symbol: str, tf: str) -> pd.DataFrame:
    return pd.read_parquet(root / symbol / f"{symbol}_{tf}.parquet")


# ─── bi5 decode ──────────────────────────────────────────────────────────────


def test_decode_bi5_day_decodes_records_and_drops_flats():
    day = date(2026, 11, 20)
    records = _minute_records(day, [0, 1, 2])
    # A flat filler minute: volume 0 and O=H=L=C.
    flat_raw = round(PRICE * FACTOR)
    records.append((3 * 60, flat_raw, flat_raw, flat_raw, flat_raw, 0.0))
    df = convert.decode_bi5_day(lzma.decompress(_encode_bi5(records)), day, FACTOR)

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 3  # flat minute dropped
    assert str(df["timestamp"].iloc[0]) == "2026-11-20 00:00:00+00:00"
    assert df["open"].iloc[0] == pytest.approx(PRICE + 0 / FACTOR, abs=1e-9)
    assert df["close"].iloc[0] == pytest.approx(PRICE + 1 / FACTOR, abs=1e-9)
    assert df["high"].iloc[0] == pytest.approx(PRICE + 2 / FACTOR, abs=1e-9)
    assert df["low"].iloc[0] == pytest.approx(PRICE - 1 / FACTOR, abs=1e-9)


def test_decode_bi5_day_rejects_truncated_payload():
    with pytest.raises(ValueError, match="truncated"):
        convert.decode_bi5_day(b"\x00" * 7, date(2026, 1, 1), FACTOR)


# ─── resampling ──────────────────────────────────────────────────────────────


def test_resample_m1_to_h1_d1_w1_boundaries():
    # Friday 2026-11-20 session plus Sunday 2026-11-22 evening (FX reopen).
    fri = date(2026, 11, 20)
    sun = date(2026, 11, 22)
    bid = pd.concat(
        [
            _side_frame(fri, [20 * 60, 21 * 60, 22 * 60, 23 * 60], ask=False),
            _side_frame(sun, [22 * 60, 23 * 60], ask=False),
        ],
        ignore_index=True,
    )
    ask = pd.concat(
        [
            _side_frame(fri, [20 * 60, 21 * 60, 22 * 60, 23 * 60], ask=True),
            _side_frame(sun, [22 * 60, 23 * 60], ask=True),
        ],
        ignore_index=True,
    )
    frames = convert.frames_for(bid, ask)

    h1 = frames["H1"]
    assert list(h1["timestamp"].dt.strftime("%Y-%m-%d %H:%M")) == [
        "2026-11-20 20:00",
        "2026-11-20 21:00",
        "2026-11-20 22:00",
        "2026-11-20 23:00",
        "2026-11-22 22:00",
        "2026-11-22 23:00",
    ]

    d1 = frames["D1"]
    # D1 bins are calendar days: Fri bars land on the 20th, Sun evening on the 22nd.
    assert list(d1["timestamp"].dt.strftime("%Y-%m-%d")) == ["2026-11-20", "2026-11-22"]

    w1 = frames["W1"]
    # W1 runs Sunday 00:00 -> next Sunday, labelled by the starting Sunday:
    # Friday's bars belong to the week starting 2026-11-15, Sunday evening's
    # bars open the week starting 2026-11-22.
    assert list(w1["timestamp"].dt.strftime("%Y-%m-%d")) == ["2026-11-15", "2026-11-22"]
    expected_vol = sum(10.0 + m for m in [20 * 60, 21 * 60, 22 * 60, 23 * 60])
    assert w1["bid_volume"].iloc[0] == pytest.approx(expected_vol)
    assert w1["bid_volume"].iloc[1] == pytest.approx(
        (10.0 + 22 * 60) + (10.0 + 23 * 60)
    )


def test_week_floor_sunday_alignment():
    assert str(convert.week_floor(pd.Timestamp("2026-11-25 13:45", tz="UTC"))) == (
        "2026-11-22 00:00:00+00:00"
    )
    assert str(convert.week_floor(pd.Timestamp("2026-11-22 00:00", tz="UTC"))) == (
        "2026-11-22 00:00:00+00:00"
    )
    assert str(convert.week_floor(pd.Timestamp("2026-11-21 23:59", tz="UTC"))) == (
        "2026-11-15 00:00:00+00:00"
    )


def test_combine_sides_sorts_minutes_present_on_one_side_only():
    """One-sided-only minutes must not leave the joined frame unsorted."""
    day = date(2026, 11, 20)
    # bid: 00:00 and 02:00; ask: 01:00 and 02:00 -> 00:00 bid-only, 01:00
    # ask-only; inputs deliberately out of order.
    bid = _side_frame(day, [120, 0], ask=False)
    ask = _side_frame(day, [60, 120], ask=True)
    out = convert.combine_sides(bid, ask)
    assert out["timestamp"].is_monotonic_increasing
    assert list(out["timestamp"].dt.strftime("%H:%M")) == ["00:00", "01:00", "02:00"]
    assert bool(out["bid_open"].isna().iloc[1])
    assert bool(out["ask_open"].isna().iloc[0])


# ─── validation ──────────────────────────────────────────────────────────────


def _valid_frame() -> pd.DataFrame:
    ts = pd.to_datetime(["2026-11-20 00:00", "2026-11-20 00:01"], utc=True)
    row = {
        "bid_open": 1.0, "bid_high": 1.1, "bid_low": 0.9, "bid_close": 1.05, "bid_volume": 1.0,
        "ask_open": 1.01, "ask_high": 1.11, "ask_low": 0.91, "ask_close": 1.06, "ask_volume": 1.0,
    }
    return pd.DataFrame([{"timestamp": t, **row} for t in ts])


@pytest.mark.parametrize(
    "mutate, match",
    [
        (
            lambda df: df.assign(
                timestamp=pd.to_datetime(
                    ["2026-11-20 00:01", "2026-11-20 00:00"], utc=True
                )
            ),
            "increasing",
        ),
        (lambda df: df.iloc[[0, 0]].reset_index(drop=True), "increasing"),
        (lambda df: df.assign(bid_open=np.nan), "NaN"),
        (lambda df: df.assign(ask_open=0.5), "ask_open"),
        (lambda df: df.assign(bid_high=0.5), "inconsistent"),
        (lambda df: df.assign(ask_low=2.0), "inconsistent"),
    ],
)
def test_validate_frame_rejects_contract_violations(mutate, match):
    df = mutate(_valid_frame())
    with pytest.raises(validate.DatasetValidationError, match=match):
        validate.validate_frame(df, context="T")


def test_validate_frame_accepts_valid():
    validate.validate_frame(_valid_frame(), context="T")


# ─── incremental update ──────────────────────────────────────────────────────


def test_incremental_update_appends_without_duplicate_or_missing_bars(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    start = date(2026, 11, 16)  # Monday; 7 days covers a full Sunday-start week
    days1 = _fixture_days(start, 4)
    days2 = _fixture_days(start + timedelta(days=4), 3)

    fetcher = FakeFetcher({**days1, **days2})

    # First publish: explicit start so the window is small.
    rep1 = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-20",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep1.ok and rep1.manifest_written
    # Second run: no start -> resumes from manifest last (2026-11-19 last day
    # fetched; its bars end 23:59). Fetcher now also serves 3 more days.
    rep2 = pipeline.update_dataset(
        root, symbols=["EURUSD"], end="2026-11-23",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep2.ok
    (sym,) = [s for s in rep2.symbols if s.symbol == "EURUSD"]
    assert sym.status == "updated"
    assert sym.new_m1_rows > 0

    m1 = _read(root, "EURUSD", "M1")
    assert not m1["timestamp"].duplicated().any()
    assert m1["timestamp"].is_monotonic_increasing

    # Seam: every stored day-1..3 timestamp is still present exactly once and
    # the tail matches the ground truth built from all seven days.
    expected = _expected_frames({**days1, **days2})
    pd.testing.assert_frame_equal(
        m1.reset_index(drop=True), expected["M1"].reset_index(drop=True),
        check_dtype=False,
    )
    for tf in convert.TIMEFRAMES:
        got = _read(root, "EURUSD", tf)
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), expected[tf].reset_index(drop=True),
            check_dtype=False,
        )

    manifest = json.loads((root / "manifest.json").read_text())
    entry = manifest["EURUSD"]
    assert entry["dukascopy_symbol"] == "EURUSD"
    assert entry["files"]["EURUSD_M1.parquet"]["rows"] == len(m1)
    assert entry["files"]["EURUSD_M1.parquet"]["last"] == str(m1["timestamp"].iloc[-1])
    assert pipeline.verify_manifest(root) == []


def test_incremental_update_is_idempotent_when_up_to_date(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    before = {p: p.read_bytes() for p in (root / "EURUSD").glob("*.parquet")}
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    (sym,) = rep.symbols
    # Window collapses (last bar day == fetch end - nothing new) or refetches
    # identical data -> up-to-date, files untouched.
    assert sym.status in {"up-to-date"}
    after = {p: p.read_bytes() for p in (root / "EURUSD").glob("*.parquet")}
    assert before == after


def test_failed_validation_publishes_nothing(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 2)
    # Corrupt the ASK side of the second day: ask below bid.
    bad = dict(days[date(2026, 11, 17)])
    minutes = _trading_minutes(date(2026, 11, 17))
    bad["ASK"] = _encode_bi5(
        [
            (s, o - 2 * FACTOR, c - 2 * FACTOR, lw - 2 * FACTOR, h - 2 * FACTOR, v)
            for (s, o, c, lw, h, v) in _minute_records(date(2026, 11, 17), minutes)
        ]
    )
    days[date(2026, 11, 17)] = bad

    fetcher = FakeFetcher(days)
    with pytest.raises(pipeline.IncompleteFetchError):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-18",
            raw_dir=raw, fetcher=fetcher,
        )
    # Nothing published: no symbol dir files, no manifest.
    assert not (root / "manifest.json").exists()
    leftovers = list(root.rglob("*.parquet"))
    assert leftovers == []
    # Staged temps are cleaned up too.
    assert list(root.rglob("*.tmp")) == []


def test_incomplete_fetch_publishes_nothing(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 2)
    fetcher = FakeFetcher(days)
    fetcher.fail_urls = ["http://datafeed.dukascopy.com/datafeed/EURUSD/x"]
    with pytest.raises(pipeline.IncompleteFetchError, match="failed"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-18",
            raw_dir=raw, fetcher=fetcher,
        )
    assert not (root / "manifest.json").exists()


def test_missing_day_files_are_recorded_in_manifest(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    # Fri 20 .. Sun 22; Saturday has no data (market closed): empty files, no
    # bars, so the two-sided join never sees a NaN and no gap is raised.
    days = _fixture_days(date(2026, 11, 20), 3)
    sat = date(2026, 11, 21)
    days[sat] = {"BID": None, "ASK": None}
    fetcher = FakeFetcher(days)
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-20", end="2026-11-23",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep.ok
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["EURUSD"]["missing_days_ask"] == [sat.isoformat()]
    assert manifest["EURUSD"]["missing_days_bid"] == [sat.isoformat()]


# ─── R2 upload ───────────────────────────────────────────────────────────────


class FakeR2:
    def __init__(self):
        self.puts: list[str] = []
        self.heads: list[str] = []
        self.sizes: dict[str, int] = {}

    def put_file(self, local_path: Path, key: str) -> int:
        self.puts.append(key)
        self.sizes[key] = local_path.stat().st_size
        return self.sizes[key]

    def head_size(self, key: str) -> int | None:
        self.heads.append(key)
        return self.sizes.get(key)


def _make_dataset(root: Path) -> list[str]:
    sym = root / "EURUSD"
    sym.mkdir(parents=True)
    frame = _valid_frame()
    frame.to_parquet(sym / "EURUSD_M1.parquet", index=False)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "EURUSD": {
                    "asset_class": "forex",
                    "files": {"EURUSD_M1.parquet": {"rows": len(frame)}},
                }
            }
        )
    )
    return ["EURUSD/EURUSD_M1.parquet", "manifest.json"]


def test_upload_orders_manifest_last_and_verifies_sizes(tmp_path):
    root = tmp_path / "ds"
    _make_dataset(root)
    client = FakeR2()
    uploaded = upload.upload_dataset(root, client=client)
    assert client.puts == ["EURUSD/EURUSD_M1.parquet", "manifest.json"]
    assert client.heads == [
        "EURUSD/EURUSD_M1.parquet",
        "EURUSD/EURUSD_M1.parquet",
        "manifest.json",
    ]
    assert uploaded == ["EURUSD/EURUSD_M1.parquet", "manifest.json"]


def test_upload_size_mismatch_raises(tmp_path):
    root = tmp_path / "ds"
    _make_dataset(root)

    class BadHead(FakeR2):
        def head_size(self, key):
            return 1

    with pytest.raises(RuntimeError, match="size mismatch"):
        upload.upload_dataset(root, client=BadHead())


def test_r2_config_requires_env():
    with pytest.raises(RuntimeError, match="R2_ENDPOINT"):
        upload.r2_config_from_env({})
    cfg = upload.r2_config_from_env(
        {
            "R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
            "R2_ACCESS_KEY_ID": "k",
            "R2_SECRET_ACCESS_KEY": "s",
            "R2_BUCKET": "tp-ohlcv",
        }
    )
    assert cfg["bucket"] == "tp-ohlcv" and cfg["region"] == "auto"


# ─── review fixes: one-sided minutes, manifest metadata, gaps, repair ────────


def _one_sided_frame() -> pd.DataFrame:
    """Two rows: one normal, one where the whole ask side is NaN."""
    df = _valid_frame()
    extra = df.iloc[[0]].copy()
    for c in ("ask_open", "ask_high", "ask_low", "ask_close", "ask_volume"):
        extra[c] = np.nan
    extra["timestamp"] = pd.Timestamp("2026-11-20 00:02", tz="UTC")
    return pd.concat([df, extra], ignore_index=True)


def test_validate_frame_allows_one_sided_rows_and_counts_them():
    count = validate.validate_frame(_one_sided_frame(), context="T")
    assert count == 1


def test_validate_frame_rejects_partial_and_double_sided_nan():
    df = _one_sided_frame()
    # Partial NaN on the ask side (only open missing) must still fail.
    partial = df.copy()
    partial.loc[partial["ask_high"].isna(), "ask_open"] = 1.01
    with pytest.raises(validate.DatasetValidationError, match="NaN"):
        validate.validate_frame(partial, context="T")
    # A row with both sides fully NaN must fail.
    both = df.copy()
    both.loc[
        both["ask_high"].isna(),
        [f"bid_{c}" for c in ("open", "high", "low", "close", "volume")],
    ] = np.nan
    with pytest.raises(validate.DatasetValidationError, match="NaN"):
        validate.validate_frame(both, context="T")


def test_incremental_update_tolerates_one_sided_minutes(tmp_path):
    """SP500-style fixture: a minute where only one side traded must publish."""
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    day = date(2026, 11, 16)
    minutes = _trading_minutes(day)
    # Bid has minute 60; ask does not -> one-sided minute in the combined M1.
    ask_minutes = [m for m in minutes if m != 60]
    days = {
        day: {
            "BID": _encode_bi5(_minute_records(day, minutes)),
            "ASK": _encode_bi5(
                [
                    (s, o + FACTOR // 10, c + FACTOR // 10, lw + FACTOR // 10, h + FACTOR // 10, v)
                    for (s, o, c, lw, h, v) in _minute_records(day, ask_minutes)
                ]
            ),
        }
    }
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-17",
        raw_dir=raw, fetcher=FakeFetcher(days),
    )
    assert rep.ok
    (sym,) = rep.symbols
    assert sym.one_sided_rows["M1"] == 1
    m1 = _read(root, "EURUSD", "M1")
    row = m1.loc[m1["timestamp"] == pd.Timestamp("2026-11-16 01:00", tz="UTC")]
    assert len(row) == 1 and bool(row["bid_open"].notna().iloc[0])
    assert bool(row["ask_open"].isna().iloc[0])


def test_manifest_entries_carry_asset_class_region_source_symbol(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    # Pre-seed a manifest entry for a symbol that will NOT be updated.
    (root / "GBPUSD").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {"GBPUSD": {"dukascopy_symbol": "GBPUSD", "decimal_factor": 100000, "files": {}}}
        )
    )
    days = _fixture_days(date(2026, 11, 16), 2)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-18",
        raw_dir=raw, fetcher=FakeFetcher(days),
    )
    manifest = json.loads((root / "manifest.json").read_text())
    eur = manifest["EURUSD"]
    assert eur["asset_class"] == "forex"
    assert eur["region"] == ""
    assert eur["source_symbol"] == "EURUSD"
    gbp = manifest["GBPUSD"]
    assert gbp["asset_class"] == "forex"
    assert gbp["region"] == ""
    assert gbp["source_symbol"] == "GBPUSD"


def test_index_symbol_gets_indices_asset_class(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 1, duk="USA500IDXUSD")
    # Fixture prices are FX-scale; SP500 factor is 1000 so prices will be huge
    # but still valid OHLC — validation only checks internal consistency.
    pipeline.update_dataset(
        root, symbols=["SP500"], start="2026-11-16", end="2026-11-17",
        raw_dir=raw, fetcher=FakeFetcher(days),
    )
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["SP500"]["asset_class"] == "indices"
    assert manifest["SP500"]["dukascopy_symbol"] == "USA500IDXUSD"
    assert manifest["SP500"]["source_symbol"] == "USA500IDXUSD"


def test_weekday_gap_fails_the_run(tmp_path):
    """An empty both-sides day on an open weekday must fail and publish nothing."""
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    start = date(2026, 11, 16)  # Monday
    days = _fixture_days(start, 7)  # Mon 16 .. Sun 22
    # Thursday 2026-11-19: market open, but Dukascopy returns nothing.
    days[date(2026, 11, 19)] = {"BID": None, "ASK": None}
    fetcher = FakeFetcher(days)
    with pytest.raises(pipeline.IncompleteFetchError, match="gap"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-23",
            raw_dir=raw, fetcher=fetcher,
        )
    assert not (root / "manifest.json").exists()


def test_saturday_gap_is_accepted_and_recorded(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    start = date(2026, 11, 16)
    days = _fixture_days(start, 7)
    sat = date(2026, 11, 21)
    days[sat] = {"BID": None, "ASK": None}
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-23",
        raw_dir=raw, fetcher=FakeFetcher(days),
    )
    assert rep.ok
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["EURUSD"]["missing_days_bid"] == [sat.isoformat()]
    assert manifest["EURUSD"]["missing_days_ask"] == [sat.isoformat()]


def test_one_sided_day_fails_when_market_open(tmp_path):
    """BID has bars, ASK file empty on a weekday -> not a silent one-sided day."""
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    start = date(2026, 11, 16)
    days = _fixture_days(start, 2)
    tue = date(2026, 11, 17)
    days[tue] = {"BID": days[tue]["BID"], "ASK": None}
    with pytest.raises(pipeline.IncompleteFetchError, match="one-sided"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-18",
            raw_dir=raw, fetcher=FakeFetcher(days),
        )
    assert not (root / "manifest.json").exists()


def test_late_arriving_day_is_picked_up_on_next_run(tmp_path):
    """A 0-byte cached day inside the fetch window is refetched, not final."""
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    start = date(2026, 11, 16)
    days = _fixture_days(start, 7)  # Mon 16 .. Sun 22
    sat = date(2026, 11, 21)  # Saturday: empty is acceptable
    sun = date(2026, 11, 22)  # Sunday session absent: keeps the refetch window open
    days[sat] = {"BID": None, "ASK": None}
    days[sun] = {"BID": None, "ASK": None}
    fetcher = FakeFetcher(days)
    rep1 = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-23",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep1.ok
    # Dukascopy publishes Saturday bars late (or the feed recovers): the next
    # run's window still covers the cached 0-byte day and must overwrite it.
    late = _fixture_days(sat, 1)[sat]
    days[sat] = late
    rep2 = pipeline.update_dataset(
        root, symbols=["EURUSD"], end="2026-11-23",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep2.ok
    m1 = _read(root, "EURUSD", "M1")
    sat_bars = m1.loc[m1["timestamp"].dt.date == sat]
    assert len(sat_bars) > 0


def test_missing_m1_file_fails_and_only_full_rebuilds(tmp_path, monkeypatch):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    monkeypatch.setattr(pipeline, "DATASET_START", date(2026, 11, 16))
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    (root / "EURUSD" / "EURUSD_M1.parquet").unlink()
    # Incremental run must refuse: rebuilding history from a 1-day window
    # would silently truncate the dataset.
    with pytest.raises(pipeline.IncompleteFetchError, match="M1"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], end="2026-11-19",
            raw_dir=raw, fetcher=fetcher,
        )
    # --full rebuilds from scratch and restores a consistent dataset.
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], full=True, end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep.ok
    assert pipeline.verify_manifest(root) == []


def test_full_ignores_stored_m1_and_refuses_shrinking_coverage(tmp_path, monkeypatch):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    monkeypatch.setattr(pipeline, "DATASET_START", date(2026, 11, 16))
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    # Poison the stored M1 with an extra stale row and a matching manifest lie
    # about the row count (first/last stay truthful so the --full span guard
    # still sees the published coverage).
    m1_path = root / "EURUSD" / "EURUSD_M1.parquet"
    m1 = pd.read_parquet(m1_path)
    stale = m1.iloc[[0]].copy()
    stale["timestamp"] = pd.Timestamp("2015-06-01", tz="UTC")
    poisoned = pd.concat([stale, m1], ignore_index=True)
    poisoned.to_parquet(m1_path, index=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["EURUSD"]["files"]["EURUSD_M1.parquet"]["rows"] = len(poisoned)
    (root / "manifest.json").write_text(json.dumps(manifest))
    fetcher.calls.clear()
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], full=True, end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep.ok
    # Full rebuild started at DATASET_START and ignored the poisoned stored M1.
    assert fetcher.calls[0][1] == date(2026, 11, 16)
    m1 = _read(root, "EURUSD", "M1")
    assert "2015-06-01" not in set(m1["timestamp"].dt.strftime("%Y-%m-%d"))
    assert pipeline.verify_manifest(root) == []


def test_full_refuses_rebuild_that_shrinks_the_published_span(tmp_path, monkeypatch):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    monkeypatch.setattr(pipeline, "DATASET_START", date(2026, 11, 16))
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    # The manifest claims data through next week; the rebuild cannot cover it.
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["EURUSD"]["files"]["EURUSD_M1.parquet"]["last"] = (
        "2026-11-25 00:00:00+00:00"
    )
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(pipeline.IncompleteFetchError, match="span"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], full=True, end="2026-11-19",
            raw_dir=raw, fetcher=fetcher,
        )


def test_start_after_manifest_last_is_rejected(tmp_path):
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    with pytest.raises(ValueError, match="start"):
        pipeline.update_dataset(
            root, symbols=["EURUSD"], start="2026-12-01", end="2026-12-05",
            raw_dir=raw, fetcher=fetcher,
        )


def test_stale_higher_tf_is_repaired_on_rerun(tmp_path):
    """Crash between M1 and manifest publish: rerun must rebuild higher TFs."""
    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 3)
    fetcher = FakeFetcher(days)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    # Simulate the crash: H1 on disk is an older, shorter file while the
    # manifest still describes the full one.
    h1_path = root / "EURUSD" / "EURUSD_H1.parquet"
    h1 = pd.read_parquet(h1_path)
    h1.iloc[:-3].to_parquet(h1_path, index=False)
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], end="2026-11-19",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep.ok
    assert pipeline.verify_manifest(root) == []
    fixed = pd.read_parquet(h1_path)
    assert len(fixed) == len(h1)


def test_published_parquet_is_world_readable(tmp_path):
    import stat as stat_mod

    root = tmp_path / "dukascopy"
    raw = tmp_path / "raw"
    days = _fixture_days(date(2026, 11, 16), 1)
    pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-17",
        raw_dir=raw, fetcher=FakeFetcher(days),
    )
    for p in (root / "EURUSD").glob("*.parquet"):
        mode = stat_mod.S_IMODE(p.stat().st_mode)
        assert mode & 0o004, f"{p.name} mode {mode:o} is not world-readable"


def test_fetch_jobs_refetch_existing_and_empty_files(tmp_path):
    """The raw cache must not treat a cached file (even non-empty) as final."""
    from data_tools.dukascopy import fetch as fetch_mod

    raw = tmp_path / "raw"
    out = raw / "EURUSD" / "BID" / "2026" / "11" / "20.bi5"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"cached")
    todo = list(fetch_mod.jobs(raw, date(2026, 11, 20), date(2026, 11, 21), ["EURUSD"]))
    urls = [u for u, _ in todo]
    assert any("2026/10/20/BID" in u for u in urls)


def test_fetch_uses_https():
    from data_tools.dukascopy import fetch as fetch_mod

    assert fetch_mod.URL.startswith("https://")


def test_verify_manifest_checks_span_without_column_statistics(tmp_path):
    """Parquet files written without stats still get their span verified."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "dukascopy"
    sym = root / "EURUSD"
    sym.mkdir(parents=True)
    df = _valid_frame()
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, sym / "EURUSD_M1.parquet", write_statistics=False)
    manifest = {
        "EURUSD": {
            "files": {
                "EURUSD_M1.parquet": {
                    "rows": len(df),
                    "first": str(df["timestamp"].iloc[0]),
                    "last": str(df["timestamp"].iloc[-1]),
                }
            }
        }
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert pipeline.verify_manifest(root) == []
    # Drift the claimed span: the stats-free footer must still catch it.
    manifest["EURUSD"]["files"]["EURUSD_M1.parquet"]["last"] = (
        "2027-01-01 00:00:00+00:00"
    )
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert any("span" in p for p in pipeline.verify_manifest(root))


def test_empty_symbol_list_processes_nothing(tmp_path):
    rep = pipeline.update_dataset(
        tmp_path / "dukascopy", symbols=[], end="2026-11-19",
        raw_dir=tmp_path / "raw",
    )
    assert rep.symbols == []
    assert rep.ok


# ─── upload: manifest-driven, idempotent ─────────────────────────────────────


def _seed_dataset(root: Path) -> None:
    sym = root / "EURUSD"
    sym.mkdir(parents=True)
    _valid_frame().to_parquet(sym / "EURUSD_M1.parquet", index=False)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "EURUSD": {
                    "asset_class": "forex",
                    "region": "",
                    "source_symbol": "EURUSD",
                    "files": {"EURUSD_M1.parquet": {"rows": 2}},
                }
            }
        )
    )


def test_upload_decides_from_manifest_not_publish_list(tmp_path):
    """A rerun after a failed upload must re-send the data files, not only the manifest."""
    root = tmp_path / "ds"
    _seed_dataset(root)
    client = FakeR2()
    # Simulate the earlier failure: remote has only an old manifest.
    client.sizes["manifest.json"] = 1
    uploaded = upload.upload_dataset(root, client=client)
    assert client.puts == ["EURUSD/EURUSD_M1.parquet", "manifest.json"]
    assert uploaded[-1] == "manifest.json"


def test_upload_is_idempotent_and_skips_matching_sizes(tmp_path):
    root = tmp_path / "ds"
    _seed_dataset(root)
    client = FakeR2()
    upload.upload_dataset(root, client=client)
    client.puts.clear()
    client.heads.clear()
    upload.upload_dataset(root, client=client)
    # Data file size already matches -> only the manifest commit point re-PUTs.
    assert client.puts == ["manifest.json"]


def test_upload_never_sends_manifest_before_data(tmp_path):
    root = tmp_path / "ds"
    _seed_dataset(root)

    class FailingPut(FakeR2):
        def put_file(self, local_path: Path, key: str) -> int:
            if key != "manifest.json":
                raise RuntimeError("boom")
            return super().put_file(local_path, key)

    with pytest.raises(RuntimeError, match="boom"):
        upload.upload_dataset(root, client=FailingPut())


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_download_subcommand_forwards_options(monkeypatch):
    import data_tools.dukascopy.__main__ as cli
    from data_tools.dukascopy import native

    seen = {}

    def fake_main():
        seen["argv"] = list(__import__("sys").argv)

    monkeypatch.setattr(native, "main", fake_main)
    rc = cli.main(["download", "--symbols", "EURUSD", "--timeframes", "d1"])
    assert rc == 0
    assert seen["argv"] == [
        "dukascopy-native", "--symbols", "EURUSD", "--timeframes", "d1"
    ]


def test_update_cli_failure_emits_alert(monkeypatch, tmp_path):
    import data_tools.dukascopy.__main__ as cli

    alerts = []
    monkeypatch.setattr(cli, "_emit_alert", lambda msg, **kw: alerts.append(msg))
    monkeypatch.setattr(cli, "_alert_sink", lambda: None)
    monkeypatch.setattr(cli, "_close_alert_sink", lambda: None)
    # shared.constants must be imported so .env is loaded before alerts/upload.
    rc = cli.main(
        ["update", "--root", str(tmp_path / "ds2"), "--symbols", "NOTASYMBOL"]
    )
    assert rc == 1
    assert alerts
