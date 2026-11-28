"""The OHLCV store: the single place that knows the on-disk layout (#471).

Layout (``$DATA_DIR/ohlcv``)::

    <source>/manifest.json
    <source>/<SYMBOL>/<SYMBOL>_<TF>.parquet
    <source>/<SYMBOL>/<SYMBOL>_<TF>_<YYYY>_<MM>.parquet   # TICK/S1 segments only

``<TF>`` is the file token (``M1``, ``H1``, ``TICK``, …); the platform keeps
its timeframe strings (``1m``, ``1h``, ``tick``, …) everywhere else, so this
module owns the only mapping between the two spellings. Two-sided sources
store ``bid_*``/``ask_*`` columns in ONE file — there are no ``_bid``/``_ask``
sibling files.

Everything outside this module must go through these functions: nothing else
builds or parses OHLCV paths or walks the ``ohlcv`` tree. Old-layout support
lives only in ``scripts/migrate_ohlcv_layout.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from shared.constants import OHLCV_COLUMNS, VALID_TIMEFRAMES, normalize_timeframe

logger = logging.getLogger(__name__)

__all__ = [
    "DatasetEntry",
    "apply_date_filter",
    "data_path",
    "dataset_bounds",
    "entry_for_path",
    "find_dataset",
    "list_datasets",
    "manifest_path",
    "normalize_ohlcv_frame",
    "parse_date_bound",
    "platform_timeframe",
    "read_frame",
    "read_manifest",
    "register_file",
    "segment_for_path",
    "source_dir",
    "store_root",
    "tf_token",
    "timestamp_bounds",
    "validate_manifest",
    "write_frame",
    "write_manifest",
]

_PLATFORM_TIMEFRAMES = frozenset(VALID_TIMEFRAMES)

#: File-token spellings, derived from the platform timeframe strings so the
#: two can never disagree: ``1m``→``M1``, ``1h``→``H1``, ``1w``→``W1``,
#: ``tick``→``TICK``, ``s1``→``S1``.
_TOKEN_RE = re.compile(r"^(\d+)([mhdw])$")
_TOKEN_UNIT = {"m": "m", "h": "h", "d": "d", "w": "w"}
_FILE_TOKEN_RE = re.compile(r"^([A-Z]+)(\d+)$")

#: ``data_source`` selection ranking preserved from the retired
#: ``contract.select_ohlcv_path``: asset-class plausibility first, then this
#: source priority, then name.
_SOURCE_PRIORITY = {
    "huggingface": 0,
    "dukascopy": 1,
    "yfinance": 2,
    "alpaca": 3,
}
_METAL_SYMBOLS = {"XAUUSD", "XAGUSD", "XAUEUR", "XAGEUR", "XAUGBP", "XAGGBP"}

#: Per-source asset-class fallback for manifests written before
#: ``asset_class`` was required (the R2 Dukascopy manifest). Anything not
#: listed resolves to ``""``, which makes slippage fail closed — the same
#: outcome the old path-derived lookup produced for unclassifiable paths.
_DUKASCOPY_ASSET_CLASSES = {
    "forex": {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD"},
    "metals": {"XAUUSD", "XAGUSD"},
    "indices": {"NAS100", "SP500", "US30", "DAX", "EURO50", "FTSE100"},
    "energy": {"BRENT", "CRUDE"},
}
_DEFAULT_ASSET_CLASS_BY_SOURCE: dict[str, dict[str, str]] = {
    "dukascopy": {
        symbol: asset_class
        for asset_class, symbols in _DUKASCOPY_ASSET_CLASSES.items()
        for symbol in symbols
    }
}

_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class DatasetEntry:
    """One stored dataset (symbol × timeframe) resolved through the manifest."""

    source: str
    symbol: str
    timeframe: str  # platform spelling ("1h"), never the file token
    asset_class: str
    region: str
    source_symbol: str
    path: Path
    rows: int
    first: str
    last: str
    segments: tuple[Path, ...] = ()


def _platform_timeframe(timeframe: str) -> str:
    normalized = normalize_timeframe(str(timeframe or "").strip())
    if normalized not in _PLATFORM_TIMEFRAMES:
        raise ValueError(f"Unknown OHLCV timeframe: {timeframe!r}")
    return normalized


def _safe_component(value: str, *, what: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_COMPONENT_RE.fullmatch(text) or ".." in text:
        raise ValueError(f"Invalid OHLCV {what}: {value!r}")
    return text


def _source_name(source: str) -> str:
    return _safe_component(source, what="source").lower()


def _symbol_name(symbol: str) -> str:
    return _safe_component(symbol, what="symbol")


def store_root(data_root: str | Path) -> Path:
    """``<data_root>/ohlcv`` — the single place that knows the root name."""
    return Path(data_root) / "ohlcv"


def source_dir(data_root: str | Path, source: str) -> Path:
    return store_root(data_root) / _source_name(source)


def manifest_path(data_root: str | Path, source: str) -> Path:
    return source_dir(data_root, source) / "manifest.json"


def tf_token(timeframe: str) -> str:
    """Platform timeframe string → file token (``1h`` → ``H1``)."""
    normalized = _platform_timeframe(timeframe)
    if normalized == "tick":
        return "TICK"
    if normalized == "s1":
        return "S1"
    match = _TOKEN_RE.fullmatch(normalized)
    if match is None:  # pragma: no cover - VALID_TIMEFRAMES guards this
        raise ValueError(f"Unknown OHLCV timeframe: {timeframe!r}")
    return f"{match.group(2).upper()}{match.group(1)}"


def platform_timeframe(token: str) -> str:
    """File token → platform timeframe string (``H1`` → ``1h``)."""
    text = str(token or "").strip().upper()
    if text == "TICK":
        return "tick"
    if text == "S1":
        return "s1"
    match = _FILE_TOKEN_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"Unknown OHLCV file token: {token!r}")
    unit, count = match.group(1).lower(), match.group(2)
    if unit not in _TOKEN_UNIT:
        raise ValueError(f"Unknown OHLCV file token: {token!r}")
    candidate = f"{count}{unit}"
    if candidate not in _PLATFORM_TIMEFRAMES:
        raise ValueError(f"Unknown OHLCV file token: {token!r}")
    return candidate


def data_path(
    data_root: str | Path,
    source: str,
    symbol: str,
    timeframe: str,
    *,
    segment: str = "",
) -> Path:
    """``<root>/<source>/<SYMBOL>/<SYMBOL>_<TF>[_{segment}].parquet``."""
    sym = _symbol_name(symbol)
    name = f"{sym}_{tf_token(timeframe)}"
    if str(segment or "").strip():
        name += f"_{_safe_component(segment, what='segment')}"
    return source_dir(data_root, source) / sym / f"{name}.parquet"


def parse_dataset_path(
    data_root: str | Path, path: str | Path
) -> tuple[str, str, str] | None:
    """``(source, symbol, filename)`` for a path inside the store, or None.

    The only place besides :func:`entry_for_path` that knows the store's
    three-component ``<source>/<SYMBOL>/<file>`` shape; callers must not
    re-derive it from path parts themselves.
    """
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(store_root(data_root).resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def read_manifest(data_root: str | Path, source: str) -> dict[str, Any]:
    """The source's ``manifest.json`` as a dict; ``{}`` when absent/corrupt."""
    path = manifest_path(data_root, source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Unreadable OHLCV manifest %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def write_manifest(
    data_root: str | Path, source: str, manifest: Mapping[str, Any]
) -> Path:
    """Atomically replace ``<source>/manifest.json`` (temp file + replace)."""
    path = manifest_path(data_root, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def _fallback_asset_class(source: str, symbol: str) -> str:
    table = _DEFAULT_ASSET_CLASS_BY_SOURCE.get(source, {})
    return table.get(str(symbol).strip().upper(), "")


def _entry_asset_class(meta: Mapping[str, Any], source: str, symbol: str) -> str:
    value = str(meta.get("asset_class") or "").strip()
    if value:
        return value
    return _fallback_asset_class(source, symbol)


def _entry_source_symbol(meta: Mapping[str, Any], source: str, symbol: str) -> str:
    value = str(meta.get("source_symbol") or "").strip()
    if value:
        return value
    source_key = str(meta.get(f"{source}_symbol") or "").strip()
    return source_key or symbol


def _parse_dataset_filename(symbol: str, filename: str) -> tuple[str, str] | None:
    """``{SYMBOL}_{TF}.parquet`` / ``{SYMBOL}_{TF}_{seg}.parquet`` → (token, seg).

    The symbol is taken from the manifest key, so symbols containing
    underscores or token-like suffixes cannot be mis-split: the remainder
    after ``{symbol}_`` must start with a known file token.
    """
    if not filename.endswith(".parquet"):
        return None
    stem = filename[: -len(".parquet")]
    prefix = f"{symbol}_"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix) :]
    token, _, segment = rest.partition("_")
    if token not in _ALL_FILE_TOKENS:
        return None
    return token, segment


