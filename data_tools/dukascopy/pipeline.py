"""Incremental update of the canonical Dukascopy dataset.

Layout (mirrored to the ``tp-ohlcv`` R2 bucket)::

    <root>/manifest.json
    <root>/<SYM>/<SYM>_<TF>.parquet     TF in M1 M5 M15 H1 H4 D1 W1

Each run reads ``manifest.json``, fetches bi5 day files from each symbol's
last M1 timestamp to the last complete UTC day, merges them into M1 without
duplicate bars at the seam, re-resamples the higher timeframes from the
first affected bin, validates every frame, then publishes staged files
atomically with ``manifest.json`` written last. ``full=True`` rebuilds every
symbol from :data:`DATASET_START`. Nothing is published when any symbol
fails validation or fetch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from data_tools.dukascopy import convert, fetch, validate
from data_tools.dukascopy.convert import CANONICAL_SYMBOLS, INSTRUMENTS, TIMEFRAMES

logger = logging.getLogger(__name__)

#: First day of the published dataset (matches the R2 mirror).
DATASET_START = date(2015, 1, 1)
MANIFEST_NAME = "manifest.json"

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


def _write_parquet_tmp(df: pd.DataFrame, final: Path) -> Path:
    """Write ``df`` to a temp sibling of ``final``; caller renames to publish."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=final.name + ".", suffix=".tmp", dir=str(final.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.reset_index(drop=True).to_parquet(tmp, index=False, compression="zstd")
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
    tail.
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
    if full or start is not None:
        fetch_start = start or DATASET_START
    else:
        entry = manifest.get(symbol) or {}
        last = _m1_last_ts(entry, symbol)
        if last is None:
            fetch_start = DATASET_START
        else:
            # Refetch the whole day containing the last bar: cheap, and covers
            # bars Dukascopy published late for that day.
            fetch_start = last.date()
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


