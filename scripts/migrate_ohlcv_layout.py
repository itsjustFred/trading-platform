"""Migrate the legacy OHLCV layout into the manifest layout (#471).

Old layout::

    <src>/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{tf}.parquet
    <src>/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{tf}_{bid,ask}.parquet
    <src>/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{tf}_{YYYY}_{MM}.parquet
    <src>/ohlcv/{source}/{asset_class}/{symbol}.parquet          # flat 1d variant

New layout (written through :func:`shared.ohlcv_store.write_frame`)::

    <dst>/ohlcv/<source>/manifest.json
    <dst>/ohlcv/<source>/<SYMBOL>/<SYMBOL>_<TF>.parquet
    <dst>/ohlcv/<source>/<SYMBOL>/<SYMBOL>_<TF>_<YYYY>_<MM>.parquet

``_bid``/``_ask`` split files are merged into ONE two-sided frame
(``bid_*``/``ask_*`` columns) before writing. ``asset_class``/``region`` are
taken from the old path. A symbol that appears under more than one
(asset_class, region) pair within a source is a collision: it is skipped and
reported, never silently merged — the new layout keys symbols uniquely per
source. Nothing is ever deleted; ``--dry-run`` reports without writing.

Usage::

    python -m scripts.migrate_ohlcv_layout \
        --src /mnt/data/ohlcv --dst /new/data/ohlcv \
        [--source dukascopy ...] [--skip-source dukascopy] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.migrate_ohlcv_layout
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import import_origin as _guard

_guard.ensure_own_checkout("migrate_ohlcv_layout")
# --- end import-origin guard ---
from shared import ohlcv_store
from shared.constants import GRID_TIMEFRAME_ALIASES, VALID_TIMEFRAMES, normalize_timeframe

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")

#: Old on-disk timeframe spellings → platform timeframe. Covers the canonical
#: VALID_TIMEFRAMES stems plus every alias the downloaders ever wrote
#: (JForex ``m1``/``h1``/``d1``/``w1`` stems, grid CLI spellings).
_OLD_TF_ALIASES: dict[str, str] = {
    **GRID_TIMEFRAME_ALIASES,
    "w1": "1w",
    "tick": "tick",
    "s1": "s1",
}

_SEGMENT_RE = re.compile(r"^(?P<tf>.+)_(?P<segment>\d{4}_\d{2})$")


@dataclass
class _SideFrames:
    """The parquet frames collected for one (symbol, tf, segment) target."""

    plain: pd.DataFrame | None = None
    bid: pd.DataFrame | None = None
    ask: pd.DataFrame | None = None


@dataclass
class MigrationReport:
    """What the run did (or would do, under --dry-run)."""

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = ["per-source summary:"]
        for source in sorted(self.per_source):
            counts = self.per_source[source]
            lines.append(
                f"  {source}: written={counts.get('written', 0)} "
                f"skipped={counts.get('skipped', 0)} "
                f"collisions={counts.get('collisions', 0)}"
            )
        for kind, items in (("collision", self.collisions), ("skipped", self.skipped)):
            lines.extend(f"  {kind}: {item}" for item in items)
        if not self.written and not self.skipped and not self.collisions:
            lines.append("  (nothing to migrate)")
        return lines


def _old_timeframe(stem: str) -> tuple[str, str] | None:
    """Old file stem → (platform timeframe, segment).

    Accepts ``{tf}``, ``{tf}_{YYYY}_{MM}`` and the alias spellings the
    downloaders wrote (``m1``, ``h1``, ``w1``, …). Returns None for stems
    that cannot be mapped — the caller reports them as skipped.
    """
    text = stem.strip().lower()
    segment = ""
    segment_match = _SEGMENT_RE.match(text)
    if segment_match is not None:
        text = segment_match.group("tf")
        segment = segment_match.group("segment")
    normalized = normalize_timeframe(text)
    if normalized not in VALID_TIMEFRAMES:
        normalized = _OLD_TF_ALIASES.get(text, "")
    if normalized not in VALID_TIMEFRAMES:
        return None
    return normalized, segment


def _classify_old_file(rel_parts: list[str]) -> tuple[str, str, str, str, str] | None:
    """Map an old-layout path (relative to the src root, file included) to
    ``(asset_class, region, symbol, timeframe, segment)``.

    Returns None when the path does not match the old layout or the file
    stem cannot be mapped to a platform timeframe — the caller reports it
    as skipped.
    """
    if len(rel_parts) < 3 or not rel_parts[-1].endswith(".parquet"):
        return None
    asset_class = rel_parts[1]
    filename = rel_parts[-1]
    # Flat variant: {source}/{asset_class}/{symbol}.parquet → implicit 1d.
    if len(rel_parts) == 3:
        symbol = filename[: -len(".parquet")]
        if not symbol or "." in symbol or _old_timeframe(symbol) is not None:
            return None
        return asset_class, "", symbol, "1d", ""
    if len(rel_parts) == 4:
        region, symbol = "", rel_parts[2]
    elif len(rel_parts) == 5:
        region, symbol = rel_parts[2], rel_parts[3]
    else:
        return None
    stem = filename[: -len(".parquet")]
    prefix = f"{symbol}_"
    if stem.lower().startswith(prefix.lower()):
        stem = stem[len(prefix) :]
    base_stem, _side = _strip_side_suffix(stem)
    parsed = _old_timeframe(base_stem)
    if parsed is None:
        return None
    timeframe, segment = parsed
    return asset_class, region, symbol, timeframe, segment


def _strip_side_suffix(stem: str) -> tuple[str, str]:
    """``h1_bid`` → ("h1", "bid"); ``1h`` → ("1h", "")."""
    lowered = stem.lower()
    for side in ("_bid", "_ask"):
        if lowered.endswith(side):
            return stem[: -len(side)], side[1:]
    return stem, ""


def _read_old_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame.columns = [str(column).lower() for column in frame.columns]
    return frame


def _merge_side_frames(frames: _SideFrames) -> pd.DataFrame:
    """Merge plain/bid/ask frames into one frame for ``write_frame``.

    With split sides the result carries ``bid_*``/``ask_*`` columns; the
    canonical ``open..volume`` come from the plain frame when present, else
    the bid side, else the ask side.
    """
    parts: list[pd.DataFrame] = []
    plain = frames.plain
    if plain is not None:
        parts.append(plain)
    for side, frame in (("bid", frames.bid), ("ask", frames.ask)):
        if frame is None:
            continue
        renamed = frame.copy()
        renamed.columns = [
            column if column == "timestamp" else f"{side}_{column}"
            for column in renamed.columns
        ]
        parts.append(renamed)
    if not parts:
        raise ValueError("nothing to merge")
    merged = parts[0]
    for extra in parts[1:]:
        merged = merged.merge(extra, on="timestamp", how="outer")
    merged = merged.sort_values("timestamp").reset_index(drop=True)
    if plain is None:
        # No canonical columns stored: expose the bid side (else ask) as
        # open/high/low/close/volume so engines keep working.
        source_side = "bid" if frames.bid is not None else "ask"
        for column in _OHLCV_COLUMNS:
            side_column = f"{source_side}_{column}"
            if side_column in merged.columns and column not in merged.columns:
                merged[column] = merged[side_column]
    return merged


def _collect_source(
    src_root: Path, source: str
) -> tuple[dict[tuple[str, str, str, str], dict[str, object]], list[str]]:
    """Group a source's old-layout parquets into write targets.

    Returns ``{(symbol, asset_class, region, tf|segment): {symbol,
    timeframe, segment, asset_class, region, frames: {plain|bid|ask: path}}}``
    plus skipped-file descriptions.
    """
    targets: dict[tuple[str, str, str, str], dict[str, object]] = {}
    skipped: list[str] = []
    source_dir = src_root / source
    for parquet in sorted(source_dir.rglob("*.parquet")):
        rel = parquet.relative_to(src_root).parts
        classified = _classify_old_file(list(rel))
        if classified is None:
            skipped.append(f"{source}: unrecognized file {parquet}")
            continue
        asset_class, region, symbol, timeframe, segment = classified
        stem = parquet.name[: -len(".parquet")]
        prefix = f"{symbol}_"
        if stem.lower().startswith(prefix.lower()):
            stem = stem[len(prefix) :]
        _base, side = _strip_side_suffix(stem)
        slot = side if side in {"bid", "ask"} else "plain"
        # asset_class/region are part of the key so the same symbol under two
        # old layouts lands in two targets — that is exactly the collision
        # the migration must refuse to merge silently.
        key = (symbol, asset_class, region, f"{timeframe}|{segment}")
        target = targets.setdefault(
            key,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "segment": segment,
                "asset_class": asset_class,
                "region": region,
                "frames": {},
            },
        )
        slots = target["frames"]
        assert isinstance(slots, dict)
        if slot in slots:
            skipped.append(
                f"{source}: duplicate {slot} file for {symbol}/{timeframe} "
                f"({parquet.name})"
            )
            continue
        slots[slot] = parquet
    return targets, skipped


def migrate(
    src_root: str | Path,
    dst_root: str | Path,
    *,
    sources: Iterable[str] | None = None,
    skip_sources: Iterable[str] = (),
    dry_run: bool = False,
) -> MigrationReport:
    """Run the migration; returns the report (writes unless ``dry_run``)."""
    src = Path(src_root)
    dst = Path(dst_root)
    # --dst is the new ohlcv root; write_frame needs its data root, which is
    # the parent of a directory literally named "ohlcv".
    ohlcv_dst = dst if dst.name == "ohlcv" else dst / "ohlcv"
    data_root = ohlcv_dst.parent

    wanted = {str(name).lower() for name in (sources or [])}
    skipped_sources = {str(name).lower() for name in skip_sources}
    report = MigrationReport()

    if not src.is_dir():
        report.skipped.append(f"source root missing: {src}")
        return report

    source_names = sorted(
        child.name
        for child in src.iterdir()
        if child.is_dir()
        and (not wanted or child.name.lower() in wanted)
        and child.name.lower() not in skipped_sources
    )

    for source in source_names:
        targets, skipped = _collect_source(src, source)
        report.per_source.setdefault(source, {"written": 0, "skipped": 0, "collisions": 0})
        report.skipped.extend(f"{item}" for item in skipped)
        report.per_source[source]["skipped"] += len(skipped)

        # Collisions: one symbol mapping to more than one (asset_class, region).
        symbol_meta: dict[str, set[tuple[str, str]]] = {}
        for target in targets.values():
            symbol_meta.setdefault(str(target["symbol"]), set()).add(
                (str(target["asset_class"]), str(target["region"]))
            )
        collided = {
            symbol for symbol, metas in symbol_meta.items() if len(metas) > 1
        }
        for symbol in sorted(collided):
            metas = sorted(symbol_meta[symbol])
            report.collisions.append(
                f"{source}: symbol {symbol} appears under multiple layouts {metas}; skipped"
            )
            report.per_source[source]["collisions"] += 1

        for (_symbol, _asset, _region, _tfseg), target in sorted(targets.items()):
            symbol = str(target["symbol"])
            if symbol in collided:
                continue
            timeframe = str(target["timeframe"])
            segment = str(target["segment"])
            slots = target["frames"]
            assert isinstance(slots, dict)
            side_frames = _SideFrames(
                plain=_read_old_frame(slots["plain"]) if "plain" in slots else None,
                bid=_read_old_frame(slots["bid"]) if "bid" in slots else None,
                ask=_read_old_frame(slots["ask"]) if "ask" in slots else None,
            )
            try:
                merged = _merge_side_frames(side_frames)
            except (ValueError, KeyError) as exc:
                report.skipped.append(f"{source}: {symbol}/{timeframe}: {exc}")
                report.per_source[source]["skipped"] += 1
                continue
            label = f"{source}/{symbol}/{timeframe}" + (f"_{segment}" if segment else "")
            if dry_run:
                report.written.append(f"{label} (dry-run)")
                report.per_source[source]["written"] += 1
                continue
            try:
                ohlcv_store.write_frame(
                    data_root,
                    source,
                    symbol,
                    timeframe,
                    merged,
                    asset_class=str(target["asset_class"]),
                    region=str(target["region"]),
                    segment=segment,
                )
            except (ValueError, OSError) as exc:
                report.skipped.append(f"{source}: {symbol}/{timeframe}: {exc}")
                report.per_source[source]["skipped"] += 1
                continue
            report.written.append(label)
            report.per_source[source]["written"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", required=True, help="old ohlcv root ({source}/{asset_class}/...)")
    parser.add_argument(
        "--dst",
        required=True,
        help="new ohlcv root (<data>/ohlcv) or its data dir",
    )
    parser.add_argument(
        "--source", action="append", default=[], help="limit to this source (repeatable)"
    )
    parser.add_argument(
        "--skip-source",
        action="append",
        default=[],
        help="skip this source (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    report = migrate(
        args.src,
        args.dst,
        sources=args.source or None,
        skip_sources=args.skip_source,
        dry_run=args.dry_run,
    )
    for line in report.summary_lines():
        print(line)
    for label in report.written:
        print(f"  wrote: {label}")
    return 1 if report.collisions else 0


if __name__ == "__main__":
    sys.exit(main())