_ALL_FILE_TOKENS = frozenset(tf_token(tf) for tf in _PLATFORM_TIMEFRAMES)


def _manifest_symbol_key(manifest: Mapping[str, Any], symbol: str) -> str | None:
    """The manifest's own key for ``symbol`` (case-insensitive fallback)."""
    if symbol in manifest:
        return symbol
    wanted = symbol.upper()
    for key in manifest:
        if str(key).upper() == wanted:
            return str(key)
    return None


def _file_stat(meta: Any) -> tuple[int, str, str]:
    if not isinstance(meta, Mapping):
        return 0, "", ""
    try:
        rows = int(meta.get("rows") or 0)
    except (TypeError, ValueError):
        rows = 0
    return rows, str(meta.get("first") or ""), str(meta.get("last") or "")


def _dataset_entries_for_source(
    data_root: str | Path, source: str
) -> list[DatasetEntry]:
    """All manifest-declared datasets of one source (manifest-driven)."""
    src = _source_name(source)
    manifest = read_manifest(data_root, src)
    entries: list[DatasetEntry] = []
    for manifest_symbol, meta in manifest.items():
        if not isinstance(meta, Mapping):
            continue
        symbol = str(manifest_symbol)
        files = meta.get("files")
        if not isinstance(files, Mapping):
            continue
        # Group the declared files by platform timeframe. File names carry
        # the token; anything unparseable is skipped here and reported by
        # validate_manifest.
        grouped: dict[str, list[str]] = {}
        for filename in files:
            parsed = _parse_dataset_filename(symbol, str(filename))
            if parsed is None:
                continue
            token, _segment = parsed
            try:
                timeframe = platform_timeframe(token)
            except ValueError:
                continue
            grouped.setdefault(timeframe, []).append(str(filename))
        for timeframe, names in grouped.items():
            paths: list[Path] = []
            rows = 0
            firsts: list[pd.Timestamp] = []
            lasts: list[pd.Timestamp] = []
            canonical_name = f"{symbol}_{tf_token(timeframe)}.parquet"
            preferred: Path | None = None
            for filename in names:
                file_rows, file_first, file_last = _file_stat(files[filename])
                rows += file_rows
                path = source_dir(data_root, src) / symbol / filename
                paths.append(path)
                if filename == canonical_name:
                    preferred = path
                for raw, sink in ((file_first, firsts), (file_last, lasts)):
                    if raw:
                        try:
                            sink.append(pd.Timestamp(raw))
                        except (TypeError, ValueError):
                            continue
            paths.sort(key=lambda path: path.name)
            first = min(firsts).isoformat() if firsts else ""
            last = max(lasts).isoformat() if lasts else ""
            entries.append(
                DatasetEntry(
                    source=src,
                    symbol=symbol,
                    timeframe=timeframe,
                    asset_class=_entry_asset_class(meta, src, symbol),
                    region=str(meta.get("region") or "").strip(),
                    source_symbol=_entry_source_symbol(meta, src, symbol),
                    path=preferred if preferred is not None else paths[0],
                    rows=rows,
                    first=first,
                    last=last,
                    segments=tuple(paths),
                )
            )
    return entries