def _process_symbol(
    root: Path,
    raw_dir: Path,
    symbol: str,
    dukascopy_symbol: str,
    factor: int,
    fetch_start: date,
    fetch_end: date,
    *,
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

    bid_new, missing_bid = convert.load_m1_side(
        raw_dir / dukascopy_symbol / "BID", factor, start=fetch_start, end=fetch_end
    )
    ask_new, missing_ask = convert.load_m1_side(
        raw_dir / dukascopy_symbol / "ASK", factor, start=fetch_start, end=fetch_end
    )
    report.missing_days = {"bid": missing_bid, "ask": missing_ask}

    m1_path = sym_dir / f"{symbol}_M1.parquet"
    existing_m1 = _read_parquet(m1_path)
    if existing_m1 is None or existing_m1.empty:
        bid_m1, ask_m1 = bid_new, ask_new
    else:
        bid_stored, ask_stored = convert.split_combined_m1(existing_m1)
        bid_m1 = _merge_side(bid_stored, bid_new)
        ask_m1 = _merge_side(ask_stored, ask_new)

    combined_m1 = convert.combine_sides(bid_m1, ask_m1)
    if combined_m1.empty:
        report.status = "failed"
        report.error = "no M1 bars produced"
        return report
    validate.validate_frame(combined_m1, context=f"{symbol} M1")

    if (
        existing_m1 is not None
        and not existing_m1.empty
        and _fingerprint(combined_m1) == _fingerprint(existing_m1)
    ):
        report.status = "up-to-date"
        return report

    if existing_m1 is not None and not existing_m1.empty:
        report.new_m1_rows = int(
            (combined_m1["timestamp"] > existing_m1["timestamp"].iloc[-1]).sum()
        )

    # Higher timeframes: rebuild from the first bin touched by the fetch
    # window — the bin containing fetch_start 00:00 UTC.
    frames: dict[str, pd.DataFrame] = {"M1": combined_m1}
    existing_frames: dict[str, pd.DataFrame | None] = {"M1": existing_m1}
    start_ts = pd.Timestamp(fetch_start.isoformat(), tz="UTC")
    for tf in TIMEFRAMES[1:]:
        cutoff = convert.bin_floor(tf, start_ts)
        tf_path = sym_dir / f"{symbol}_{tf}.parquet"
        existing_tf = _read_parquet(tf_path)
        existing_frames[tf] = existing_tf
        frames[tf] = _higher_tf_frame(existing_tf, bid_m1, ask_m1, tf, cutoff)
        validate.validate_frame(frames[tf], context=f"{symbol} {tf}")

    # Stage every changed file; nothing is published until all symbols pass.
    for tf, df in frames.items():
        final = sym_dir / f"{symbol}_{tf}.parquet"
        existing = existing_frames[tf]
        if (
            existing is not None
            and not existing.empty
            and _fingerprint(df) == _fingerprint(existing)
        ):
            # Unchanged content still gets a manifest entry below.
            continue
        tmp = _write_parquet_tmp(df, final)
        staged.append((tmp, final))

    report.status = "updated"
    report.file_entries = {
        f"{symbol}_{tf}.parquet": convert.file_entry(df) for tf, df in frames.items()
    }
    return report


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


def update_dataset(
    root: str | Path,
    *,
    symbols: list[str] | None = None,
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

    wanted = symbols if symbols else list(CANONICAL_SYMBOLS)
    report = UpdateReport(root=root)
    staged: list[tuple[Path, Path]] = []
    new_manifest = dict(manifest)
    failures: list[str] = []

    for item in wanted:
        resolved = _resolve_symbol(str(item))
        if resolved is None:
            report.symbols.append(
                SymbolReport(symbol=str(item), status="failed", error="unknown symbol")
            )
            failures.append(f"{item}: unknown symbol")
            continue
        symbol, code, factor = resolved

        window = _symbol_window(
            manifest, symbol, full=full, start=fetch_start_req, end=fetch_end
        )
        if window is None:
            report.symbols.append(
                SymbolReport(symbol=symbol, dukascopy_symbol=code, status="up-to-date")
            )
            continue
        fetch_start, _ = window
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
            entry = dict(new_manifest.get(symbol) or {})
            entry["dukascopy_symbol"] = code
            entry["decimal_factor"] = factor
            entry["files"] = sym_report.file_entries
            _merge_missing_days(entry, sym_report.missing_days)
            new_manifest[symbol] = entry
        elif sym_report.status in {"failed", "incomplete"}:
            failures.append(
                f"{symbol}: {sym_report.error or sym_report.status}"
            )

    if failures:
        for tmp, _final in staged:
            tmp.unlink(missing_ok=True)
        raise IncompleteFetchError("; ".join(failures))

    # Atomic publish: rename every staged temp into place, manifest last.
    for tmp, final in staged:
        os.replace(tmp, final)
        report.published.append(str(final.relative_to(root)))
    manifest_tmp = root / f".{MANIFEST_NAME}.tmp"
    manifest_tmp.write_text(json.dumps(new_manifest, indent=1))
    os.replace(manifest_tmp, root / MANIFEST_NAME)
    report.manifest_written = True
    report.published.append(MANIFEST_NAME)
    return report


def verify_manifest(root: str | Path) -> list[str]:
    """Check manifest ``rows``/``first``/``last`` against parquet metadata.

    Returns a list of human-readable mismatches (empty when consistent).
    Reads only parquet footers — cheap enough to run after every publish.
    """
    import pyarrow.parquet as pq

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
            try:
                pf = pq.ParquetFile(path)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{symbol}/{fname}: unreadable ({exc})")
                continue
            md = pf.metadata
            if md.num_rows != info.get("rows"):
                problems.append(
                    f"{symbol}/{fname}: rows {md.num_rows} != manifest {info.get('rows')}"
                )
                continue
            names = list(pf.schema_arrow.names)
            if "timestamp" not in names:
                problems.append(f"{symbol}/{fname}: no timestamp column")
                continue
            idx = names.index("timestamp")
            stats = md.row_group(0).column(idx).statistics
            if stats is None or not getattr(stats, "has_min_max", False):
                continue
            first = str(pd.to_datetime(stats.min, utc=True))
            last = str(pd.to_datetime(stats.max, utc=True))
            if first != info.get("first") or last != info.get("last"):
                problems.append(
                    f"{symbol}/{fname}: span {first}..{last} != manifest "
                    f"{info.get('first')}..{info.get('last')}"
                )
    return problems
