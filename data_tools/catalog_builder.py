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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from data_tools.file_fingerprint import file_fingerprint, fingerprint_to_json
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, normalize_timeframe
from shared.ohlcv_store import DatasetEntry, list_datasets, segment_for_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

CATALOG_FILE = "catalog.json"
_COULD_NOT_READ_MESSAGE = "Could not read %s: %s"

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
            logger.warning(_COULD_NOT_READ_MESSAGE, meta_path, exc)
    return {}


def _catalog_path_key(value: object, data_dir: Path | None = None) -> str:
    try:
        path = Path(str(value)).expanduser()
        if data_dir is not None and not path.is_absolute():
            path = Path(data_dir) / path
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(value)


def _load_catalog_cache(data_dir: Path) -> dict[str, dict]:
    """Load the previous catalog to reuse unchanged file metadata."""
    cache_path = data_dir / CATALOG_FILE
    if not cache_path.exists():
        return {}

    try:
        payload = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(_COULD_NOT_READ_MESSAGE, cache_path, exc)
        return {}

    cached_entries = payload.get("ohlcv", []) if isinstance(payload, dict) else []
    cache: dict[str, dict] = {}
    for entry in cached_entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if path:
            cache[_catalog_path_key(path, data_dir)] = entry
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
                cache[_catalog_path_key(segment_path, data_dir)] = merged_segment_entry
    return cache


def _detect_storage(parquet_path: Path, data_dir: Path) -> str:
    """Determine storage mode: local, cached, or remote."""
    cache_dir = data_dir / "cache"
    if str(parquet_path).startswith(str(cache_dir)):
        return "cached"
    return "local"


def _run_validation(
    parquet_path: Path, data_dir: Path | None = None
) -> dict | None:
    """Run data validation checks and return summary dict."""
    try:
        from data_tools.validate import validate_parquet

        result = validate_parquet(
            parquet_path,
            ohlcv_root=(data_dir / "ohlcv") if data_dir is not None else None,
        )
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


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    return file_fingerprint(path)


def _should_reuse_cached_summary(
    cached_entry: dict[str, Any] | None, fingerprint: tuple[int, int] | None
) -> bool:
    """Reuse a cached summary only on exact fingerprint match (B3).

    The old float-mtime/ISO comparison (±1µs epsilon) called a rewritten
    file fresh when only its content changed within the window. Size is
    part of the fingerprint so same-second rewrites with different bytes
    cannot masquerade as fresh. Legacy entries without the pair never match.
    """
    if not isinstance(cached_entry, dict) or fingerprint is None:
        return False
    try:
        cached_ns = int(cached_entry["mtime_ns"])
        cached_size = int(cached_entry["size_bytes"])
    except (KeyError, TypeError, ValueError):
        return False
    return (cached_ns, cached_size) == fingerprint


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


def _new_group_aggregate(symbol: str, timeframe: str) -> dict:
    return {
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
        "file_fingerprints": {},
    }


def _entry_segment_candidates(entry: dict) -> list[dict]:
    raw_segments = entry.get("segments")
    if not isinstance(raw_segments, list):
        return []
    return [segment for segment in raw_segments if isinstance(segment, dict)]


def _collect_entry_paths(aggregate: dict, entry: dict) -> None:
    path_value = str(entry.get("path") or "")
    if path_value:
        aggregate["paths"].add(path_value)

    file_paths = entry.get("files")
    if isinstance(file_paths, list):
        for file_path in file_paths:
            normalized_path = str(file_path or "").strip()
            if normalized_path:
                aggregate["paths"].add(normalized_path)


def _accumulate_stats(aggregate: dict, candidate: dict) -> None:
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


def _latest_scalar(aggregate: dict, key: str, value: Any, *, numeric: bool) -> None:
    """Keep the largest (numeric or string) value seen for a scalar aggregate key."""
    current = aggregate[key]
    if numeric:
        if value is not None and (current is None or value > current):
            aggregate[key] = value
        return
    if value and (current is None or value > current):
        aggregate[key] = value


def _accumulate_metadata(aggregate: dict, candidate: dict) -> None:
    raw_fingerprints = candidate.get("file_fingerprints")
    if isinstance(raw_fingerprints, dict):
        for raw_path, raw_fingerprint in raw_fingerprints.items():
            if isinstance(raw_fingerprint, dict):
                aggregate["file_fingerprints"][str(raw_path)] = dict(raw_fingerprint)

    path_value = str(candidate.get("path") or "").strip()
    candidate_mtime = candidate.get("mtime_ns")
    candidate_size = candidate.get("size_bytes")
    if path_value and candidate_mtime is not None and candidate_size is not None:
        try:
            aggregate["file_fingerprints"][path_value] = fingerprint_to_json(
                (int(candidate_mtime), int(candidate_size))
            )
        except (TypeError, ValueError):
            pass

    mtime_ns = candidate.get("mtime_ns")
    if mtime_ns is not None:
        try:
            numeric_mtime = int(mtime_ns)
        except (TypeError, ValueError):
            numeric_mtime = None
        if numeric_mtime is not None:
            _latest_scalar(aggregate, "mtime_ns", numeric_mtime, numeric=True)

    last_updated = str(candidate.get("last_updated") or "").strip()
    _latest_scalar(aggregate, "last_updated", last_updated, numeric=False)

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
        _accumulate_validation(aggregate, validation)

    error_value = str(candidate.get("error") or "").strip()
    if error_value:
        aggregate["errors"].add(error_value)


