"""
Scan ~/data/ and produce catalog.json — a machine-readable manifest of all
available datasets.

Supports source_meta, validation status, and storage mode fields (Phase 3).

Usage:
    python -m data_tools.catalog_builder              # print to stdout
    python -m data_tools.catalog_builder --output ~/data/catalog.json
    python -m data_tools.catalog_builder --validate   # include validation results
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, normalize_timeframe

CATALOG_FILE = "catalog.json"

# Path to persisted source metadata (written by downloaders)
SOURCE_META_FILE = "source_meta.json"


def write_json_atomically(path: Path, payload: str) -> None:
    """Write serialized JSON to ``path`` atomically.

    Readers never observe a truncated catalog, and a crash mid-write leaves
    the previous file intact.
    """
    write_text_atomically(Path(path), payload)


def write_catalog_json(path: Path, catalog: dict) -> None:
    """Serialize ``catalog`` and persist it atomically to ``path``."""
    write_json_atomically(path, json.dumps(catalog, indent=2, default=str))
_TIMEFRAME_REGEX = r"(?:tick|mn1|(?:\d+)?(?:s|sec|m|min|h|d|w|mo)|[smhdw]\d+)"
TIMEFRAME_PATTERN = re.compile(rf"^{_TIMEFRAME_REGEX}$", re.IGNORECASE)
_SEGMENT_CHARS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SEGMENTED_TIMEFRAME_PATTERN = re.compile(
    rf"^(?P<timeframe>{_TIMEFRAME_REGEX})(?:[_-].+)$",
    re.IGNORECASE,
)
SYMBOL_PREFIXED_TIMEFRAME_PATTERN = re.compile(
    rf"^(?P<symbol>.+?)[_-](?P<timeframe>{_TIMEFRAME_REGEX})(?:[_-](?P<segment>.+))?$",
    re.IGNORECASE,
)


def _load_source_meta(data_dir: Path) -> dict[str, dict]:
    """Load source metadata index if it exists.

    The file maps dataset keys (e.g. 'dukascopy/forex/XAUUSD/1h') to metadata
    dicts with fields: url, downloaded_at, license, update_frequency.
    """
    meta_path = data_dir / SOURCE_META_FILE
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", meta_path, exc)
    return {}


def _load_catalog_cache(data_dir: Path) -> dict[str, dict]:
    """Load the previous catalog to reuse unchanged file metadata."""
    cache_path = data_dir / CATALOG_FILE
    if not cache_path.exists():
        return {}

    try:
        payload = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", cache_path, exc)
        return {}

    cached_entries = payload.get("ohlcv", []) if isinstance(payload, dict) else []
    cache: dict[str, dict] = {}
    for entry in cached_entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if path:
            cache[str(path)] = entry
        segments = entry.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                segment_path = segment.get("path")
                if not segment_path:
                    continue
                merged_segment_entry = dict(entry)
                merged_segment_entry.update(segment)
                cache[str(segment_path)] = merged_segment_entry
    return cache


def _detect_storage(parquet_path: Path, data_dir: Path) -> str:
    """Determine storage mode: local, cached, or remote."""
    cache_dir = data_dir / "cache"
    if str(parquet_path).startswith(str(cache_dir)):
        return "cached"
    return "local"


def _run_validation(parquet_path: Path, symbol: str, timeframe: str) -> dict | None:
    """Run data validation checks and return summary dict."""
    try:
        from data_tools.validate import validate_parquet

        result = validate_parquet(parquet_path)
        return {
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "status": result.status,
            "gaps": len(result.gaps),
            "duplicates": result.duplicates,
            "warnings": [
                w
                for w in [
                    f"null_values: {result.null_counts}"
                    if any(v > 0 for v in result.null_counts.values())
                    else None,
                    f"ohlc_violations: {result.ohlc_violations}"
                    if result.ohlc_violations > 0
                    else None,
                    f"zero_volume: {result.zero_volume_pct:.1f}%"
                    if result.zero_volume_pct > 5.0
                    else None,
                ]
                if w is not None
            ],
        }
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Validation skipped for %s: %s", parquet_path, exc)
        return None


def _looks_like_timeframe(value: str) -> bool:
    return bool(TIMEFRAME_PATTERN.match(value.strip()))


def _extract_timeframe_and_segment(stem: str) -> tuple[str | None, str | None]:
    normalized = str(stem or "").strip()
    if not normalized:
        return None, None
    if _looks_like_timeframe(normalized):
        return normalize_timeframe(normalized), None

    segmented_match = SEGMENTED_TIMEFRAME_PATTERN.match(normalized)
    if segmented_match is None:
        return None, None

    timeframe = normalize_timeframe(str(segmented_match.group("timeframe") or ""))
    if timeframe == "tick":
        # Tick datasets are commonly segmented by month (e.g. tick_2024_01).
        # Treat these as timeframe-only filenames when the segment is a plain
        # partition token to avoid misclassifying the segment as a symbol.
        segment_candidate = normalized[len(timeframe) + 1 :].strip()
        if segment_candidate and _SEGMENT_CHARS_PATTERN.match(segment_candidate):
            return timeframe, segment_candidate

    separator_index = len(timeframe)
    segment = (
        normalized[separator_index + 1 :].strip()
        if len(normalized) > separator_index + 1
        else ""
    )
    return timeframe, (segment or None)


def _extract_symbol_timeframe_and_segment(
    stem: str,
) -> tuple[str | None, str | None, str | None]:
    normalized = str(stem or "").strip()
    if not normalized:
        return None, None, None

    timeframe, segment = _extract_timeframe_and_segment(normalized)
    if timeframe is not None:
        return None, timeframe, segment

    prefixed_match = SYMBOL_PREFIXED_TIMEFRAME_PATTERN.match(normalized)
    if prefixed_match is None:
        return None, None, None

    symbol = str(prefixed_match.group("symbol") or "").strip()
    timeframe_value = normalize_timeframe(str(prefixed_match.group("timeframe") or "").strip())
    segment_value = str(prefixed_match.group("segment") or "").strip()
    if not symbol or not timeframe_value:
        return None, None, None
    return symbol, timeframe_value, (segment_value or None)


def _format_catalog_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = pd.to_datetime(value, utc=True)
    except (ValueError, TypeError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date().isoformat()


def _catalog_timestamp_to_epoch(value: object) -> float | None:
    if value is None:
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return float(timestamp.timestamp())


def _mtime_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _should_reuse_cached_summary(cached_entry: dict | None, file_mtime: float) -> bool:
    if not isinstance(cached_entry, dict):
        return False

    cached_last_updated = _catalog_timestamp_to_epoch(cached_entry.get("last_updated"))
    if cached_last_updated is not None:
        return file_mtime <= (cached_last_updated + 1e-6)

    # Backward-compatible fallback for older cache payloads without last_updated.
    cached_mtime_ns = cached_entry.get("mtime_ns")
    try:
        return float(file_mtime) <= float(cached_mtime_ns) / 1_000_000_000
    except (TypeError, ValueError):
        return False


def _looks_like_bare_symbol(value: str) -> bool:
    """Whether a filename stem is a plain instrument symbol (no timeframe info).

    Used for flat export layouts where the stem is the only symbol hint. Stems
    containing separators or timeframe-like spellings cannot be trusted as a
    bare symbol and must not be catalogued as a "1d" dataset.
    """
    normalized = str(value or "").strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9]+", normalized):
        return False
    if not any(character.isalpha() for character in normalized):
        return False
    if _looks_like_timeframe(normalized):
        return False
    return True


def parse_ohlcv_path(parquet_path: Path, ohlcv_root: Path) -> dict | None:
    """Parse an OHLCV parquet path into source/asset_class/symbol/timeframe fields."""
    rel = parquet_path.relative_to(ohlcv_root)
    parts = rel.parts
    if len(parts) < 3:
        logger.warning("Unexpected path depth: %s", rel)
        return None

    source = parts[0]
    asset_class = parts[1]
    region: str | None = None
    stem = parquet_path.stem
    stem_symbol, timeframe, segment = _extract_symbol_timeframe_and_segment(stem)

    if len(parts) >= 5:
        region = parts[-3]
        symbol = parts[-2]
        timeframe_value = timeframe
    elif len(parts) == 4:
        parent = parts[-2]
        if timeframe:
            timeframe_value = timeframe
            if stem_symbol and stem_symbol.lower() != parent.lower():
                region = parent
                symbol = stem_symbol
            else:
                symbol = parent
        else:
            # Flat layout {symbol}.parquet: the symbol directory is the region
            # and the file stem is the symbol. The stem must look like a bare
            # symbol (no timeframe-like or compound segments), otherwise the
            # entry is rejected instead of silently catalogued as a "1d" dataset.
            region = parent
            symbol = stem
            timeframe_value = "1d" if _looks_like_bare_symbol(stem) else None
    else:
        if timeframe and stem_symbol:
            symbol = stem_symbol
            timeframe_value = timeframe
        elif timeframe:
            logger.warning("Could not infer symbol from timeframe-only filename: %s", rel)
            return None
        else:
            symbol = stem
            timeframe_value = "1d" if _looks_like_bare_symbol(stem) else None

    if not timeframe_value:
        logger.warning(
            "Skipping parquet with unparseable timeframe (not defaulted to 1d): %s", rel
        )
        return None

    entry = {
        "source": source,
        "asset_class": asset_class,
        "symbol": symbol,
        "timeframe": timeframe_value,
        "path": str(parquet_path),
    }
    if region:
        entry["region"] = region
    if segment:
        entry["segment"] = segment
    return entry


def _locate_ohlcv_root_from_path(parquet_path: Path) -> Path | None:
    for parent in parquet_path.parents:
        if parent.name == "ohlcv":
            return parent
    return None


def _normalize_grouping_entry_from_path(entry: dict) -> dict:
    path_value = str(entry.get("path") or "").strip()
    if not path_value.endswith(".parquet"):
        return entry

    path_obj = Path(path_value)
    symbol_value = str(entry.get("symbol") or "").strip()
    timeframe_value = normalize_timeframe(str(entry.get("timeframe") or "").strip())
    needs_reparse = (
        not symbol_value
        or not timeframe_value
        or symbol_value.lower().startswith("tick_")
        or (timeframe_value == "1d" and path_obj.stem.lower().startswith("tick_"))
    )
    if not needs_reparse:
        return entry

    ohlcv_root = _locate_ohlcv_root_from_path(path_obj)
    if ohlcv_root is None:
        return entry

    reparsed = parse_ohlcv_path(path_obj, ohlcv_root)
    if reparsed is None:
        return entry

    normalized = dict(entry)
    for key in ("source", "asset_class", "symbol", "timeframe", "region", "segment"):
        if key in reparsed:
            normalized[key] = reparsed[key]
        elif key in {"region", "segment"}:
            normalized.pop(key, None)
    return normalized


def _select_timestamp_column(columns: list[str]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in lowered:
            return lowered[candidate]
    return None


def _read_row_group_stat(
    metadata: pq.FileMetaData,
    column_index: int,
    row_group_index: int,
    *,
    boundary: str,
) -> object | None:
    stats = metadata.row_group(row_group_index).column(column_index).statistics
    if stats is None or not getattr(stats, "has_min_max", False):
        return None
    return stats.min if boundary == "min" else stats.max


def read_parquet_summary(parquet_path: Path) -> dict:
    """Read parquet metadata and timestamp bounds without loading the full file."""
    stat = parquet_path.stat()
    parquet_file = pq.ParquetFile(parquet_path)
    metadata = parquet_file.metadata
    columns = list(parquet_file.schema_arrow.names)
    summary = {
        "rows": metadata.num_rows,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "last_updated": _mtime_to_iso(stat.st_mtime),
        "columns": columns,
    }

    timestamp_column = _select_timestamp_column(columns)
    if not timestamp_column or metadata.num_rows == 0 or metadata.num_row_groups == 0:
        return summary

    column_index = columns.index(timestamp_column)
    start_value = _read_row_group_stat(metadata, column_index, 0, boundary="min")
    end_value = _read_row_group_stat(
        metadata,
        column_index,
        metadata.num_row_groups - 1,
        boundary="max",
    )

    if start_value is None:
        first_group = parquet_file.read_row_group(0, columns=[timestamp_column])
        if first_group.num_rows > 0:
            start_value = first_group.column(0)[0].as_py()
    if end_value is None:
        last_group = parquet_file.read_row_group(
            metadata.num_row_groups - 1,
            columns=[timestamp_column],
        )
        if last_group.num_rows > 0:
            end_value = last_group.column(0)[last_group.num_rows - 1].as_py()

    summary["start"] = _format_catalog_date(start_value)
    summary["end"] = _format_catalog_date(end_value)
    return summary


def _merge_catalog_dates(current: str | None, candidate: object, *, latest: bool) -> str | None:
    value = str(candidate or "").strip()
    if not value:
        return current
    if current is None:
        return value
    if latest:
        return value if value > current else current
    return value if value < current else current


def _coerce_nonnegative_int(value: object) -> int:
    try:
        numeric = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(numeric, 0)


def _collect_text_values(target: set[str], value: object) -> None:
    if isinstance(value, list):
        for item in value:
            normalized = str(item or "").strip()
            if normalized:
                target.add(normalized)
        return

    normalized = str(value or "").strip()
    if normalized:
        target.add(normalized)


def _canonical_group_path(paths: set[str], timeframe: str) -> str:
    if not paths:
        return ""
    normalized_timeframe = str(timeframe or "").strip().lower()
    sorted_paths = sorted(paths)
    for path_value in sorted_paths:
        if Path(path_value).stem.lower() == normalized_timeframe:
            return path_value
    return sorted_paths[0]


def _group_ohlcv_entries(entries: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}

    for raw_entry in entries:
        entry = _normalize_grouping_entry_from_path(raw_entry)
        entry["timeframe"] = normalize_timeframe(str(entry.get("timeframe") or ""))
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "")
        key = (symbol, timeframe)

        aggregate = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "sources": set(),
                "asset_classes": set(),
                "regions": set(),
                "paths": set(),
                "rows_total": 0,
                "rows_unknown": False,
                "size_bytes": 0,
                "mtime_ns": None,
                "last_updated": None,
                "start": None,
                "end": None,
                "columns": set(),
                "formats": set(),
                "storages": set(),
                "source_meta": None,
                "validation_seen": False,
                "validation_status": "ok",
                "validation_warnings": set(),
                "validation_gaps": 0,
                "validation_duplicates": 0,
                "validation_last_checked": None,
                "errors": set(),
                "segments": [],
                "segment_paths": set(),
            },
        )

        _collect_text_values(aggregate["sources"], entry.get("source"))
        _collect_text_values(aggregate["sources"], entry.get("sources"))
        _collect_text_values(aggregate["asset_classes"], entry.get("asset_class"))
        _collect_text_values(aggregate["asset_classes"], entry.get("asset_classes"))
        _collect_text_values(aggregate["regions"], entry.get("region"))
        _collect_text_values(aggregate["regions"], entry.get("regions"))

        path_value = str(entry.get("path") or "")
        if path_value:
            aggregate["paths"].add(path_value)

        file_paths = entry.get("files")
        if isinstance(file_paths, list):
            for file_path in file_paths:
                normalized_path = str(file_path or "").strip()
                if normalized_path:
                    aggregate["paths"].add(normalized_path)

        raw_segments = entry.get("segments")
        segment_candidates = (
            [segment for segment in raw_segments if isinstance(segment, dict)]
            if isinstance(raw_segments, list)
            else []
        )
        stat_candidates = segment_candidates if segment_candidates else [entry]

        for candidate in stat_candidates:
            rows_value = candidate.get("row_count", candidate.get("rows"))
            if rows_value is None:
                aggregate["rows_unknown"] = True
            else:
                try:
                    aggregate["rows_total"] += max(int(rows_value), 0)
                except (TypeError, ValueError):
                    aggregate["rows_unknown"] = True

            aggregate["size_bytes"] += _coerce_nonnegative_int(candidate.get("size_bytes"))
            aggregate["start"] = _merge_catalog_dates(
                aggregate["start"],
                candidate.get("start", candidate.get("start_date")),
                latest=False,
            )
            aggregate["end"] = _merge_catalog_dates(
                aggregate["end"],
                candidate.get("end", candidate.get("end_date")),
                latest=True,
            )

        metadata_candidates = [entry, *segment_candidates] if segment_candidates else [entry]
        for candidate in metadata_candidates:
            mtime_ns = candidate.get("mtime_ns")
            if mtime_ns is not None:
                try:
                    numeric_mtime = int(mtime_ns)
                except (TypeError, ValueError):
                    numeric_mtime = None
                if numeric_mtime is not None and (
                    aggregate["mtime_ns"] is None or numeric_mtime > aggregate["mtime_ns"]
                ):
                    aggregate["mtime_ns"] = numeric_mtime

            last_updated = str(candidate.get("last_updated") or "").strip()
            if last_updated and (
                aggregate["last_updated"] is None or last_updated > aggregate["last_updated"]
            ):
                aggregate["last_updated"] = last_updated

            columns = candidate.get("columns")
            if isinstance(columns, list):
                aggregate["columns"].update(str(column) for column in columns if str(column))

            format_value = str(candidate.get("format") or "").strip()
            if format_value:
                aggregate["formats"].add(format_value)

            storage_value = str(candidate.get("storage") or "").strip()
            if storage_value:
                aggregate["storages"].add(storage_value)

            source_meta = candidate.get("source_meta")
            if isinstance(source_meta, dict) and aggregate["source_meta"] is None:
                aggregate["source_meta"] = dict(source_meta)

            validation = candidate.get("validation")
            if isinstance(validation, dict):
                aggregate["validation_seen"] = True
                validation_status = str(validation.get("status") or "").strip().lower()
                if validation_status == "error":
                    aggregate["validation_status"] = "error"
                elif (
                    validation_status
                    and validation_status != "ok"
                    and aggregate["validation_status"] == "ok"
                ):
                    aggregate["validation_status"] = validation_status

                warnings = validation.get("warnings")
                if isinstance(warnings, list):
                    aggregate["validation_warnings"].update(
                        str(item).strip() for item in warnings if str(item).strip()
                    )

                gaps = validation.get("gaps")
                if isinstance(gaps, int):
                    aggregate["validation_gaps"] += max(gaps, 0)
                elif isinstance(gaps, list):
                    aggregate["validation_gaps"] += len(gaps)

                duplicates = validation.get("duplicates")
                if isinstance(duplicates, int):
                    aggregate["validation_duplicates"] += max(duplicates, 0)

                checked_at = str(validation.get("last_checked") or "").strip()
                if checked_at and (
                    aggregate["validation_last_checked"] is None
                    or checked_at > aggregate["validation_last_checked"]
                ):
                    aggregate["validation_last_checked"] = checked_at

            error_value = str(candidate.get("error") or "").strip()
            if error_value:
                aggregate["errors"].add(error_value)

        segment_entries_source = segment_candidates if segment_candidates else [entry]
        for segment in segment_entries_source:
            segment_path = str(segment.get("path") or "").strip()
            if not segment_path:
                continue
            aggregate["paths"].add(segment_path)
            if segment_path in aggregate["segment_paths"]:
                continue

            segment_entry = {
                "path": segment_path,
                "start": segment.get("start", segment.get("start_date")),
                "end": segment.get("end", segment.get("end_date")),
                "rows": segment.get("rows"),
                "size_bytes": _coerce_nonnegative_int(segment.get("size_bytes")),
            }
            if segment.get("segment"):
                segment_entry["segment"] = segment.get("segment")
            for key_name in (
                "mtime_ns",
                "last_updated",
                "columns",
                "format",
                "storage",
                "validation",
                "error",
                "source_meta",
            ):
                if segment.get(key_name) is not None:
                    segment_entry[key_name] = segment.get(key_name)
            aggregate["segments"].append(segment_entry)
            aggregate["segment_paths"].add(segment_path)

    grouped_entries: list[dict] = []
    for aggregate in sorted(
        grouped.values(),
        key=lambda item: (
            item["symbol"],
            item["timeframe"],
            min(item["sources"]) if item["sources"] else "",
        ),
    ):
        rows = None if aggregate["rows_unknown"] else aggregate["rows_total"]
        source_values = sorted(aggregate["sources"])
        asset_class_values = sorted(aggregate["asset_classes"])
        region_values = sorted(aggregate["regions"])
        format_values = sorted(aggregate["formats"])
        storage_values = sorted(aggregate["storages"])
        files = sorted(aggregate["paths"])
        segment_entries = sorted(
            aggregate["segments"], key=lambda segment: str(segment.get("path") or "")
        )

        grouped_entry = {
            "source": source_values[0] if len(source_values) == 1 else (
                "mixed" if source_values else ""
            ),
            "asset_class": asset_class_values[0] if len(asset_class_values) == 1 else (
                "mixed" if asset_class_values else ""
            ),
            "symbol": aggregate["symbol"],
            "timeframe": aggregate["timeframe"],
            "path": _canonical_group_path(aggregate["paths"], aggregate["timeframe"]),
            "format": format_values[0] if len(format_values) == 1 else "parquet",
            "storage": storage_values[0] if len(storage_values) == 1 else (
                "mixed" if storage_values else "local"
            ),
            "rows": rows,
            "row_count": aggregate["rows_total"],
            "size_bytes": aggregate["size_bytes"],
            "start": aggregate["start"],
            "end": aggregate["end"],
            "start_date": aggregate["start"],
            "end_date": aggregate["end"],
            "files": files,
            "file_count": len(files),
        }
        if source_values:
            grouped_entry["sources"] = source_values
        if asset_class_values:
            grouped_entry["asset_classes"] = asset_class_values
        if region_values:
            grouped_entry["regions"] = region_values
            if len(region_values) == 1:
                grouped_entry["region"] = region_values[0]
        if aggregate["mtime_ns"] is not None:
            grouped_entry["mtime_ns"] = aggregate["mtime_ns"]
        if aggregate["last_updated"] is not None:
            grouped_entry["last_updated"] = aggregate["last_updated"]
        if aggregate["columns"]:
            grouped_entry["columns"] = sorted(aggregate["columns"])
        if aggregate["source_meta"] is not None:
            grouped_entry["source_meta"] = aggregate["source_meta"]

        if aggregate["validation_seen"]:
            validation_payload: dict[str, object] = {"status": aggregate["validation_status"]}
            if aggregate["validation_warnings"]:
                validation_payload["warnings"] = sorted(aggregate["validation_warnings"])
            if aggregate["validation_gaps"] > 0:
                validation_payload["gaps"] = aggregate["validation_gaps"]
            if aggregate["validation_duplicates"] > 0:
                validation_payload["duplicates"] = aggregate["validation_duplicates"]
            if aggregate["validation_last_checked"] is not None:
                validation_payload["last_checked"] = aggregate["validation_last_checked"]
            grouped_entry["validation"] = validation_payload

        if aggregate["errors"]:
            grouped_entry["error"] = " | ".join(sorted(aggregate["errors"]))

        include_segments = len(segment_entries) > 1 or any(
            segment.get("segment") for segment in segment_entries
        )
        if include_segments:
            grouped_entry["segments"] = segment_entries
            grouped_entry["segment_count"] = len(segment_entries)

        grouped_entries.append(grouped_entry)

    return grouped_entries


def merge_ohlcv_entries(entries: list[dict]) -> list[dict]:
    normalized_entries = [dict(entry) for entry in entries if isinstance(entry, dict)]
    if not normalized_entries:
        return []
    return _group_ohlcv_entries(normalized_entries)


def scan_ohlcv(data_dir: Path, include_validation: bool = False) -> list[dict]:
    """Scan ohlcv/ subdirectory and return dataset entries."""
    entries = []
    ohlcv_root = data_dir / "ohlcv"
    if not ohlcv_root.exists():
        return entries

    source_meta_index = _load_source_meta(data_dir)
    catalog_cache = _load_catalog_cache(data_dir)

    for parquet_file in sorted(ohlcv_root.rglob("*.parquet")):
        entry = parse_ohlcv_path(parquet_file, ohlcv_root)
        if entry is None:
            continue

        source = entry["source"]
        asset_class = entry["asset_class"]
        symbol = entry["symbol"]
        timeframe = entry["timeframe"]
        entry["format"] = "parquet"
        entry["storage"] = _detect_storage(parquet_file, data_dir)

        try:
            file_mtime = os.path.getmtime(parquet_file)
            cached_entry = catalog_cache.get(str(parquet_file))
            if _should_reuse_cached_summary(cached_entry, file_mtime):
                for key in (
                    "rows",
                    "size_bytes",
                    "mtime_ns",
                    "last_updated",
                    "columns",
                    "start",
                    "end",
                ):
                    if key in cached_entry:
                        entry[key] = cached_entry[key]
                if entry.get("last_updated") is None:
                    entry["last_updated"] = _mtime_to_iso(file_mtime)
            else:
                entry.update(read_parquet_summary(parquet_file))
        except (OSError, ValueError) as e:
            logger.warning("Could not read %s: %s", parquet_file, e)
            try:
                file_mtime = os.path.getmtime(parquet_file)
                entry["size_bytes"] = os.path.getsize(parquet_file)
                entry["mtime_ns"] = int(file_mtime * 1_000_000_000)
                entry["last_updated"] = _mtime_to_iso(file_mtime)
            except OSError:
                entry["size_bytes"] = 0
            entry["rows"] = None
            entry["error"] = str(e)

        # Source metadata (from downloaders)
        meta_key = f"{source}/{asset_class}/{symbol}/{timeframe}"
        if meta_key in source_meta_index:
            entry["source_meta"] = source_meta_index[meta_key]

        # Validation (optional, can be slow for large datasets)
        if include_validation:
            validation = _run_validation(parquet_file, symbol, timeframe)
            if validation:
                entry["validation"] = validation

        entries.append(entry)

    return merge_ohlcv_entries(entries)


def scan_universes(data_dir: Path) -> list[dict]:
    """Scan universes/ subdirectory."""
    entries = []
    uni_dir = data_dir / "universes"
    if not uni_dir.exists():
        return entries

    for f in sorted(uni_dir.iterdir()):
        if f.is_file():
            entries.append(
                {
                    "name": f.stem,
                    "path": str(f),
                    "format": f.suffix.lstrip("."),
                    "size_bytes": f.stat().st_size,
                }
            )
    return entries


def scan_fundamentals(data_dir: Path) -> list[dict]:
    """Scan fundamentals/ subdirectory for non-OHLCV datasets."""
    entries = []
    fund_root = data_dir / "fundamentals"
    if not fund_root.exists():
        return entries

    for parquet_file in sorted(fund_root.rglob("*.parquet")):
        rel = parquet_file.relative_to(fund_root)
        parts = rel.parts
        source = parts[0] if len(parts) > 1 else "unknown"
        category = parts[1] if len(parts) > 2 else "general"
        table_name = parquet_file.stem

        entry = {
            "source": source,
            "category": category,
            "table": table_name,
            "path": str(parquet_file),
            "format": "parquet",
            "size_bytes": parquet_file.stat().st_size,
        }
        try:
            df = pd.read_parquet(parquet_file)
            entry["rows"] = len(df)
            entry["columns"] = list(df.columns)
        except (OSError, ValueError) as e:
            entry["rows"] = None
            entry["error"] = str(e)

        entries.append(entry)
    return entries


def build_catalog(data_dir: Path | None = None, include_validation: bool = False) -> dict:
    """Build the full catalog dict."""
    root = data_dir or DATA_DIR

    ohlcv = scan_ohlcv(root, include_validation=include_validation)

    # Scan fundamentals
    fundamentals = scan_fundamentals(root)

    # Build summary
    symbols = sorted({str(e.get("symbol") or "") for e in ohlcv if str(e.get("symbol") or "").strip()})
    sources: set[str] = set()
    for entry in ohlcv:
        entry_sources = entry.get("sources")
        if isinstance(entry_sources, list):
            for source in entry_sources:
                normalized = str(source or "").strip()
                if normalized:
                    sources.add(normalized)
        else:
            normalized = str(entry.get("source") or "").strip()
            if normalized and normalized != "mixed":
                sources.add(normalized)
    timeframes = sorted(
        {str(e.get("timeframe") or "") for e in ohlcv if str(e.get("timeframe") or "").strip()}
    )

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(root),
        "summary": {
            "total_datasets": len(ohlcv) + len(fundamentals),
            "symbols": len(symbols),
            "sources": sorted(sources),
            "timeframes": timeframes,
            "total_size_bytes": sum(e.get("size_bytes", 0) for e in ohlcv)
            + sum(e.get("size_bytes", 0) for e in fundamentals),
        },
        "ohlcv": ohlcv,
        "fundamentals": fundamentals,
        "universes": scan_universes(root),
    }
    return catalog


def main():
    parser = argparse.ArgumentParser(description="Build data catalog for ~/data/")
    parser.add_argument("--output", "-o", type=str, help="Write catalog to file (default: stdout)")
    parser.add_argument("--data-dir", type=str, help="Override data directory")
    parser.add_argument(
        "--validate", action="store_true", help="Include validation results (slower)"
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    catalog = build_catalog(data_dir, include_validation=args.validate)

    out = json.dumps(catalog, indent=2, default=str)

    if args.output:
        write_json_atomically(Path(args.output), out)
        logger.info("Catalog written to %s (%d datasets)", args.output, len(catalog["ohlcv"]))
    else:
        print(out)


if __name__ == "__main__":
    main()