def _unmanifested_file_count(data_root: str | Path, source: str) -> int:
    """Parquet files on disk that no manifest entry declares (for the warning)."""
    src_dir = source_dir(data_root, source)
    if not src_dir.is_dir():
        return 0
    manifest = read_manifest(data_root, source)
    declared: set[str] = set()
    for meta in manifest.values():
        if isinstance(meta, Mapping) and isinstance(meta.get("files"), Mapping):
            for filename in meta["files"]:
                declared.add(str(filename))
    count = 0
    for symbol_dir in src_dir.iterdir():
        if not symbol_dir.is_dir():
            continue
        for parquet in symbol_dir.glob("*.parquet"):
            if parquet.name not in declared:
                count += 1
    return count


def _warn_unmanifested(data_root: str | Path, source: str) -> None:
    count = _unmanifested_file_count(data_root, source)
    if count:
        logger.warning(
            "OHLCV source %s: %d parquet file(s) on disk are not in "
            "manifest.json and are ignored",
            source,
            count,
        )


def list_datasets(
    data_root: str | Path,
    *,
    source: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> list[DatasetEntry]:
    """Manifest-driven dataset listing for the whole store or a subset.

    Files on disk that the manifest does not declare are ignored (one logged
    warning per source). ``symbol``/``timeframe`` filters compare case-
    insensitively on the platform timeframe spelling.
    """
    root = store_root(data_root)
    if str(source or "").strip():
        sources = [_source_name(source)]
    elif root.is_dir():
        sources = sorted(
            child.name for child in root.iterdir() if child.is_dir()
        )
    else:
        sources = []

    wanted_symbol = str(symbol or "").strip().upper()
    wanted_timeframe = ""
    if str(timeframe or "").strip():
        # Unknown spellings simply match nothing (filter semantics).
        wanted_timeframe = normalize_timeframe(str(timeframe).strip())

    entries: list[DatasetEntry] = []
    for src in sources:
        _warn_unmanifested(data_root, src)
        for entry in _dataset_entries_for_source(data_root, src):
            if wanted_symbol and entry.symbol.upper() != wanted_symbol:
                continue
            if wanted_timeframe and entry.timeframe != wanted_timeframe:
                continue
            entries.append(entry)
    return entries


def _selection_rank(entry: DatasetEntry, symbol: str) -> tuple[Any, ...]:
    """The ``select_ohlcv_path`` ranking, now fed by manifest metadata."""
    # The retired path-based lookup compared lowercased components; manifest
    # values keep their case, so the plausibility checks lowercase here.
    asset_class = entry.asset_class.lower()
    if symbol in _METAL_SYMBOLS:
        asset_rank = 0 if asset_class == "metals" else 1
    elif len(symbol) == 6 and symbol.isalpha():
        asset_rank = 0 if asset_class == "forex" else 1
    else:
        asset_rank = 0 if asset_class in {"equity", "equities", "stocks"} else 1
    source_rank = _SOURCE_PRIORITY.get(entry.source, 4)
    return (
        asset_rank,
        source_rank,
        entry.source,
        entry.symbol,
        entry.timeframe,
        entry.path.as_posix(),
    )


def find_dataset(
    data_root: str | Path,
    symbol: str,
    timeframe: str,
    *,
    data_source: str = "",
) -> DatasetEntry | None:
    """The deterministically chosen dataset for ``symbol``/``timeframe``.

    With ``data_source`` only entries whose source, asset class or region
    matches (the historical ``select_ohlcv_path`` filter, quirks included)
    are considered; otherwise every source's copy competes under the
    preserved asset-class/source ranking with ties broken by name.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if normalized_timeframe not in _PLATFORM_TIMEFRAMES:
        return None

    candidates = list_datasets(
        data_root, symbol=normalized_symbol, timeframe=normalized_timeframe
    )
    requested = str(data_source or "").strip().lower()
    if requested:
        candidates = [
            entry
            for entry in candidates
            if requested
            in {
                str(value).lower()
                for value in (entry.source, entry.asset_class, entry.region)
                if value
            }
        ]
    if not candidates:
        return None
    return min(candidates, key=lambda entry: _selection_rank(entry, normalized_symbol))


def entry_for_path(data_root: str | Path, path: str | Path) -> DatasetEntry | None:
    """The manifest dataset a stored parquet path belongs to, or None."""
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(store_root(data_root).resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) != 3:
        return None
    source, symbol, filename = parts
    manifest = read_manifest(data_root, source)
    key = _manifest_symbol_key(manifest, symbol)
    if key is None:
        return None
    parsed = _parse_dataset_filename(key, filename)
    if parsed is None:
        return None
    token, _segment = parsed
    try:
        timeframe = platform_timeframe(token)
    except ValueError:
        return None
    for entry in _dataset_entries_for_source(data_root, source):
        if entry.symbol == key and entry.timeframe == timeframe:
            if any(segment.resolve() == resolved for segment in entry.segments):
                return entry
    return None


def segment_for_path(data_root: str | Path, path: str | Path) -> str:
    """The manifest segment label for a stored parquet path (``""`` if none)."""
    entry = entry_for_path(data_root, path)
    if entry is None:
        return ""
    prefix = f"{entry.symbol}_{tf_token(entry.timeframe)}"
    stem = Path(path).stem
    if stem == prefix:
        return ""
    if stem.startswith(f"{prefix}_"):
        return stem[len(prefix) + 1 :]
    return ""


def _read_segment_frames(paths: list[Path]) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for path in paths:
        try:
            frame = pd.read_parquet(path)
        except FileNotFoundError:
            # A missing file reads as "no data" only when it is the whole
            # dataset; among sibling segments it would silently shrink the
            # frame, so it fails loudly like any other read error.
            if len(paths) > 1:
                raise
            logger.warning("OHLCV parquet missing: %s", path)
            continue
        frame.columns = [str(column).lower() for column in frame.columns]
        frames.append(frame)
    return frames


# ─── Date filter convention (F9) ────────────────────────────────────────────


def parse_date_bound(value: str, *, is_end: bool) -> pd.Timestamp:
    """Parse a ``start_date``/``end_date`` value into an inclusive filter bound.

    Decision Record (Cleanup 16-A, 2026-08-31): a date-only end boundary is
    inclusive of the full day. Concretely:

    - ``"2024-01-15"`` as a start bound → ``2024-01-15T00:00:00Z``;
    - ``"2024-01-15"`` as an end bound → ``2024-01-15T23:59:59.999999Z``;
    - a value carrying a time component (``"2024-01-15T14:30:00Z"``) is used
      as-is; naive timestamps are interpreted as UTC.

    Raises:
        ValueError: If ``value`` is empty or not parseable as a timestamp.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Date filter value cannot be empty")

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid date filter value: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if is_end and "T" not in raw and ":" not in raw:
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return pd.Timestamp(parsed)


def apply_date_filter(
    frame: pd.DataFrame,
    *,
    start: str | None,
    end: str | None,
    timestamp_column: str = "timestamp",
) -> pd.DataFrame:
    """Filter ``frame`` to the inclusive [start, end] window (see F9 above).

    Empty/None bounds are treated as unbounded on that side. The frame must
    carry a UTC-convertible ``timestamp`` column.

    This must be the ONLY date filter applied to the frame. A caller that
    pre-filters with a bare-date string comparison (``df[df["timestamp"] <=
    end_date]``) truncates at midnight and reintroduces the F9 end-day
    truncation bug before this helper ever sees the data.
    """
    if start:
        frame = frame[
            frame[timestamp_column] >= parse_date_bound(start, is_end=False)
        ]
    if end:
        frame = frame[
            frame[timestamp_column] <= parse_date_bound(end, is_end=True)
        ]
    return frame


# ─── OHLCV column normalization (bid_/ask_-prefixed feeds) ─────────────────


def normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill canonical ``open/high/low/close/volume`` from ``bid_*`` columns.

    In-memory counterpart of
    ``data_tools.download_grid_market_data.normalize_ohlcv_columns``
    (which rewrites parquet files and drops non-OHLCV columns -- unsuitable
    here because ``ask_*``/spread columns must survive for ask-side stop
    monitoring). Dukascopy index/energy feeds store ``bid_open/bid_high/...``
    (plus ``ask_*``) instead of, or alongside all-NaN, plain OHLC. Missing
    values are filled per-value from the ``bid_`` counterpart (the bid side
    is the conservative, MT5-chart equivalent series); existing non-null
    canonical values always win. All other columns pass through untouched.

    C-1: this helper is NON-MUTATING — it works on a copy and returns it.
    Callers must use the return value; the input frame is never modified
    in place.
    """
    frame = frame.copy()
    for canonical in ("open", "high", "low", "close", "volume"):
        bid_col = f"bid_{canonical}"
        if bid_col not in frame.columns:
            continue
        if canonical not in frame.columns:
            frame[canonical] = frame[bid_col].to_numpy()
        else:
            frame[canonical] = frame[canonical].fillna(frame[bid_col])
    return frame


def read_frame(
    entry_or_path: DatasetEntry | str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Load a dataset's frame: lowercase cols, UTC-sorted, bid-side canonical.

    ``open/high/low/close/volume`` are always present in the output — for
    two-sided files without plain columns they are the ``bid_*`` side (via
    :func:`normalize_ohlcv_frame`); ``bid_*``/``ask_*`` columns are kept.
    Segments are concatenated in order. ``start``/``end`` follow the shared
    date-filter convention (a date-only end bound is inclusive of the day).
    """
    if isinstance(entry_or_path, DatasetEntry):
        paths = list(entry_or_path.segments) or [entry_or_path.path]
    else:
        paths = [Path(entry_or_path)]

    frames = _read_segment_frames(paths)
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True, sort=False)
    frame = normalize_ohlcv_frame(frame)
    if "timestamp" not in frame.columns:
        return pd.DataFrame()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    frame = apply_date_filter(frame, start=start, end=end)
    for column in OHLCV_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    if columns is not None:
        wanted = [str(column).lower() for column in columns]
        present = [column for column in wanted if column in frame.columns]
        frame = frame.loc[:, present]
    return frame.reset_index(drop=True)


