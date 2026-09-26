"""Tests for data_tools.migrate_csv backup/force semantics (B3)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from data_tools.migrate_csv import _convert_ohlcv, _source_fingerprint


def _csv(path: Path, rows: int = 3) -> Path:
    path.write_text(
        "Datetime,Open,High,Low,Close,Volume\n"
        + "\n".join(f"2024-01-0{d},100,101,99,100.5,1000" for d in range(1, rows + 1)),
        encoding="utf-8",
    )
    return path


def test_force_backs_up_previous_artifact(tmp_path: Path) -> None:
    src = _csv(tmp_path / "a.csv")
    dest = tmp_path / "a.parquet"
    assert _convert_ohlcv(src, dest, False) == 1

    _csv(tmp_path / "a.csv", rows=5)
    assert _convert_ohlcv(src, dest, False) == 0  # skip without force
    assert _convert_ohlcv(src, dest, False, force=True) == 1

    backups = list(tmp_path.glob("a.bak.*.parquet"))
    assert len(backups) == 1
    assert len(pd.read_parquet(backups[0])) == 3
    assert len(pd.read_parquet(dest)) == 5


def test_source_fingerprint_reports_hash_and_rows(tmp_path: Path) -> None:
    src = _csv(tmp_path / "a.csv")
    fingerprint = _source_fingerprint(src)
    assert fingerprint.startswith("sha256:")
    assert "rows~3" in fingerprint
