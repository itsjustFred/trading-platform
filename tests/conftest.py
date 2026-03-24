from __future__ import annotations

import os
from pathlib import Path

# Keep test runs deterministic regardless of machine/user HOME resolution.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_DIR = (PROJECT_ROOT / "data").resolve()

os.environ.setdefault("DATA_DIR", str(TEST_DATA_DIR))
os.environ.setdefault("TRADING_DATA_DIR", str(TEST_DATA_DIR))