def timestamp_bounds(path: str | Path) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(min, max) timestamp of a parquet from row-group statistics.

    No full read: returns None when the file or its timestamp statistics are
    unavailable, so callers can fall back to a real read.
    """
    import pyarrow.parquet as pq

    try:
        parquet = pq.ParquetFile(str(path))
    except (OSError, ValueError):
        return None
    try:
        names = [str(name).lower() for name in parquet.schema_arrow.names]
        column_index = names.index("timestamp")
    except (ValueError, AttributeError):
        return None
    lows: list[pd.Timestamp] = []
    highs: list[pd.Timestamp] = []
    try:
        metadata = parquet.metadata
        for row_group in range(metadata.num_row_groups):
            stats = metadata.row_group(row_group).column(column_index).statistics
            if stats is None or not stats.has_min_max:
                return None
            lows.append(_utc_timestamp(stats.min))
            highs.append(_utc_timestamp(stats.max))
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    if not lows:
        return None
    return min(lows), max(highs)


def dataset_bounds(entry: DatasetEntry) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(first, last) coverage of a dataset as UTC timestamps.

    The manifest's ``first``/``last`` win; when either is missing or
    unparseable the row-group statistics of the segment files are used for
    both bounds. None when no bound can be established.
    """
    parsed: list[pd.Timestamp | None] = []
    for raw in (entry.first, entry.last):
        timestamp: pd.Timestamp | None = None
        if str(raw or "").strip():
            try:
                candidate = pd.Timestamp(raw)
                timestamp = (
                    candidate.tz_localize("UTC")
                    if candidate.tzinfo is None
                    else candidate.tz_convert("UTC")
                )
            except (ValueError, TypeError, OverflowError):
                timestamp = None
        parsed.append(timestamp)
    if parsed[0] is not None and parsed[1] is not None:
        return parsed[0], parsed[1]

    lows: list[pd.Timestamp] = []
    highs: list[pd.Timestamp] = []
    for segment in entry.segments or (entry.path,):
        bounds = timestamp_bounds(segment)
        if bounds is None:
            return None
        lows.append(bounds[0])
        highs.append(bounds[1])
    if not lows:
        return None
    return min(lows), max(highs)


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp


