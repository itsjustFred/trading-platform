"""Incremental update of the canonical Dukascopy dataset.

Layout (mirrored to the ``tp-ohlcv`` R2 bucket)::

    <root>/manifest.json
    <root>/<SYM>/<SYM>_<TF>.parquet     TF in M1 M5 M15 H1 H4 D1 W1

Each run reads ``manifest.json``, verifies every published file still matches
its manifest entry (a crash between renames and the manifest write is
repaired, a missing M1 file fails the run), fetches bi5 day files from each
symbol's last M1 timestamp to the last complete UTC day, merges them into M1
without duplicate bars at the seam, re-resamples the higher timeframes from
the first affected bin, validates every frame — including the market-closed
gap check — then publishes staged files atomically with ``manifest.json``
written last. ``full=True`` rebuilds every symbol from :data:`DATASET_START`,
ignoring stored M1 and the raw cache. Nothing is published when any symbol
fails validation, fetch or gap check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat as stat_mod
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from data_tools.dukascopy import calendar as duk_calendar
from data_tools.dukascopy import convert, fetch, validate
from data_tools.dukascopy.convert import CANONICAL_SYMBOLS, INSTRUMENTS, TIMEFRAMES

logger = logging.getLogger(__name__)

#: First day of the published dataset (matches the R2 mirror).
DATASET_START = date(2015, 1, 1)
MANIFEST_NAME = "manifest.json"

#: ``--full`` refuses to publish when the rebuilt M1 has fewer than this
#: fraction of the rows the manifest claimed — a guard against a truncated
#: fetch silently shrinking the dataset.
FULL_MIN_ROW_RATIO = 0.9

ASSET_CLASSES: dict[str, frozenset[str]] = {
    "forex": frozenset({"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD"}),
    "metals": frozenset({"XAUUSD", "XAGUSD"}),
    "indices": frozenset({"NAS100", "SP500", "US30", "DAX", "EURO50", "FTSE100"}),
    "energy": frozenset({"BRENT", "CRUDE"}),
}

_ASSET_CLASS_BY_SYMBOL = {
    symbol: asset_class
    for asset_class, symbols in ASSET_CLASSES.items()
    for symbol in symbols
}

FetchFn = Callable[[Path, str, date, date], "fetch.FetchStats"]


class IncompleteFetchError(RuntimeError):
    """One or more bi5 day files could not be fetched or a frame failed validation."""


@dataclass
class SymbolReport:
    symbol: str
    dukascopy_symbol: str = ""
    status: str = "pending"  # updated | up-to-date | failed | incomplete
    fetched_files: int = 0
    new_m1_rows: int = 0
    one_sided_rows: dict[str, int] = field(default_factory=dict)
    error: str = ""
    file_entries: dict[str, dict] = field(default_factory=dict)
    missing_days: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class UpdateReport:
    root: Path
    symbols: list[SymbolReport] = field(default_factory=list)
    manifest_written: bool = False
    published: list[str] = field(default_factory=list)  # paths relative to root

    @property
    def ok(self) -> bool:
        return all(s.status in {"updated", "up-to-date"} for s in self.symbols)


def asset_class_for(symbol: str) -> str:
    return _ASSET_CLASS_BY_SYMBOL.get(symbol, "")


def _read_manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: manifest is not a JSON object")
    return payload


def _m1_last_ts(entry: dict, symbol: str) -> pd.Timestamp | None:
    files = entry.get("files") or {}
    info = files.get(f"{symbol}_M1.parquet") or {}
    if not info.get("last"):
        return None
    ts = pd.to_datetime(str(info["last"]), utc=True, errors="coerce")
    return None if pd.isna(ts) else pd.Timestamp(ts)


def _fingerprint(df: pd.DataFrame) -> int:
    """Dtype-stable content hash used to detect whether a rewrite is needed."""
    h = hashlib.blake2b(digest_size=16)
    ts = (
        pd.to_datetime(df["timestamp"], utc=True)
        .dt.as_unit("ms")
        .astype("int64")
        .to_numpy()
    )
    h.update(np.ascontiguousarray(ts).tobytes())
    for c in df.columns:
        if c == "timestamp":
            continue
        vals = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype="float64")
        h.update(np.ascontiguousarray(vals).tobytes())
    return int.from_bytes(h.digest(), "little")


def _read_parquet(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_parquet(path)


def _parquet_footer(path: Path) -> dict | None:
    """``rows``/``first``/``last`` from parquet metadata, or None if unreadable."""
    import pyarrow.parquet as pq

    try:
        pf = pq.ParquetFile(path)
    except Exception:  # noqa: BLE001 - any read failure is a consistency problem
        return None
    md = pf.metadata
    names = list(pf.schema_arrow.names)
    if "timestamp" not in names or md.num_rows == 0:
        return {"rows": md.num_rows} if md.num_rows else None
    idx = names.index("timestamp")
    # Frames are written time-sorted: first/last live in the outer row groups.
    first_stats = md.row_group(0).column(idx).statistics
    last_stats = md.row_group(md.num_row_groups - 1).column(idx).statistics
    if (
        first_stats is None
        or last_stats is None
        or not getattr(first_stats, "has_min_max", False)
        or not getattr(last_stats, "has_min_max", False)
    ):
        return {"rows": md.num_rows}
    return {
        "rows": md.num_rows,
        "first": str(pd.to_datetime(first_stats.min, utc=True)),
        "last": str(pd.to_datetime(last_stats.max, utc=True)),
    }


def _entry_file_problems(root: Path, symbol: str, entry: dict) -> list[tuple[str, str]]:
    """``(filename, problem)`` for every manifest file that is missing or drifted."""
    problems: list[tuple[str, str]] = []
    for fname, info in (entry.get("files") or {}).items():
        path = root / symbol / fname
        if not path.exists():
            problems.append((fname, "missing file"))
            continue
        footer = _parquet_footer(path)
        if footer is None:
            problems.append((fname, "unreadable"))
            continue
        if footer.get("rows") != info.get("rows"):
            problems.append(
                (fname, f"rows {footer.get('rows')} != manifest {info.get('rows')}")
            )
            continue
        if "first" in footer and (
            footer.get("first") != info.get("first")
            or footer.get("last") != info.get("last")
        ):
            problems.append((fname, "span mismatch"))
    return problems


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _publish_mode(final: Path) -> int:
    """Mode for a published file: keep the existing file's mode, else 0666&~umask."""
    try:
        return stat_mod.S_IMODE(final.stat().st_mode)
    except OSError:
        return 0o666 & ~_umask()


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_parquet_tmp(df: pd.DataFrame, final: Path) -> Path:
    """Write ``df`` to a temp sibling of ``final``; caller renames to publish."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=final.name + ".", suffix=".tmp", dir=str(final.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.reset_index(drop=True).to_parquet(tmp, index=False, compression="zstd")
        _fsync_file(tmp)
        os.chmod(tmp, _publish_mode(final))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _merge_side(stored: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Append ``fresh`` M1 rows onto ``stored``; fresh wins on timestamp ties."""
    if stored.empty:
        return fresh.reset_index(drop=True)
    if fresh.empty:
        return stored.reset_index(drop=True)
    merged = pd.concat([stored, fresh], ignore_index=True)
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last")
    merged = merged.sort_values("timestamp").reset_index(drop=True)
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], utc=True)
    return merged


