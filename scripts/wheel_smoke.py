from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ENTRYPOINT = "viewer.main:app"
DEEP_IMPORTS = (
    "data_tools",
    "live_runner",
    "risk",
    "runner",
    "scheduler",
    "shared",
    "signal_bots",
    "strategies",
    "validation",
    "viewer",
    "live_runner.account_discovery",
    "viewer.core",
    "runner.cli",
    "validation.cli",
    "data_tools.catalog_builder",
    "live_runner.daemon",
    "shared.logging",
)
PACKAGE_ENTRIES = (
    "data_tools/__init__.py",
    "data_tools/catalog_builder.py",
    "live_runner/__init__.py",
    "live_runner/account_discovery.py",
    "live_runner/daemon.py",
    "risk/__init__.py",
    "runner/__init__.py",
    "runner/cli.py",
    "scheduler/__init__.py",
    "shared/__init__.py",
    "shared/logging.py",
    "signal_bots/__init__.py",
    "strategies/__init__.py",
    "validation/__init__.py",
    "validation/cli.py",
    "viewer/__init__.py",
    "viewer/main.py",
    "viewer/core.py",
)
RESOURCE_ENTRIES = (
    "data_tools/config/downloader_profiles.json",
    "data_tools/config/dukascopy_genesis.json",
    "data_tools/config/dukascopy_instruments.json",
    "signal_bots/nitro/config.yaml",
    "signal_bots/ramjet/config.yaml",
    "signal_bots/rocketfuel/config.yaml",
    "strategies/pynecore_orb/pynecore_orb.toml",
    "strategies/pynecore_sma_crossover/pynecore_sma_crossover.toml",
    "viewer/static/index.html",
)


def _wheel_path(value: str | None) -> Path:
    if value:
        path = Path(value)
    else:
        candidates = sorted((ROOT / "dist").glob("*.whl"))
        if len(candidates) != 1:
            raise RuntimeError("expected exactly one wheel in dist")
        path = candidates[0]
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def smoke(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("wheel contains a corrupt member")
        names = set(archive.namelist())
        missing = [name for name in (*PACKAGE_ENTRIES, *RESOURCE_ENTRIES) if name not in names]
        if missing:
            raise RuntimeError(f"wheel is missing: {', '.join(missing)}")
        with tempfile.TemporaryDirectory() as temp_dir:
            archive.extractall(temp_dir)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            code = (
                "import importlib, importlib.resources, sys\n"
                "sys.path.insert(0, sys.argv.pop(1))\n"
                "from fastapi import FastAPI\n"
                f"entrypoint_spec = {APPLICATION_ENTRYPOINT!r}\n"
                "entrypoint_module, entrypoint_attribute = entrypoint_spec.split(':', 1)\n"
                "entrypoint = getattr(\n"
                "    importlib.import_module(entrypoint_module), entrypoint_attribute\n"
                ")\n"

                "assert isinstance(entrypoint, FastAPI)\n"
                "assert entrypoint.routes\n"
                f"for name in {DEEP_IMPORTS!r}:\n"
                "    importlib.import_module(name)\n"
                "assert 'ctrader_open_api' not in sys.modules\n"
                "assert importlib.resources.files('data_tools').joinpath("
                "'config/dukascopy_genesis.json').is_file()\n"
                "assert importlib.resources.files('signal_bots').joinpath("
                "'nitro/config.yaml').is_file()\n"
                "assert importlib.resources.files('viewer').joinpath("
                "'static/index.html').is_file()\n"
            )
            subprocess.run(
                [sys.executable, "-E", "-c", code, temp_dir],
                cwd=temp_dir,
                env=env,
                check=True,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the built wheel.")
    parser.add_argument("wheel", nargs="?")
    args = parser.parse_args(argv)
    smoke(_wheel_path(args.wheel))
    print("wheel import smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