def _parquet_row_count(path: Path) -> int | None:
    import pyarrow.parquet as pq

    try:
        return int(pq.ParquetFile(str(path)).metadata.num_rows)
    except (OSError, ValueError, AttributeError):
        return None


def _validate_symbol_meta(
    data_root: str | Path, source: str, symbol: str, meta: Any
) -> list[str]:
    problems: list[str] = []
    label = f"{source}/{symbol}"
    if not isinstance(meta, Mapping):
        return [f"{label}: manifest entry is not an object"]
    files = meta.get("files")
    if not isinstance(files, Mapping) or not files:
        problems.append(f"{label}: missing required 'files' mapping")
        files = {}
    asset_class = _entry_asset_class(meta, source, symbol)
    if not asset_class:
        problems.append(f"{label}: no asset_class (manifest or source default)")
    for filename, file_meta in files.items():
        parsed = _parse_dataset_filename(symbol, str(filename))
        if parsed is None:
            problems.append(f"{label}: unparseable file name {filename!r}")
            continue
        token, _segment = parsed
        try:
            platform_timeframe(token)
        except ValueError:
            problems.append(f"{label}: unknown TF token in {filename!r}")
        path = source_dir(data_root, source) / symbol / str(filename)
        if not path.is_file():
            problems.append(f"{label}: declared file missing on disk: {filename}")
            continue
        expected_rows, expected_first, expected_last = _file_stat(file_meta)
        actual_rows = _parquet_row_count(path)
        if actual_rows is not None and expected_rows and actual_rows != expected_rows:
            problems.append(
                f"{label}: {filename} rows mismatch "
                f"(manifest {expected_rows}, file {actual_rows})"
            )
        bounds = timestamp_bounds(path)
        if bounds is not None:
            first, last = bounds
            if expected_first:
                try:
                    if pd.Timestamp(expected_first) != first:
                        problems.append(
                            f"{label}: {filename} first mismatch "
                            f"(manifest {expected_first}, file {first})"
                        )
                except (TypeError, ValueError):
                    problems.append(f"{label}: {filename} has unparseable 'first'")
            if expected_last:
                try:
                    if pd.Timestamp(expected_last) != last:
                        problems.append(
                            f"{label}: {filename} last mismatch "
                            f"(manifest {expected_last}, file {last})"
                        )
                except (TypeError, ValueError):
                    problems.append(f"{label}: {filename} has unparseable 'last'")
    return problems


