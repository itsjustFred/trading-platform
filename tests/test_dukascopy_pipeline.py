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
    days = _fixture_days(date(2026, 11, 16), 2)
    # Day 2 has no data on either side (e.g. a holiday): empty files, no bars,
    # so the two-sided join never sees a NaN.
    day2 = date(2026, 11, 17)
    days[day2] = {"BID": None, "ASK": None}
    fetcher = FakeFetcher(days)
    rep = pipeline.update_dataset(
        root, symbols=["EURUSD"], start="2026-11-16", end="2026-11-18",
        raw_dir=raw, fetcher=fetcher,
    )
    assert rep.ok
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["EURUSD"]["missing_days_ask"] == ["2026-11-17"]
    assert manifest["EURUSD"]["missing_days_bid"] == ["2026-11-17"]


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
    (root / "manifest.json").write_text("{}")
    return ["EURUSD/EURUSD_M1.parquet", "manifest.json"]


def test_upload_orders_manifest_last_and_verifies_sizes(tmp_path):
    root = tmp_path / "ds"
    published = _make_dataset(root)
    client = FakeR2()
    uploaded = upload.upload_dataset(root, published, client=client)
    assert client.puts == ["EURUSD/EURUSD_M1.parquet", "manifest.json"]
    assert client.heads == client.puts
    assert uploaded == published


def test_upload_size_mismatch_raises(tmp_path):
    root = tmp_path / "ds"
    published = _make_dataset(root)

    class BadHead(FakeR2):
        def head_size(self, key):
            return 1

    with pytest.raises(RuntimeError, match="size mismatch"):
        upload.upload_dataset(root, published, client=BadHead())


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