def _accumulate_validation(aggregate: dict, validation: dict) -> None:
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


def _append_segment_entries(aggregate: dict, segment_candidates: list[dict]) -> None:
    for segment in segment_candidates:
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


def _accumulate_entry(aggregate: dict, entry: dict) -> None:
    segment_candidates = _entry_segment_candidates(entry)
    stat_candidates = segment_candidates if segment_candidates else [entry]

    for candidate in stat_candidates:
        _accumulate_stats(aggregate, candidate)

    metadata_candidates = [entry, *segment_candidates] if segment_candidates else [entry]
    for candidate in metadata_candidates:
        _accumulate_metadata(aggregate, candidate)

    _append_segment_entries(aggregate, segment_candidates if segment_candidates else [entry])


def _group_ohlcv_entries(entries: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}

    for raw_entry in entries:
        entry = dict(raw_entry)
        entry["timeframe"] = normalize_timeframe(str(entry.get("timeframe") or ""))
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "")
        key = (symbol, timeframe)

        aggregate = grouped.setdefault(key, _new_group_aggregate(symbol, timeframe))

        _collect_text_values(aggregate["sources"], entry.get("source"))
        _collect_text_values(aggregate["sources"], entry.get("sources"))
        _collect_text_values(aggregate["asset_classes"], entry.get("asset_class"))
        _collect_text_values(aggregate["asset_classes"], entry.get("asset_classes"))
        _collect_text_values(aggregate["regions"], entry.get("region"))
        _collect_text_values(aggregate["regions"], entry.get("regions"))

        _collect_entry_paths(aggregate, entry)
        _accumulate_entry(aggregate, entry)

    grouped_entries: list[dict] = []
    for aggregate in sorted(
        grouped.values(),
        key=lambda item: (
            item["symbol"],
            item["timeframe"],
            min(item["sources"]) if item["sources"] else "",
        ),
    ):
        grouped_entry = _build_grouped_entry(aggregate)
        grouped_entries.append(grouped_entry)

    return grouped_entries


def _single_or_label(values: list[str], default: str) -> str:
    if len(values) == 1:
        return values[0]
    return "mixed" if values else default


