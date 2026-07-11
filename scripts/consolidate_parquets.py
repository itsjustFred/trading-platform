from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve()
    if _data_dir_env
    else (PROJECT_ROOT / "data").resolve()
)

FRAGMENT_SUFFIX = re.compile(r"(?:_|-)(?:part|chunk|fragment)\d+$", re.IGNORECASE)
KNOWN_TIMEFRAMES = {"tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "mn1"}
TIMEFRAME_PATTERN = re.compile(r"^(?:tick|s1|mn1|\d+[smhdw])$", re.IGNORECASE)


def _normalize_timeframe(stem: str, symbol: str) -> str:
    cleaned = FRAGMENT_SUFFIX.sub("", stem).lower()
    if cleaned in KNOWN_TIMEFRAMES or TIMEFRAME_PATTERN.match(cleaned):
        return cleaned
    if cleaned == symbol.lower():
        return "1d"
    return cleaned


def _normalize_frame(df: pd.DataFrame, fragment_path: Path | None = None) -> pd.DataFrame:
    normalized = df.copy()
    if "timestamp" in normalized.columns:
        before = len(normalized)
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
        normalized = normalized.dropna(subset=["timestamp"])
        dropped = before - len(normalized)
        if dropped:
            label = str(fragment_path) if fragment_path is not None else "<unknown>"
            print(f"Dropped {dropped} row(s) with unparsable timestamps from fragment {label}")
    return normalized


def _canonical_target(parquet_path: Path) -> Path:
    symbol = parquet_path.parent.name
    timeframe = _normalize_timeframe(parquet_path.stem, symbol)
    return parquet_path.parent / f"{timeframe}.parquet"


def _fragment_sort_key(path):
    """Natural-numeric sort key so 'part2' precedes 'part10'."""
    text = str(path)
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", text)]


def consolidate(root: Path) -> tuple[int, int]:
    groups: dict[Path, list[Path]] = {}
    for parquet_path in root.rglob("*.parquet"):
        target = _canonical_target(parquet_path)
        groups.setdefault(target, []).append(parquet_path)

    merged_groups = 0
    deleted_fragments = 0

    for target_path, members in sorted(groups.items(), key=lambda item: str(item[0])):
        unique_members = sorted(set(members), key=_fragment_sort_key)
        if len(unique_members) == 1 and unique_members[0] == target_path:
            continue

        frames = []
        for member in unique_members:
            frame = pd.read_parquet(member)
            frames.append(_normalize_frame(frame, member))

        combined = pd.concat(frames, ignore_index=True, sort=False)
        if "timestamp" in combined.columns:
            combined = (
                combined.drop_duplicates(subset=["timestamp"], keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
        else:
            combined = combined.drop_duplicates().reset_index(drop=True)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(target_path, index=False)
        merged_groups += 1

        for member in unique_members:
            if member == target_path:
                continue
            member.unlink()
            deleted_fragments += 1

    return merged_groups, deleted_fragments


def main() -> None:
    ohlcv_root = DATA_DIR / "ohlcv"
    if not ohlcv_root.exists():
        print(f"No OHLCV directory found at {ohlcv_root}")
        return

    merged_groups, deleted_fragments = consolidate(ohlcv_root)
    print(
        "Consolidation complete:",
        f"merged_groups={merged_groups}",
        f"deleted_fragments={deleted_fragments}",
        f"root={ohlcv_root}",
    )


if __name__ == "__main__":
    main()
