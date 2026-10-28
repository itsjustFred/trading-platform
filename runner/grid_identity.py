"""The identity of a grid cell result: the config and the data behind it.

A grid cell is a cached artifact: ``--resume`` reuses a persisted
``.trades.json`` instead of recomputing it, and the matrix/viewer serve those
artifacts afterwards. Reuse is only sound while the artifact answers the same
question, so a cell result carries an identity and a reader must refuse to
serve one whose identity is absent or different.

The identity has two halves, because they fail independently:

* **config** — engine, strategy source bytes, symbol, timeframe, resolved
  window, capital, every risk/modelling knob and the strategy parameters. A
  strategy file edited after the fact changes this half even when no flag
  changed, which is the whole point: the artifact's numbers were produced by
  code that no longer exists on disk.
* **data** — the (mtime, size) fingerprint of every parquet the cell could
  have read. Re-downloading or rewriting a frame changes this half even when
  every flag is identical.

Both halves are hashed into one ``fingerprint`` string. The full payload is
kept alongside it so a mismatch can be explained instead of just refused.

Fail-closed rules, in both directions:

* an artifact with no identity (or an older ``version``) is refused: it was
  written by something that never promised what it was computed from;
* an identity whose data half could not be resolved (``None``) is refused: the
  inputs are unknown, so "it looks unchanged" cannot be claimed;
* an identity the reader cannot re-verify (:func:`data_staleness_reason`) is
  refused rather than reported as current.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from data_tools.file_fingerprint import file_fingerprint
from shared.constants import DATA_DIR

#: Bumped whenever the identity payload changes shape. An artifact stamped with
#: another version cannot be compared field-by-field, so it is refused.
GRID_IDENTITY_VERSION = 1

#: Where the identity lives inside a persisted ``BacktestResult``.
GRID_IDENTITY_KEY = "grid_identity"

__all__ = [
    "GRID_IDENTITY_KEY",
    "GRID_IDENTITY_VERSION",
    "cell_identity",
    "data_fingerprint",
    "data_root_for",
    "data_staleness_reason",
    "fingerprint_of",
    "identity_from_result_meta",
    "identity_matches",
    "read_identity",
    "strategy_source_sha256",
]


def _canonical(payload: Any) -> str:
    """A stable JSON encoding: key order and float repr can never drift."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint_of(payload: Any) -> str:
    """The sha256 of a JSON-canonical payload."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def strategy_source_sha256(strategy_path: str | Path) -> str | None:
    """sha256 of the strategy file's bytes, or None when it cannot be read.

    None is a refusal, not a wildcard: an artifact whose strategy cannot be
    hashed is never a candidate for reuse.
    """
    try:
        return hashlib.sha256(Path(strategy_path).read_bytes()).hexdigest()
    except OSError:
        return None


def data_root_for(data_dir: str = "") -> Path:
    """The OHLCV root a cell reads, resolved the same way everywhere."""
    return Path(data_dir) / "ohlcv" if str(data_dir or "").strip() else Path(DATA_DIR) / "ohlcv"


def _candidate_frames(
    *,
    symbol: str,
    timeframe: str,
    data_dir: str,
    intrabar_timeframe: str = "",
) -> list[Path]:
    """Every parquet a cell for this symbol/timeframe could have read.

    All candidates are fingerprinted, not just the one the selector would pick
    today: a second copy of the same symbol/timeframe from another source can
    change which file wins, so a cell computed from the previous winner is no
    longer the answer to the same question.
    """
    root = data_root_for(data_dir)
    normalized_symbol = str(symbol or "").strip().upper()
    frames = [
        str(timeframe or "").strip().lower(),
        str(intrabar_timeframe or "").strip().lower(),
    ]
    found: set[Path] = set()
    for frame in frames:
        if not frame:
            continue
        found.update(root.rglob(f"**/{normalized_symbol}/{frame}.parquet"))
    return sorted(found, key=lambda path: path.as_posix())


def data_fingerprint(files: Iterable[Path | str]) -> dict[str, Any] | None:
    """Fingerprint a set of data files, or None when one of them is unreadable.

    None means "unknown", and unknown is a refusal: a missing mtime/size for
    any candidate means the data half of the identity cannot be established.
    """
    entries: list[dict[str, Any]] = []
    for path in sorted((Path(item) for item in files), key=lambda p: p.as_posix()):
        fingerprint = file_fingerprint(path)
        if fingerprint is None:
            return None
        entries.append(
            {
                "path": path.as_posix(),
                "mtime_ns": fingerprint[0],
                "size_bytes": fingerprint[1],
            }
        )
    return {"files": entries, "fingerprint": fingerprint_of(entries)}


def cell_identity(
    *,
    engine: str,
    strategy: str | Path,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    capital: float,
    risk_pct: float | None,
    max_position_exposure_pct: float | None = None,
    max_leverage: float | None = None,
    intrabar_resolution: str = "open_close",
    params: Mapping[str, Any] | None = None,
    data_dir: str = "",
    intrabar_timeframe: str = "",
) -> dict[str, Any]:
    """Build the identity of one grid cell.

    Every argument is the *request* the cell was (or would be) run with; the
    resolved window must already be resolved, so the identity cannot describe
    "whatever dates were detected" and quietly change between two runs.
    """
    config = {
        "engine": str(engine or "").strip().lower(),
        "strategy_slug": Path(str(strategy)).stem,
        "strategy_sha256": strategy_source_sha256(strategy),
        "symbol": str(symbol or "").strip().upper(),
        "timeframe": str(timeframe or "").strip().lower(),
        "start": str(start or ""),
        "end": str(end or ""),
        "capital": float(capital),
        "risk_per_trade_pct": None if risk_pct is None else float(risk_pct),
        "max_position_exposure_pct": (
            None
            if max_position_exposure_pct is None
            else float(max_position_exposure_pct)
        ),
        "max_leverage": None if max_leverage is None else float(max_leverage),
        "modeling_resolution": str(intrabar_resolution or "open_close"),
        # Parameters go in as their canonical JSON string, not as the object:
        # the stored identity must be deterministic across processes.
        "params": _canonical(dict(params or {})),
        "data_dir": str(data_root_for(data_dir).parent),
    }
    data = data_fingerprint(
        _candidate_frames(
            symbol=symbol,
            timeframe=timeframe,
            data_dir=data_dir,
            intrabar_timeframe=intrabar_timeframe,
        )
    )
    payload = {
        "version": GRID_IDENTITY_VERSION,
        "config": config,
        "config_fingerprint": fingerprint_of(config),
        # None here is meaningful and must survive: "the data half could not
        # be established" is not the same claim as "the data is unchanged".
        "data": data,
    }
    return {**payload, "fingerprint": fingerprint_of(payload)}


def identity_from_result_meta(meta: Any) -> dict[str, Any] | None:
    """Read a persisted result's grid identity, or None when it carries none."""
    data = getattr(meta, "data", None)
    if not isinstance(data, dict):
        return None
    identity = data.get(GRID_IDENTITY_KEY)
    if not isinstance(identity, dict):
        return None
    return identity


