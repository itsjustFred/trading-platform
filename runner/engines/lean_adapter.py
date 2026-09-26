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
from datetime import datetime
from pathlib import Path

from runner.agent_process import build_backtest_child_env
from runner.approval_gate import require_generated_strategy_approval
from runner.converters.lean_converter import convert_lean_result
from runner.engines.base import BacktestConfig, EngineAdapter, ProgressCallback
from shared.atomic_io import write_text_atomically
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

        strategy_path must point to a LEAN algorithm source file. Existing
        result JSON is imported through :meth:`import_result`.
        """
        sp = Path(strategy_path)
        if sp.suffix.lower() == ".json":
            raise ValueError(
                "LEAN result JSON cannot be used as strategy_path; call "
                "import_result() with the result path instead."
            )
        require_generated_strategy_approval(strategy_path)

        lean_config = self._write_config(config, strategy_path)
        output_dir = self._run_lean(lean_config)
        result_file = self._find_result(output_dir)

        if result_file is None:
            raise RuntimeError(f"No LEAN result found in {output_dir}")

        return self.import_result(result_file, config=config)

    def import_result(
        self,
        result_path: str | Path,
        *,
        config: BacktestConfig | None = None,
        strategy_name: str | None = None,
    ) -> BacktestResult:
        path = Path(result_path)
        if path.suffix.lower() != ".json":
            raise ValueError("LEAN import_result requires a JSON result path")
        if not path.is_file():
            raise FileNotFoundError(f"LEAN result file not found: {path}")
        resolved_name = strategy_name or (config.strategy_name if config is not None else None)
        logger.info("Converting existing LEAN result: %s", path)
        return convert_lean_result(path, strategy_name=resolved_name)

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
        write_text_atomically(config_path, json.dumps(cfg, indent=2, ensure_ascii=False))
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
                "Or import an existing LEAN result with import_result()."
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
                env=build_backtest_child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"LEAN backtest timed out after {exc.timeout} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "LEAN CLI not found. Install with: pip install lean\n"
                "Or import an existing LEAN result with import_result()."
            ) from exc
        if result.returncode != 0:
            logger.error("LEAN failed:\n%s\n%s", result.stdout, result.stderr)
            raise RuntimeError(
                f"LEAN exited with code {result.returncode}: "
                f"{(result.stderr or result.stdout or '')[-2000:]}"
            )

        return output_dir

    def _find_result(self, output_dir: Path) -> Path | None:
        """Find the LEAN result JSON in output directory (C2).

        First JSON shaped like a result wins — deterministically (sorted),
        requiring a dict with `orders`/`trades` when present. LEAN writes
        orders dict-shaped (keyed by order id); lists are accepted too —
        matching what :mod:`runner.converters.lean_converter` consumes.
        Returns None (caller fails loudly) when nothing validates.
        """
        for pattern in ["*.json", "**/*.json"]:
            for f in sorted(output_dir.glob(pattern)):
                try:
                    data = json.loads(f.read_text())
                except (json.JSONDecodeError, KeyError, OSError):
                    continue
                if not isinstance(data, dict):
                    continue
                if "orders" not in data and "trades" not in data:
                    continue
                orders = data.get("orders", [])
                trades = data.get("trades", [])
                if not isinstance(orders, (list, dict)) or not isinstance(
                    trades, (list, dict)
                ):
                    continue
                logger.info("Using LEAN result file: %s", f)
                return f
        return None
