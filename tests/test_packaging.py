from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from live_runner import account_discovery
from scripts.sync_requirements_from_lock import requirements_drift

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_declares_every_runtime_package() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = set(project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
    assert packages == {
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
    }


def test_runtime_imports_have_direct_dependencies() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = {
        re.split(r"[<>=!~ ;\[]", value, maxsplit=1)[0].lower()
        for value in project["project"]["dependencies"]
    }
    assert {
        "beautifulsoup4",
        "filelock",
        "lxml",
        "pyyaml",
        "python-dotenv",
        "scipy",
        "sqlalchemy",
        "starlette",
        "tzdata",
    } <= names
    ctrader = {
        re.split(r"[<>=!~ ;\[]", value, maxsplit=1)[0].lower()
        for value in project["project"]["optional-dependencies"]["ctrader"]
    }
    assert "protobuf" in ctrader


def test_base_import_does_not_require_ctrader_extra() -> None:
    code = """
import sys
class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ctrader_open_api' or fullname.startswith('ctrader_open_api.'):
            raise ModuleNotFoundError(fullname)
        return None
sys.meta_path.insert(0, Blocker())
import live_runner
import live_runner.account_discovery
assert 'ctrader_open_api' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_verify_script_import_has_no_broker_side_effects() -> None:
    code = """
import runpy, sys
runpy.run_path('scripts/verify_ctrader_order.py', run_name='verify_import')
assert 'twisted' not in sys.modules
assert 'ctrader_open_api' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ctrader_unavailable_is_local_to_discovery(monkeypatch, tmp_path: Path) -> None:
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        "CTRADER_APP_ID=app\nCTRADER_APP_SECRET=secret\nCTRADER_ACCESS_TOKEN=token\n",
        encoding="utf-8",
    )

    def unavailable() -> None:
        raise account_discovery.CTraderUnavailableError("optional extra missing")

    monkeypatch.setattr(account_discovery, "_load_ctrader_protocol", unavailable)
    assert asyncio.run(account_discovery.discover_configured_accounts(accounts_env)) == []


def test_no_unix_only_lock_import_at_module_scope() -> None:
    for relative in (
        "live_runner/ctrader_adapter.py",
        "scripts/matrix_queue_runner.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        module_imports = [
            node
            for node in tree.body
            if isinstance(node, ast.Import)
            and any(alias.name == "fcntl" for alias in node.names)
        ]
        assert module_imports == []


def test_requirements_match_locked_export() -> None:
    assert requirements_drift(ROOT) is None


def test_verify_report_writer_is_atomic_and_private(tmp_path: Path) -> None:
    path = ROOT / "scripts" / "verify_ctrader_order.py"
    spec = importlib.util.spec_from_file_location("verify_ctrader_order_packaging", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out_path = tmp_path / "report.json"
    module._write_report_atomic({"verified": True}, out_path)
    assert json.loads(out_path.read_text(encoding="utf-8")) == {"verified": True}
    if os.name != "nt":
        assert stat.S_IMODE(out_path.stat().st_mode) == 0o600


def test_live_deployment_file_is_not_tracked_and_example_is_symbolic() -> None:
    assert not (ROOT / "live_deployments.yaml").exists()
    example = (ROOT / "live_deployments.example.yaml").read_text(encoding="utf-8")
    assert "${ALPACA_PAPER_ACCOUNT_REFERENCE}" in example
    assert "${CTRADER_DEMO_CTID}" in example
    assert re.search(r"\bPA[0-9A-Z]{10,}\b", example) is None