def read_identity(path: str | Path) -> dict[str, Any] | None:
    """Read a persisted ``.trades.json`` artifact's grid identity.

    An unreadable or malformed artifact yields None, which every caller treats
    as "unknown identity" and therefore refuses.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    data = meta.get("data")
    if not isinstance(data, dict):
        return None
    identity = data.get(GRID_IDENTITY_KEY)
    return identity if isinstance(identity, dict) else None


def identity_matches(stored: Any, current: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether a persisted identity still answers the current request.

    Returns ``(ok, reason)``; the reason is filled in whenever ``ok`` is False
    so a refusal can be logged with something better than "mismatch".
    """
    if not isinstance(stored, dict):
        return False, "artifact carries no grid identity"
    if stored.get("version") != GRID_IDENTITY_VERSION:
        return (
            False,
            f"grid identity version {stored.get('version')!r} is not "
            f"{GRID_IDENTITY_VERSION}",
        )
    if stored.get("data") is None:
        return False, "the recorded data half could not be established"
    if current.get("data") is None:
        return False, "the current data half could not be established"

    stored_config = stored.get("config")
    current_config = current.get("config")
    if not isinstance(stored_config, dict) or not isinstance(current_config, dict):
        return False, "grid identity carries no comparable config"
    if stored_config != current_config:
        changed = sorted(
            key
            for key in set(stored_config) | set(current_config)
            if stored_config.get(key) != current_config.get(key)
        )
        return False, f"config changed: {', '.join(changed)}"
    if stored.get("data") != current.get("data"):
        return False, "the underlying data changed"
    if stored.get("fingerprint") != current.get("fingerprint"):
        return False, "grid identity fingerprint mismatch"
    return True, ""


def data_staleness_reason(identity: Any) -> str | None:
    """Re-verify a recorded identity against the data on disk.

    This is the check a reader without a current request can make (the viewer
    serving a historical matrix): the config half cannot be re-derived from
    nothing, but the data half can always be re-stat'ed, and a changed frame
    means the numbers came from data that no longer exists.

    Returns None when the identity still holds, else a human-readable reason.
    """
    if not isinstance(identity, dict):
        return "artifact carries no grid identity"
    if identity.get("version") != GRID_IDENTITY_VERSION:
        return f"grid identity version {identity.get('version')!r} is not {GRID_IDENTITY_VERSION}"
    data = identity.get("data")
    if not isinstance(data, dict):
        return "the recorded data half could not be established"
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return "the identity recorded no data files"
    for entry in files:
        if not isinstance(entry, dict):
            return "a recorded data file entry is unreadable"
        path = str(entry.get("path") or "")
        if not path:
            return "a recorded data file has no path"
        current = file_fingerprint(path)
        if current is None:
            return f"data file is gone: {path}"
        try:
            recorded = (int(entry.get("mtime_ns", -1)), int(entry.get("size_bytes", -1)))
        except (TypeError, ValueError):
            # A malformed recorded stat cannot be compared, and "I cannot check
            # it" is exactly the case that must not be served.
            return f"a recorded data file entry is unreadable: {path}"
        if current != recorded:
            return f"data file changed since the run: {path}"
    if data.get("fingerprint") != fingerprint_of(files):
        return "the recorded data fingerprint does not match its own file list"
    return None


def iter_row_identities(rows: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str]]:
    """Pair every row that names an artifact with that artifact's staleness.

    A row without an ``output`` path has no artifact to verify and is not
    reported; rows whose artifact is missing or unreadable ARE reported,
    because "I cannot check it" is exactly the case that must not be served.
    """
    findings: list[tuple[Mapping[str, Any], str]] = []
    for row in rows:
        output = str(row.get("output") or "").strip()
        if not output:
            continue
        if not Path(output).exists():
            findings.append((row, f"artifact is gone: {output}"))
            continue
        findings.append((row, data_staleness_reason(read_identity(output)) or ""))
    return [(row, reason) for row, reason in findings if reason]