def _resample_all(bid_m1: pd.DataFrame, ask_m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "M1":
        return convert.combine_sides(bid_m1, ask_m1)
    rule = convert.RULES[tf]
    return convert.combine_sides(
        convert.resample_side(bid_m1, rule), convert.resample_side(ask_m1, rule)
    )


def _higher_tf_frame(
    existing: pd.DataFrame | None,
    bid_m1: pd.DataFrame,
    ask_m1: pd.DataFrame,
    tf: str,
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    """Stored bins before ``cutoff`` plus the resampled tail at/after it.

    When the stored file is missing entirely the frame is resampled from the
    whole M1 history — a partial file must never be silently truncated to the
    tail. ``bid_m1``/``ask_m1`` may be tail slices; only rows at/after
    ``cutoff`` are used.
    """
    if existing is None or existing.empty:
        return _resample_all(bid_m1, ask_m1, tf)
    rule = convert.RULES[tf]
    tail_bid = bid_m1.loc[bid_m1["timestamp"] >= cutoff]
    tail_ask = ask_m1.loc[ask_m1["timestamp"] >= cutoff]
    tail = convert.combine_sides(
        tail_bid if tail_bid.empty else convert.resample_side(tail_bid, rule),
        tail_ask if tail_ask.empty else convert.resample_side(tail_ask, rule),
    )
    head = existing.loc[existing["timestamp"] < cutoff]
    if head.empty:
        return tail
    out = pd.concat([head, tail], ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out


def _symbol_window(
    manifest: dict,
    symbol: str,
    *,
    full: bool,
    start: date | None,
    end: date,
) -> tuple[date, date] | None:
    """``(fetch_start, fetch_end)`` for one symbol, or None when up-to-date."""
    entry = manifest.get(symbol) or {}
    last = _m1_last_ts(entry, symbol)
    last_day = last.date() if last is not None else None
    if not full and start is not None and last_day is not None and start > last_day:
        raise ValueError(
            f"--start {start} is after the last published bar ({last_day}) "
            f"for {symbol}; refusing to append past a gap"
        )
    if full:
        fetch_start = start or DATASET_START
    elif start is not None:
        fetch_start = start
    elif last_day is None:
        fetch_start = DATASET_START
    else:
        # Refetch the whole day containing the last bar: cheap, and covers
        # bars Dukascopy published late for that day.
        fetch_start = last_day
    if fetch_start >= end:
        return None
    return fetch_start, end


def _resolve_symbol(key: str) -> tuple[str, str, int] | None:
    """Map a canonical name or Dukascopy code to ``(canonical, code, factor)``."""
    upper = key.strip().upper()
    for code, (name, factor) in INSTRUMENTS.items():
        if upper in {code, name}:
            return name, code, factor
    return None


def _frame_days(df: pd.DataFrame) -> set[date]:
    """Dates on which a one-sided M1 frame has any bar."""
    if df.empty:
        return set()
    return set(pd.to_datetime(df["timestamp"], utc=True).dt.date)


def _combined_side_days(df: pd.DataFrame, side: str) -> set[date]:
    """Dates on which a combined two-sided M1 frame has bars for ``side``."""
    col = f"{side}_open"
    if col not in df.columns:
        return set()
    mask = df[col].notna()
    if not mask.any():
        return set()
    return set(pd.to_datetime(df.loc[mask, "timestamp"], utc=True).dt.date)


def _gap_violations(
    symbol: str,
    bid_days: set[date],
    ask_days: set[date],
    fetch_start: date,
    fetch_end: date,
) -> list[str]:
    """Days in the fetch window that are empty while the market should be open."""
    bar_days = bid_days | ask_days
    problems: list[str] = []
    coverage = duk_calendar.empty_days_in_window(
        bid_days, ask_days, fetch_start, fetch_end
    )
    for iso in coverage["both"]:
        day = date.fromisoformat(iso)
        if not duk_calendar.market_closed(symbol, day, bar_days):
            problems.append(f"{symbol}: market gap on {iso} (no bars either side)")
    for key, empty_side in (("bid", "ask"), ("ask", "bid")):
        for iso in coverage[key]:
            day = date.fromisoformat(iso)
            if not duk_calendar.market_closed(symbol, day, bar_days):
                problems.append(
                    f"{symbol}: one-sided day on {iso} ({empty_side} side empty)"
                )
    return problems


def _merge_missing_days(entry: dict, missing: dict[str, list[str]]) -> None:
    for side, days in missing.items():
        key = f"missing_days_{side}"
        existing = list(entry.get(key) or [])
        entry[key] = sorted(set(existing) | set(days))


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _normalize_entry(
    symbol: str,
    base: dict | None,
    *,
    code: str | None = None,
    factor: int | None = None,
    files: dict[str, dict] | None = None,
    missing: dict[str, list[str]] | None = None,
) -> dict:
    """Manifest entry with the spec-required keys, preserving stored extras."""
    entry = dict(base or {})
    entry["asset_class"] = asset_class_for(symbol) or str(entry.get("asset_class") or "")
    entry["region"] = str(entry.get("region") or "")
    entry["source_symbol"] = (
        str(entry.get("source_symbol") or "")
        or str(entry.get("dukascopy_symbol") or "")
        or (code or symbol)
    )
    if code is not None:
        entry["dukascopy_symbol"] = code
    if factor is not None:
        entry["decimal_factor"] = factor
    if files is not None:
        entry["files"] = files
    if missing:
        _merge_missing_days(entry, missing)
    return entry


def _stage_changed(
    frames: dict[str, pd.DataFrame],
    sym_dir: Path,
    symbol: str,
    existing_frames: dict[str, pd.DataFrame | None],
    existing_fps: dict[str, int | None],
    staged: list[tuple[Path, Path]],
) -> None:
    """Stage every frame whose content differs from what is on disk."""
    for tf, df in frames.items():
        final = sym_dir / f"{symbol}_{tf}.parquet"
        fp = _fingerprint(df)
        existing_fp = existing_fps.get(tf)
        if existing_fp is None:
            existing = existing_frames.get(tf)
            existing_fp = (
                _fingerprint(existing)
                if existing is not None and not existing.empty
                else None
            )
        if existing_fp == fp:
            continue
        tmp = _write_parquet_tmp(df, final)
        staged.append((tmp, final))


def _existing_m1_state(
    sym_dir: Path, symbol: str, full: bool
) -> tuple[pd.DataFrame | None, int | None, pd.Timestamp | None, set[date], set[date]]:
    """Stored M1 frame plus fingerprint, last timestamp and per-side bar days."""
    existing = None if full else _read_parquet(sym_dir / f"{symbol}_M1.parquet")
    if existing is None or existing.empty:
        return None, None, None, set(), set()
    return (
        existing,
        _fingerprint(existing),
        pd.Timestamp(existing["timestamp"].iloc[-1]),
        _combined_side_days(existing, "bid"),
        _combined_side_days(existing, "ask"),
    )


def _merged_m1_sides(
    existing_m1: pd.DataFrame | None,
    bid_new: pd.DataFrame,
    ask_new: pd.DataFrame,
    *,
    full: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fresh-wins per-side merge of stored M1 with the newly fetched bars."""
    if full or existing_m1 is None or existing_m1.empty:
        return bid_new, ask_new
    bid_stored, ask_stored = convert.split_combined_m1(existing_m1)
    return _merge_side(bid_stored, bid_new), _merge_side(ask_stored, ask_new)


def _all_frames(
    symbol: str,
    sym_dir: Path,
    bid_m1: pd.DataFrame,
    ask_m1: pd.DataFrame,
    *,
    fetch_start: date,
    tf_rebuild: bool,
    full: bool,
    coverage_floor: int,
    existing_m1_fp: int | None,
    existing_m1_last: pd.Timestamp | None,
    report: SymbolReport,
) -> tuple[
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame | None],
    dict[str, int | None],
] | None:
    """Every timeframe frame for ``symbol``, or None when nothing changed.

    Returns ``(frames, existing_frames, existing_fingerprints)`` for
    ``_stage_changed``. Sets ``report.status``/``report.error`` and returns
    None on up-to-date, validation failure or coverage-floor refusal.
    """
    combined_m1 = convert.combine_sides(bid_m1, ask_m1)
    if combined_m1.empty:
        report.status = "failed"
        report.error = "no M1 bars produced"
        return None
    report.one_sided_rows["M1"] = validate.validate_frame(
        combined_m1, context=f"{symbol} M1"
    )
    if (
        existing_m1_fp is not None
        and _fingerprint(combined_m1) == existing_m1_fp
        and not tf_rebuild
    ):
        report.status = "up-to-date"
        return None
    if full and coverage_floor and len(combined_m1) < coverage_floor:
        report.status = "failed"
        report.error = (
            f"full rebuild produced {len(combined_m1)} M1 rows, below the "
            f"{coverage_floor}-row coverage floor"
        )
        return None
    if existing_m1_last is not None:
        report.new_m1_rows = int(
            (combined_m1["timestamp"] > existing_m1_last).sum()
        )
    else:
        report.new_m1_rows = len(combined_m1)

    frames: dict[str, pd.DataFrame] = {"M1": combined_m1}
    existing_frames: dict[str, pd.DataFrame | None] = {}
    existing_fps: dict[str, int | None] = {"M1": existing_m1_fp}

    def read_existing(tf: str) -> pd.DataFrame | None:
        existing = _read_parquet(sym_dir / f"{symbol}_{tf}.parquet")
        existing_frames[tf] = existing
        existing_fps[tf] = (
            _fingerprint(existing)
            if existing is not None and not existing.empty
            else None
        )
        return existing

    # Higher timeframes: rebuild from the first bin touched by the fetch
    # window — the bin containing fetch_start 00:00 UTC — unless stored
    # state is untrusted (tf_rebuild/full), in which case resample all of M1.
    start_ts = pd.Timestamp(fetch_start.isoformat(), tz="UTC")
    if tf_rebuild or full:
        for tf in TIMEFRAMES[1:]:
            frames[tf] = _resample_all(bid_m1, ask_m1, tf)
            report.one_sided_rows[tf] = validate.validate_frame(
                frames[tf], context=f"{symbol} {tf}"
            )
            read_existing(tf)
        del bid_m1, ask_m1
    else:
        # Only the M1 tail at/after the earliest cutoff is needed for the
        # re-resample; slice once instead of passing full history around.
        earliest = min(convert.bin_floor(tf, start_ts) for tf in TIMEFRAMES[1:])
        tail_bid = bid_m1.loc[bid_m1["timestamp"] >= earliest]
        tail_ask = ask_m1.loc[ask_m1["timestamp"] >= earliest]
        del bid_m1, ask_m1
        for tf in TIMEFRAMES[1:]:
            cutoff = convert.bin_floor(tf, start_ts)
            frames[tf] = _higher_tf_frame(
                read_existing(tf), tail_bid, tail_ask, tf, cutoff
            )
            report.one_sided_rows[tf] = validate.validate_frame(
                frames[tf], context=f"{symbol} {tf}"
            )
        del tail_bid, tail_ask
    return frames, existing_frames, existing_fps


def _process_symbol(
    root: Path,
    raw_dir: Path,
    symbol: str,
    dukascopy_symbol: str,
    factor: int,
    fetch_start: date,
    fetch_end: date,
    *,
    full: bool,
    tf_rebuild: bool,
    coverage_floor: int,
    fetcher: FetchFn,
    staged: list[tuple[Path, Path]],
) -> SymbolReport:
    report = SymbolReport(symbol=symbol, dukascopy_symbol=dukascopy_symbol)
    sym_dir = root / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)

    stats = fetcher(raw_dir, dukascopy_symbol, fetch_start, fetch_end)
    if getattr(stats, "failed", None):
        report.status = "incomplete"
        report.error = f"{len(stats.failed)} bi5 files failed after retries"
        return report
    report.fetched_files = getattr(stats, "done", 0)

    bid_new, missing_bid = convert.load_m1_side(
        raw_dir / dukascopy_symbol / "BID", factor, start=fetch_start, end=fetch_end
    )
    ask_new, missing_ask = convert.load_m1_side(
        raw_dir / dukascopy_symbol / "ASK", factor, start=fetch_start, end=fetch_end
    )
    report.missing_days = {"bid": missing_bid, "ask": missing_ask}

    (
        existing_m1,
        existing_m1_fp,
        existing_m1_last,
        stored_bid_days,
        stored_ask_days,
    ) = _existing_m1_state(sym_dir, symbol, full)
    bid_days = stored_bid_days | _frame_days(bid_new)
    ask_days = stored_ask_days | _frame_days(ask_new)
    violations = _gap_violations(symbol, bid_days, ask_days, fetch_start, fetch_end)
    if violations:
        report.status = "failed"
        report.error = "; ".join(violations[:10])
        return report

    bid_m1, ask_m1 = _merged_m1_sides(
        existing_m1, bid_new, ask_new, full=full
    )
    del existing_m1, bid_new, ask_new
    built = _all_frames(
        symbol,
        sym_dir,
        bid_m1,
        ask_m1,
        fetch_start=fetch_start,
        tf_rebuild=tf_rebuild,
        full=full,
        coverage_floor=coverage_floor,
        existing_m1_fp=existing_m1_fp,
        existing_m1_last=existing_m1_last,
        report=report,
    )
    if built is None:
        return report
    frames, existing_frames, existing_fps = built

    # Stage every changed file; nothing is published until all symbols pass.
    _stage_changed(frames, sym_dir, symbol, existing_frames, existing_fps, staged)

    report.status = "updated"
    report.file_entries = {
        f"{symbol}_{tf}.parquet": convert.file_entry(df) for tf, df in frames.items()
    }
    return report


def _repair_symbol(
    root: Path,
    symbol: str,
    dukascopy_symbol: str,
    staged: list[tuple[Path, Path]],
) -> SymbolReport:
    """Rebuild higher TFs from the stored M1 after a crashed/inconsistent publish."""
    report = SymbolReport(symbol=symbol, dukascopy_symbol=dukascopy_symbol)
    sym_dir = root / symbol
    m1_path = sym_dir / f"{symbol}_M1.parquet"
    m1 = _read_parquet(m1_path)
    if m1 is None or m1.empty:
        report.status = "failed"
        report.error = "stored M1 missing; run with --full to rebuild"
        return report
    report.one_sided_rows["M1"] = validate.validate_frame(m1, context=f"{symbol} M1")
    bid_m1, ask_m1 = convert.split_combined_m1(m1)
    frames: dict[str, pd.DataFrame] = {"M1": m1}
    existing_frames: dict[str, pd.DataFrame | None] = {"M1": m1}
    existing_fps: dict[str, int | None] = {"M1": _fingerprint(m1)}
    for tf in TIMEFRAMES[1:]:
        frames[tf] = _resample_all(bid_m1, ask_m1, tf)
        report.one_sided_rows[tf] = validate.validate_frame(
            frames[tf], context=f"{symbol} {tf}"
        )
        existing = _read_parquet(sym_dir / f"{symbol}_{tf}.parquet")
        existing_frames[tf] = existing
        existing_fps[tf] = (
            _fingerprint(existing)
            if existing is not None and not existing.empty
            else None
        )
    del bid_m1, ask_m1
    _stage_changed(frames, sym_dir, symbol, existing_frames, existing_fps, staged)
    report.status = "updated"
    report.file_entries = {
        f"{symbol}_{tf}.parquet": convert.file_entry(df) for tf, df in frames.items()
    }
    return report


def _clean_stale_tmps(root: Path) -> int:
    """Remove leftover publish temps from an interrupted run."""
    removed = 0
    candidates = list(root.glob("*.tmp")) + list(root.glob(".*.tmp"))
    for sym_dir in root.iterdir():
        if sym_dir.is_dir():
            candidates.extend(sym_dir.glob("*.tmp"))
    for tmp in candidates:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
            removed += 1
    return removed


def _consistency_check(
    root: Path,
    symbol: str,
    code: str,
    old_entry: dict,
    *,
    full: bool,
    log: Callable[[str], None],
) -> tuple[SymbolReport | None, bool]:
    """``(failed report or None, tf_rebuild)`` for stored-vs-manifest drift.

    A missing/drifted M1 can only be fixed by ``--full``; drifted higher TFs
    are rebuilt from the stored M1 (crash repair).
    """
    if full or not old_entry:
        return None, False
    problems = _entry_file_problems(root, symbol, old_entry)
    if not problems:
        return None, False
    if any(fname.endswith("_M1.parquet") for fname, _ in problems):
        return (
            SymbolReport(
                symbol=symbol,
                dukascopy_symbol=code,
                status="failed",
                error=(
                    "stored M1 missing or inconsistent "
                    f"({problems[0][1]}); run with --full to rebuild"
                ),
            ),
            False,
        )
    log(
        f"{symbol}: {len(problems)} published file(s) inconsistent; "
        "will rebuild higher timeframes from M1"
    )
    return None, True


def _coverage_floor(old_entry: dict, symbol: str, *, full: bool) -> int:
    """Minimum acceptable M1 row count for a ``--full`` rebuild."""
    if not full:
        return 0
    rows = ((old_entry.get("files") or {}).get(f"{symbol}_M1.parquet") or {}).get(
        "rows"
    )
    return int(int(rows) * FULL_MIN_ROW_RATIO) if rows else 0


def _rebuild_manifest(
    manifest: dict, updated_entries: dict[str, tuple[dict, dict]]
) -> dict[str, dict]:
    """Manifest with spec-required keys for every known symbol.

    Symbols not updated in this run keep their stored file/missing-day info;
    updated symbols get fresh ``files``/``missing_days_*`` entries.
    """
    new_manifest: dict[str, dict] = {}
    for symbol, entry in manifest.items():
        if not isinstance(entry, dict):
            new_manifest[symbol] = entry
            continue
        files, missing = updated_entries.get(symbol, (None, None))
        code = entry.get("dukascopy_symbol") or entry.get("source_symbol") or symbol
        resolved = _resolve_symbol(str(code))
        new_manifest[symbol] = _normalize_entry(
            symbol,
            entry,
            code=resolved[1] if resolved else None,
            factor=resolved[2] if resolved else None,
            files=files,
            missing=missing,
        )
    for symbol, (files, missing) in updated_entries.items():
        if symbol in new_manifest:
            continue
        resolved = _resolve_symbol(symbol)
        new_manifest[symbol] = _normalize_entry(
            symbol,
            {},
            code=resolved[1] if resolved else symbol,
            factor=resolved[2] if resolved else None,
            files=files,
            missing=missing,
        )
    return new_manifest


def _write_manifest(root: Path, new_manifest: dict) -> None:
    """Atomically replace ``manifest.json`` (temp file + fsync + rename)."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.", suffix=".tmp", dir=str(root)
    )
    os.close(fd)
    manifest_tmp = Path(tmp_name)
    try:
        manifest_tmp.write_text(json.dumps(new_manifest, indent=1))
        _fsync_file(manifest_tmp)
        os.chmod(manifest_tmp, _publish_mode(root / MANIFEST_NAME))
        os.replace(manifest_tmp, root / MANIFEST_NAME)
    except Exception:
        manifest_tmp.unlink(missing_ok=True)
        raise


def update_dataset(
    root: str | Path,
    *,
    symbols: list[str] | str | None = None,
    full: bool = False,
    start: str | date | None = None,
    end: str | date | None = None,
    raw_dir: str | Path | None = None,
    fetcher: FetchFn | None = None,
    concurrency: int = 8,
    log: Callable[[str], None] = logger.info,
) -> UpdateReport:
    """Run one incremental (or ``full``) dataset update.

    ``end`` is exclusive: the default is today's UTC date, so the fetch covers
    through the last complete UTC day. Returns an :class:`UpdateReport`;
    raises :class:`IncompleteFetchError` when any symbol failed, in which case
    nothing was published (staged temps removed, manifest untouched).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(raw_dir) if raw_dir else root / "_raw"
    fetch_fn: FetchFn = fetcher or (
        lambda rd, sym, s, e: fetch.fetch_bi5_range(
            rd, [sym], s, e, concurrency=concurrency
        )
    )

    manifest = _read_manifest(root)
    fetch_end = _parse_date(end) or datetime.now(timezone.utc).date()
    fetch_start_req = _parse_date(start)

    if isinstance(symbols, str):
        symbols = [symbols]
    wanted = list(symbols) if symbols else list(CANONICAL_SYMBOLS)

    cleaned = _clean_stale_tmps(root)
    if cleaned:
        log(f"removed {cleaned} leftover temp file(s) from an interrupted run")

    report = UpdateReport(root=root)
    staged: list[tuple[Path, Path]] = []
    failures: list[str] = []
    updated_entries: dict[str, tuple[dict, dict]] = {}

    for item in wanted:
        resolved = _resolve_symbol(str(item))
        if resolved is None:
            report.symbols.append(
                SymbolReport(symbol=str(item), status="failed", error="unknown symbol")
            )
            failures.append(f"{item}: unknown symbol")
            continue
        symbol, code, factor = resolved

        old_entry = manifest.get(symbol) or {}
        failed_check, tf_rebuild = _consistency_check(
            root, symbol, code, old_entry, full=full, log=log
        )
        if failed_check is not None:
            report.symbols.append(failed_check)
            failures.append(f"{symbol}: {failed_check.error}")
            continue

        window = _symbol_window(
            manifest, symbol, full=full, start=fetch_start_req, end=fetch_end
        )

        if window is None and not tf_rebuild:
            report.symbols.append(
                SymbolReport(symbol=symbol, dukascopy_symbol=code, status="up-to-date")
            )
            continue

        if window is None:
            # Data is current but published TFs drifted: repair from M1.
            sym_report = _repair_symbol(root, symbol, code, staged)
        else:
            fetch_start, _ = window
            coverage_floor = _coverage_floor(old_entry, symbol, full=full)
            log(f"{symbol}: fetching {code} {fetch_start} -> {fetch_end}")
            try:
                sym_report = _process_symbol(
                    root,
                    raw_dir,
                    symbol,
                    code,
                    factor,
                    fetch_start,
                    fetch_end,
                    full=full,
                    tf_rebuild=tf_rebuild,
                    coverage_floor=coverage_floor,
                    fetcher=fetch_fn,
                    staged=staged,
                )
            except Exception as exc:  # noqa: BLE001 - collect per-symbol failures
                logger.exception("Dukascopy update failed for %s", symbol)
                sym_report = SymbolReport(
                    symbol=symbol, dukascopy_symbol=code, status="failed", error=str(exc)
                )
        report.symbols.append(sym_report)

        if sym_report.status == "updated":
            updated_entries[symbol] = (
                sym_report.file_entries,
                sym_report.missing_days,
            )
        elif sym_report.status in {"failed", "incomplete"}:
            failures.append(f"{symbol}: {sym_report.error or sym_report.status}")

    if failures:
        for tmp, _final in staged:
            tmp.unlink(missing_ok=True)
        raise IncompleteFetchError("; ".join(failures))

    new_manifest = _rebuild_manifest(manifest, updated_entries)

    # Atomic publish: rename every staged temp into place, manifest last.
    for tmp, final in staged:
        os.replace(tmp, final)
        report.published.append(str(final.relative_to(root)))
    _write_manifest(root, new_manifest)
    report.manifest_written = True
    report.published.append(MANIFEST_NAME)
    return report


def verify_manifest(root: str | Path) -> list[str]:
    """Check manifest ``rows``/``first``/``last`` against parquet metadata.

    Returns a list of human-readable mismatches (empty when consistent).
    Reads only parquet footers — cheap enough to run after every publish.
    """
    root = Path(root)
    manifest = _read_manifest(root)
    problems: list[str] = []
    for symbol, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        for fname, info in (entry.get("files") or {}).items():
            path = root / symbol / fname
            if not path.exists():
                problems.append(f"{symbol}/{fname}: missing file")
                continue
            footer = _parquet_footer(path)
            if footer is None:
                problems.append(f"{symbol}/{fname}: unreadable")
                continue
            if footer.get("rows") != info.get("rows"):
                problems.append(
                    f"{symbol}/{fname}: rows {footer.get('rows')} != manifest {info.get('rows')}"
                )
                continue
            if "first" in footer and (
                footer.get("first") != info.get("first")
                or footer.get("last") != info.get("last")
            ):
                problems.append(
                    f"{symbol}/{fname}: span {footer.get('first')}..{footer.get('last')} "
                    f"!= manifest {info.get('first')}..{info.get('last')}"
                )
    return problems