def _build_grouped_entry(aggregate: dict) -> dict:
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

    grouped_entry: dict[str, Any] = {
        "source": _single_or_label(source_values, ""),
        "asset_class": _single_or_label(asset_class_values, ""),
        "symbol": aggregate["symbol"],
        "timeframe": aggregate["timeframe"],
        "path": _canonical_group_path(aggregate["paths"], aggregate["timeframe"]),
        "format": format_values[0] if len(format_values) == 1 else "parquet",
        "storage": _single_or_label(storage_values, "local"),
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
    if aggregate["file_fingerprints"]:
        grouped_entry["file_fingerprints"] = dict(sorted(aggregate["file_fingerprints"].items()))
    if aggregate["last_updated"] is not None:
        grouped_entry["last_updated"] = aggregate["last_updated"]
    if aggregate["columns"]:
        grouped_entry["columns"] = sorted(aggregate["columns"])
    if aggregate["source_meta"] is not None:
        grouped_entry["source_meta"] = aggregate["source_meta"]

    if aggregate["validation_seen"]:
        grouped_entry["validation"] = _build_validation_payload(aggregate)

    if aggregate["errors"]:
        grouped_entry["error"] = " | ".join(sorted(aggregate["errors"]))

    include_segments = len(segment_entries) > 1 or any(
        segment.get("segment") for segment in segment_entries
    )
    if include_segments:
        grouped_entry["segments"] = segment_entries
        grouped_entry["segment_count"] = len(segment_entries)

    return grouped_entry


def _build_validation_payload(aggregate: dict) -> dict[str, Any]:
    validation_payload: dict[str, Any] = {"status": aggregate["validation_status"]}
    if aggregate["validation_warnings"]:
        validation_payload["warnings"] = sorted(aggregate["validation_warnings"])
    if aggregate["validation_gaps"] > 0:
        validation_payload["gaps"] = aggregate["validation_gaps"]
    if aggregate["validation_duplicates"] > 0:
        validation_payload["duplicates"] = aggregate["validation_duplicates"]
    if aggregate["validation_last_checked"] is not None:
        validation_payload["last_checked"] = aggregate["validation_last_checked"]
    return validation_payload


def merge_ohlcv_entries(entries: list[dict]) -> list[dict]:
    normalized_entries = [dict(entry) for entry in entries if isinstance(entry, dict)]
    if not normalized_entries:
        return []
    return _group_ohlcv_entries(normalized_entries)


def _summary_with_cache(
    parquet_path: Path,
    data_dir: Path,
    catalog_cache: dict[str, dict],
) -> dict[str, Any]:
    try:
        fingerprint = _file_fingerprint(parquet_path)
    except OSError:
        fingerprint = None
    cached_entry = catalog_cache.get(_catalog_path_key(parquet_path, data_dir))
    if _should_reuse_cached_summary(cached_entry, fingerprint):
        summary = {
            key: cached_entry[key]
            for key in (
                "rows",
                "size_bytes",
                "mtime_ns",
                "last_updated",
                "columns",
                "start",
                "end",
            )
            if key in cached_entry
        }
        if summary.get("last_updated") is None and fingerprint is not None:
            summary["last_updated"] = _mtime_to_iso(fingerprint[0] / 1_000_000_000)
        return summary

    try:
        return read_parquet_summary(parquet_path)
    except (OSError, ValueError) as exc:
        logger.warning(_COULD_NOT_READ_MESSAGE, parquet_path, exc)
        try:
            stat = parquet_path.stat()
            summary = {
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "last_updated": _mtime_to_iso(stat.st_mtime),
            }
        except OSError:
            summary = {"size_bytes": 0}
        summary["rows"] = None
        summary["error"] = str(exc)
        return summary


def _dataset_segment_entry(
    dataset: DatasetEntry,
    parquet_path: Path,
    data_dir: Path,
    catalog_cache: dict[str, dict],
    *,
    include_validation: bool,
) -> dict[str, Any]:
    segment = segment_for_path(data_dir, parquet_path)
    entry: dict[str, Any] = {
        "path": str(parquet_path),
        "format": "parquet",
        "storage": _detect_storage(parquet_path, data_dir),
    }
    if segment:
        entry["segment"] = segment
    entry.update(_summary_with_cache(parquet_path, data_dir, catalog_cache))

    if include_validation:
        validation = _run_validation(parquet_path, data_dir=data_dir)
        if validation:
            entry["validation"] = validation
    return entry


def _dataset_catalog_entry(
    dataset: DatasetEntry,
    data_dir: Path,
    catalog_cache: dict[str, dict],
    *,
    include_validation: bool,
    source_meta_index: dict[str, dict],
) -> dict[str, Any]:
    segment_paths = list(dataset.segments) or [dataset.path]
    segments = [
        _dataset_segment_entry(
            dataset,
            path,
            data_dir,
            catalog_cache,
            include_validation=include_validation,
        )
        for path in segment_paths
    ]

    entry: dict[str, Any] = {
        "source": dataset.source,
        "asset_class": dataset.asset_class,
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "path": str(dataset.path),
        "format": "parquet",
        "storage": _detect_storage(dataset.path, data_dir),
        "files": [str(path) for path in segment_paths],
        "segments": segments,
    }
    if dataset.region:
        entry["region"] = dataset.region
    if dataset.source_symbol and dataset.source_symbol != dataset.symbol:
        entry["source_symbol"] = dataset.source_symbol

    meta_key = f"{dataset.source}/{dataset.asset_class}/{dataset.symbol}/{dataset.timeframe}"
    if meta_key in source_meta_index:
        entry["source_meta"] = source_meta_index[meta_key]
    return entry


def scan_ohlcv(data_dir: Path, include_validation: bool = False) -> list[dict]:
    """Scan the manifest-driven OHLCV store and return dataset entries."""
    source_meta_index = _load_source_meta(data_dir)
    catalog_cache = _load_catalog_cache(data_dir)
    entries = [
        _dataset_catalog_entry(
            dataset,
            data_dir,
            catalog_cache,
            include_validation=include_validation,
            source_meta_index=source_meta_index,
        )
        for dataset in list_datasets(data_dir)
    ]
    return merge_ohlcv_entries(entries)


def scan_universes(data_dir: Path) -> list[dict]:
    """Scan universes/ subdirectory."""
    entries = []
    uni_dir = data_dir / "universes"
    if not uni_dir.exists():
        return entries

    entries.extend(
        {
            "name": entry.stem,
            "path": str(entry),
            "format": entry.suffix.lstrip("."),
            "size_bytes": entry.stat().st_size,
        }
        for entry in sorted(uni_dir.iterdir())
        if entry.is_file()
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
    symbols = sorted(
        {str(e.get("symbol") or "") for e in ohlcv if str(e.get("symbol") or "").strip()}
    )
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