def validate_manifest(data_root: str | Path, source: str) -> list[str]:
    """Manifest problems for one source: missing files, stat mismatches, bad names."""
    src = _source_name(source)
    path = manifest_path(data_root, src)
    if not path.is_file():
        return [f"{src}: manifest.json missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{src}: manifest.json unreadable: {exc}"]
    if not isinstance(payload, dict):
        return [f"{src}: manifest.json is not a JSON object"]
    problems: list[str] = []
    for symbol, meta in payload.items():
        problems.extend(_validate_symbol_meta(data_root, src, str(symbol), meta))
    return problems


def _validate_write_frame(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """Lowercase columns and enforce the write contract; returns (frame, ts)."""
    frame = frame.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if "timestamp" not in frame.columns:
        raise ValueError("write_frame requires a 'timestamp' column")
    has_plain = all(column in frame.columns for column in ("open", "high", "low", "close"))
    has_bid = all(
        f"bid_{column}" in frame.columns for column in ("open", "high", "low", "close")
    )
    if not (has_plain or has_bid):
        raise ValueError(
            "write_frame requires open/high/low/close columns, plain or bid_-prefixed"
        )
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        raise ValueError("write_frame timestamps must all be valid")
    if not timestamps.is_unique:
        raise ValueError("write_frame timestamps must be unique")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("write_frame timestamps must be sorted ascending")
    return frame, timestamps


def write_frame(
    data_root: str | Path,
    source: str,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    *,
    asset_class: str,
    region: str = "",
    source_symbol: str = "",
    segment: str = "",
    extra_meta: Mapping[str, Any] | None = None,
) -> Path:
    """Write one dataset file atomically and update the source manifest.

    Validates the column contract (``timestamp`` plus OHLC — plain or
    ``bid_``-prefixed — always; ``volume``/``ask_*`` optional) and requires
    sorted, unique, UTC timestamps. ``rows``/``first``/``last`` are recomputed
    from the frame, never trusted from the caller. The manifest is written
    after the data file, also atomically.
    """
    src = _source_name(source)
    sym = _symbol_name(symbol)
    path = data_path(data_root, src, sym, timeframe, segment=segment)
    checked, timestamps = _validate_write_frame(frame)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        checked.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    manifest = read_manifest(data_root, src)
    meta = manifest.get(sym)
    if not isinstance(meta, dict):
        meta = {}
        manifest[sym] = meta
    files = meta.get("files")
    if not isinstance(files, dict):
        files = {}
        meta["files"] = files
    files[path.name] = {
        "rows": len(checked),
        "first": str(timestamps.min()),
        "last": str(timestamps.max()),
    }
    meta["asset_class"] = str(asset_class or "").strip()
    meta["region"] = str(region or "").strip()
    meta["source_symbol"] = str(source_symbol or "").strip() or sym
    if extra_meta:
        for key, value in extra_meta.items():
            meta[str(key)] = value
    write_manifest(data_root, src, manifest)
    return path


def register_file(
    data_root: str | Path,
    source: str,
    symbol: str,
    timeframe: str,
    path: str | Path,
    *,
    asset_class: str,
    region: str = "",
    source_symbol: str = "",
    segment: str = "",
    extra_meta: Mapping[str, Any] | None = None,
) -> Path:
    """Declare an already-written parquet in the source manifest.

    ``rows``/``first``/``last`` are recomputed from the file's parquet
    metadata and timestamp statistics — never trusted from the caller. Use
    this for byte-identical copies (aliases) where re-writing the frame
    through :func:`write_frame` would be wasted I/O; ``write_frame`` remains
    the writer for frames held in memory.
    """
    src = _source_name(source)
    sym = _symbol_name(symbol)
    target = Path(path)
    expected = data_path(data_root, src, sym, timeframe, segment=segment)
    if target.resolve() != expected.resolve():
        raise ValueError(f"register_file path {target} is not {expected}")
    if not target.is_file():
        raise ValueError(f"register_file target does not exist: {target}")

    rows = _parquet_row_count(target)
    bounds = timestamp_bounds(target)
    manifest = read_manifest(data_root, src)
    meta = manifest.get(sym)
    if not isinstance(meta, dict):
        meta = {}
        manifest[sym] = meta
    files = meta.get("files")
    if not isinstance(files, dict):
        files = {}
        meta["files"] = files
    files[expected.name] = {
        "rows": rows or 0,
        "first": bounds[0].isoformat() if bounds else "",
        "last": bounds[1].isoformat() if bounds else "",
    }
    meta["asset_class"] = str(asset_class or "").strip()
    meta["region"] = str(region or "").strip()
    meta["source_symbol"] = str(source_symbol or "").strip() or sym
    if extra_meta:
        for key, value in extra_meta.items():
            meta[str(key)] = value
    write_manifest(data_root, src, manifest)
    return target
