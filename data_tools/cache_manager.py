"""In-memory, thread-safe cache for catalog and overview metadata.

This module is designed for data-management reads where catalog payloads are
requested repeatedly (for example by FastAPI endpoints). Cache entries are
created lazily on first read and can be invalidated explicitly after data
mutations like downloads or resampling.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_tools.catalog_builder import build_catalog, merge_ohlcv_entries, parse_ohlcv_path
from data_tools.file_fingerprint import fingerprint_matches
from data_tools.path_safety import ensure_dataset_path_within
from shared.constants import DATA_DIR
from shared.logging import get_logger

logger = get_logger("data_tools.cache")

CacheKey = tuple[str, bool]

_CACHE_LOCK = threading.RLock()
_CATALOG_CACHE: dict[CacheKey, dict[str, Any]] = {}
_OVERVIEW_CACHE: dict[CacheKey, dict[str, Any]] = {}
_CACHE_STATS: dict[str, Any] = {
    "hits": 0,
    "misses": 0,
    "builds": 0,
    "invalidations": 0,
    "last_build_at": None,
    "last_invalidation_at": None,
    "invalidation_reasons": {
        "manual": 0,
        "post_download": 0,
        "post_resample": 0,
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_data_dir(data_dir: Path | str | None) -> Path:
    if data_dir is None:
        return DATA_DIR
    return Path(data_dir).expanduser().resolve()


def _cache_key(data_dir: Path | str | None, include_validation: bool) -> CacheKey:
    root = _resolve_data_dir(data_dir)
    return str(root), bool(include_validation)


def _safe_int(value: object) -> int:
    try:
        # `int` is overloaded and an `object` argument matches none of the
        # overloads, so mypy reports call-overload rather than arg-type.
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _build_overview_metadata(catalog: dict[str, Any]) -> dict[str, Any]:
    summary = catalog.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}

    ohlcv = catalog.get("ohlcv", [])
    fundamentals = catalog.get("fundamentals", [])
    universes = catalog.get("universes", [])
    ohlcv_count = len(ohlcv) if isinstance(ohlcv, list) else 0
    fundamentals_count = len(fundamentals) if isinstance(fundamentals, list) else 0
    universes_count = len(universes) if isinstance(universes, list) else 0

    return {
        "generated_at": catalog.get("generated_at"),
        "data_dir": catalog.get("data_dir"),
        "summary": {
            "total_datasets": _safe_int(
                summary.get("total_datasets", ohlcv_count + fundamentals_count)
            ),
            "symbols": _safe_int(summary.get("symbols", 0)),
            "sources": summary.get("sources", []),
            "timeframes": summary.get("timeframes", []),
            "total_size_bytes": _safe_int(summary.get("total_size_bytes", 0)),
        },
        "counts": {
            "ohlcv": ohlcv_count,
            "fundamentals": fundamentals_count,
            "universes": universes_count,
        },
    }


def _collect_sources(entry: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    source_list = entry.get("sources")
    if isinstance(source_list, list):
        for item in source_list:
            value = str(item or "").strip()
            if value:
                values.add(value)
    source_value = str(entry.get("source") or "").strip()
    if source_value and source_value != "mixed":
        values.add(source_value)
    return values


def _normalize_catalog_payload(payload: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    normalized = dict(payload)
    ohlcv_raw = normalized.get("ohlcv")
    ohlcv_entries = (
        [entry for entry in ohlcv_raw if isinstance(entry, dict)]
        if isinstance(ohlcv_raw, list)
        else []
    )
    merged_ohlcv = merge_ohlcv_entries(ohlcv_entries)

    fundamentals_raw = normalized.get("fundamentals")
    fundamentals = (
        [entry for entry in fundamentals_raw if isinstance(entry, dict)]
        if isinstance(fundamentals_raw, list)
        else []
    )

    universes_raw = normalized.get("universes")
    universes = (
        [entry for entry in universes_raw if isinstance(entry, dict)]
        if isinstance(universes_raw, list)
        else []
    )

    symbol_count = len(
        {str(entry.get("symbol") or "") for entry in merged_ohlcv if entry.get("symbol")}
    )
    source_values: set[str] = set()
    for entry in merged_ohlcv:
        source_values.update(_collect_sources(entry))
    timeframe_values = sorted(
        {str(entry.get("timeframe") or "") for entry in merged_ohlcv if entry.get("timeframe")}
    )

    total_size_bytes = sum(_safe_int(entry.get("size_bytes", 0)) for entry in merged_ohlcv) + sum(
        _safe_int(entry.get("size_bytes", 0)) for entry in fundamentals
    )

    summary = normalized.get("summary")
    summary_payload: dict[str, Any] = dict(summary) if isinstance(summary, dict) else {}
    summary_payload.update(
        {
            "total_datasets": len(merged_ohlcv) + len(fundamentals),
            "symbols": symbol_count,
            "sources": sorted(source_values),
            "timeframes": timeframe_values,
            "total_size_bytes": total_size_bytes,
        }
    )

    normalized["data_dir"] = str(data_dir)
    normalized["ohlcv"] = merged_ohlcv
    normalized["fundamentals"] = fundamentals
    normalized["universes"] = universes
    normalized["summary"] = summary_payload
    return normalized


def _catalog_path(data_dir: Path, value: object) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else data_dir / path


def _catalog_entry_fingerprints(entry: dict[str, Any]) -> dict[str, Any] | None:
    raw = entry.get("file_fingerprints")
    if isinstance(raw, dict) and raw:
        return {str(path): value for path, value in raw.items()}

    fingerprints: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    segments = entry.get("segments")
    if isinstance(segments, list):
        candidates.extend(segment for segment in segments if isinstance(segment, dict))
    if not candidates:
        candidates = [entry]
    for candidate in candidates:
        path = str(candidate.get("path") or "").strip()
        if not path:
            continue
        if candidate.get("mtime_ns") is not None and candidate.get("size_bytes") is not None:
            fingerprints[path] = {
                "mtime_ns": candidate.get("mtime_ns"),
                "size_bytes": candidate.get("size_bytes"),
            }
    return fingerprints or None


def _catalog_matches_disk(payload: dict[str, Any], data_dir: Path) -> bool:
    entries = payload.get("ohlcv")
    if not isinstance(entries, list):
        return True

    ohlcv_root = data_dir / "ohlcv"
    expected_paths: set[str] = set()
    has_fingerprint_metadata = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fingerprints = _catalog_entry_fingerprints(entry)
        if fingerprints is None:
            continue
        has_fingerprint_metadata = True
        for path, expected in fingerprints.items():
            resolved = _catalog_path(data_dir, path)
            try:
                resolved = ensure_dataset_path_within(ohlcv_root, resolved)
            except ValueError:
                return False
            expected_paths.add(str(resolved.resolve(strict=False)))
            if not fingerprint_matches(resolved, expected):
                return False

    if not has_fingerprint_metadata:
        return True

    actual_paths: set[str] = set()
    if ohlcv_root.exists():
        # `path` above is a catalog *key* (a str); the loop below walks `Path`s.
        # One name for both is how a key ends up passed where a file is expected.
        for parquet_file in ohlcv_root.rglob("*.parquet"):
            try:
                ensure_dataset_path_within(ohlcv_root, parquet_file)
            except ValueError:
                continue
            if parse_ohlcv_path(parquet_file, ohlcv_root) is None or not parquet_file.is_file():
                continue
            actual_paths.add(str(parquet_file.resolve(strict=False)))
    return actual_paths == expected_paths


def _fresh_cached_catalog(key: CacheKey, data_dir: Path) -> dict[str, Any] | None:
    while True:
        with _CACHE_LOCK:
            cached_catalog = _CATALOG_CACHE.get(key)
        if cached_catalog is None:
            return None

        matches_disk = _catalog_matches_disk(cached_catalog, data_dir)
        with _CACHE_LOCK:
            if _CATALOG_CACHE.get(key) is not cached_catalog:
                continue
            if not matches_disk:
                _CATALOG_CACHE.pop(key, None)
                _OVERVIEW_CACHE.pop(key, None)
                return None
            _CACHE_STATS["hits"] += 1
            return cached_catalog


def _load_catalog_from_disk(
    data_dir: Path,
    *,
    include_validation: bool,
) -> dict[str, Any] | None:
    """Load catalog.json directly when available to avoid a full filesystem rescan."""
    if include_validation:
        return None

    catalog_path = data_dir / "catalog.json"
    if not catalog_path.exists():
        return None

    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not load catalog cache file",
            extra={"data": {"path": str(catalog_path), "error": str(exc)}},
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "Ignoring malformed catalog cache file",
            extra={"data": {"path": str(catalog_path), "reason": "payload is not an object"}},
        )
        return None

    payload.setdefault("data_dir", str(data_dir))
    payload.setdefault("ohlcv", [])
    payload.setdefault("fundamentals", [])
    payload.setdefault("universes", [])
    payload.setdefault("summary", {})
    if not _catalog_matches_disk(payload, data_dir):
        logger.info(
            "Catalog cache fingerprints are stale",
            extra={"data": {"path": str(catalog_path)}},
        )
        return None
    return payload


def _normalize_reason(reason: str | None) -> str:
    normalized = str(reason or "manual").strip().lower()
    if normalized in {"post_download", "post-download", "download"}:
        return "post_download"
    if normalized in {"post_resample", "post-resample", "resample"}:
        return "post_resample"
    return "manual"


def _matching_cache_keys(
    data_dir: Path | str | None,
    include_validation: bool | None,
) -> list[CacheKey]:
    all_keys = set(_CATALOG_CACHE) | set(_OVERVIEW_CACHE)
    target_dir = str(_resolve_data_dir(data_dir)) if data_dir is not None else None

    matching: list[CacheKey] = []
    for key in all_keys:
        key_dir, key_validation = key
        if target_dir is not None and key_dir != target_dir:
            continue
        if include_validation is not None and key_validation != include_validation:
            continue
        matching.append(key)
    return matching


def get_catalog(
    data_dir: Path | str | None = None,
    *,
    include_validation: bool = False,
) -> dict[str, Any]:
    """Return cached catalog payload, lazily building on first read."""
    key = _cache_key(data_dir, include_validation)
    root = Path(key[0])

    while True:
        cached_catalog = _fresh_cached_catalog(key, root)
        if cached_catalog is not None:
            return deepcopy(cached_catalog)

        with _CACHE_LOCK:
            if key in _CATALOG_CACHE:
                continue
            _CACHE_STATS["misses"] += 1
            catalog = _load_catalog_from_disk(root, include_validation=include_validation)
            if catalog is None:
                catalog = build_catalog(root, include_validation=include_validation)
            catalog = _normalize_catalog_payload(catalog, root)
            _CATALOG_CACHE[key] = catalog
            _OVERVIEW_CACHE[key] = _build_overview_metadata(catalog)
            _CACHE_STATS["builds"] += 1
            _CACHE_STATS["last_build_at"] = _utc_now_iso()
            break

    logger.debug(
        "Catalog cache populated",
        extra={"data": {"data_dir": key[0], "include_validation": key[1]}},
    )
    return deepcopy(catalog)


def get_catalog_ohlcv_entries(
    data_dir: Path | str | None = None,
    *,
    include_validation: bool = False,
    unsafe_no_copy: bool = False,
) -> list[dict[str, Any]]:
    """Return cached catalog OHLCV entries."""
    if unsafe_no_copy:
        key = _cache_key(data_dir, include_validation)
        cached_catalog = _fresh_cached_catalog(key, Path(key[0]))
        if cached_catalog is not None:
            entries = cached_catalog.get("ohlcv", [])
            if isinstance(entries, list):
                return entries

    catalog = get_catalog(data_dir=data_dir, include_validation=include_validation)
    entries = catalog.get("ohlcv", [])
    if not isinstance(entries, list):
        return []
    return entries


def get_overview_metadata(
    data_dir: Path | str | None = None,
    *,
    include_validation: bool = False,
) -> dict[str, Any]:
    """Return cached overview metadata derived from the catalog."""
    key = _cache_key(data_dir, include_validation)
    while True:
        with _CACHE_LOCK:
            cached_overview = _OVERVIEW_CACHE.get(key)
        if cached_overview is None:
            break
        cached_catalog = _fresh_cached_catalog(key, Path(key[0]))
        if cached_catalog is None:
            with _CACHE_LOCK:
                if key not in _CATALOG_CACHE:
                    _OVERVIEW_CACHE.pop(key, None)
            break
        with _CACHE_LOCK:
            cached_overview = _OVERVIEW_CACHE.get(key)
        if cached_overview is not None:
            return deepcopy(cached_overview)
        break

    catalog = get_catalog(data_dir=data_dir, include_validation=include_validation)
    overview = _build_overview_metadata(catalog)

    with _CACHE_LOCK:
        _OVERVIEW_CACHE.setdefault(key, overview)
        return deepcopy(_OVERVIEW_CACHE[key])


def invalidate_cache(
    data_dir: Path | str | None = None,
    *,
    include_validation: bool | None = None,
    reason: str = "manual",
) -> int:
    """Invalidate cache entries globally or for a specific data directory."""
    normalized_reason = _normalize_reason(reason)

    with _CACHE_LOCK:
        keys_to_remove = _matching_cache_keys(data_dir, include_validation)
        for key in keys_to_remove:
            _CATALOG_CACHE.pop(key, None)
            _OVERVIEW_CACHE.pop(key, None)

        removed_count = len(keys_to_remove)
        if removed_count > 0:
            _CACHE_STATS["invalidations"] += removed_count
            _CACHE_STATS["last_invalidation_at"] = _utc_now_iso()

        reason_counts = _CACHE_STATS["invalidation_reasons"]
        reason_counts[normalized_reason] = (
            _safe_int(reason_counts.get(normalized_reason)) + removed_count
        )

    logger.debug(
        "Catalog cache invalidated",
        extra={
            "data": {
                "reason": normalized_reason,
                "removed_entries": removed_count,
                "data_dir": str(_resolve_data_dir(data_dir)) if data_dir is not None else "all",
                "include_validation": include_validation,
            }
        },
    )
    return removed_count


def invalidate_after_download(data_dir: Path | str | None = None) -> int:
    """Invalidate cache after a successful download mutation."""
    return invalidate_cache(data_dir=data_dir, reason="post_download")


def invalidate_after_resample(data_dir: Path | str | None = None) -> int:
    """Invalidate cache after a successful resample mutation."""
    return invalidate_cache(data_dir=data_dir, reason="post_resample")


def get_cache_stats() -> dict[str, Any]:
    """Return cache hit/miss/invalidation stats for observability."""
    with _CACHE_LOCK:
        stats = deepcopy(_CACHE_STATS)
        stats["entries"] = len(_CATALOG_CACHE)
        stats["keys"] = [
            {"data_dir": data_dir, "include_validation": include_validation}
            for data_dir, include_validation in sorted(_CATALOG_CACHE)
        ]
    return stats


__all__ = [
    "get_catalog",
    "get_catalog_ohlcv_entries",
    "get_overview_metadata",
    "invalidate_cache",
    "invalidate_after_download",
    "invalidate_after_resample",
    "get_cache_stats",
]
