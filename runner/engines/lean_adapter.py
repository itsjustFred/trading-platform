"""
LEAN Engine Adapter — runs strategies through QuantConnect LEAN.

This adapter bridges to existing LEAN infrastructure:
1. Writes a backtest_config.json
2. Invokes LEAN (via subprocess or the lean CLI)
3. Reads the LEAN output JSON
4. Converts to universal format via lean_converter

For Phase 1, this is a thin wrapper. Phase 2 will add Docker-based LEAN runs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from runner.converters.lean_converter import convert_lean_result
from runner.engines.base import BacktestConfig, EngineAdapter, ProgressCallback
from shared.models import BacktestResult

logger = logging.getLogger(__name__)

LEAN_PROJECT_DIR = Path(
    os.environ.get(
        "LEAN_PROJECT_DIR",
        Path.home() / "Desktop" / "custom_lean_integration",
    )
)


class LeanAdapter(EngineAdapter):
    """Adapter for QuantConnect LEAN backtesting engine."""

    @property
    def name(self) -> str:
        return "lean"

    def __init__(self, lean_project_dir: str | Path | None = None):
        self.project_dir = Path(lean_project_dir) if lean_project_dir else LEAN_PROJECT_DIR

    def run(
        self,
        config: BacktestConfig,
        strategy_path: str,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        """
        Run a LEAN backtest.

        strategy_path should point to a LEAN algorithm file or a config JSON
        in the custom_lean_integration project.
        """
        # For Phase 1: if strategy_path points to an existing LEAN result JSON,
        # just convert it (no actual LEAN execution yet)
        sp = Path(strategy_path)
        if sp.suffix == ".json" and sp.exists():
            logger.info("Converting existing LEAN result: %s", sp)
            return convert_lean_result(sp, strategy_name=config.strategy_name)

        # Otherwise, try to run LEAN
        lean_config = self._write_config(config, strategy_path)
        output_dir = self._run_lean(lean_config)
        result_file = self._find_result(output_dir)

        if result_file is None:
            raise RuntimeError(f"No LEAN result found in {output_dir}")

        return convert_lean_result(result_file, strategy_name=config.strategy_name)

    def _write_config(self, config: BacktestConfig, strategy_path: str) -> Path:
        """Write LEAN backtest config JSON."""
        cfg = {
            "algorithm": strategy_path,
            "symbols": config.symbols,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_capital": config.initial_capital,
            "parameters": config.parameters,
        }
        config_path = self.project_dir / "backtest_config.json"
        config_path.write_text(json.dumps(cfg, indent=2))
        logger.info("Wrote LEAN config: %s", config_path)
        return config_path

    def _run_lean(self, config_path: Path) -> Path:
        """Execute LEAN backtest via the ``lean`` CLI console script."""
        output_dir = self.project_dir / "output" / datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir.mkdir(parents=True, exist_ok=True)

        lean_bin = shutil.which("lean")
        if not lean_bin:
            raise RuntimeError(
                "LEAN CLI not found on PATH. Install with: pip install lean\n"
                "Or pass an existing LEAN result JSON as strategy_path."
            )

        cmd = [
            lean_bin,
            "backtest",
            str(config_path),
            "--output",
            str(output_dir),
        ]

        logger.info("Running LEAN: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(self.project_dir),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"LEAN backtest timed out after {exc.timeout} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "LEAN CLI not found. Install with: pip install lean\n"
                "Or pass an existing LEAN result JSON as strategy_path."
            ) from exc
        if result.returncode != 0:
            logger.error("LEAN failed:\n%s\n%s", result.stdout, result.stderr)
            raise RuntimeError(
                f"LEAN exited with code {result.returncode}: "
                f"{(result.stderr or result.stdout or '')[-2000:]}"
            )

        return output_dir

    def _find_result(self, output_dir: Path) -> Path | None:
        """Find the LEAN result JSON in output directory."""
        for pattern in ["*.json", "**/*.json"]:
            for f in output_dir.glob(pattern):
                try:
                    data = json.loads(f.read_text())
                    if "orders" in data or "trades" in data:
                        return f
                except (json.JSONDecodeError, KeyError):
                    continue
        return None
