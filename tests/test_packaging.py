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
import zipfile
from pathlib import Path

import pytest

from live_runner import account_discovery
from scripts import verify_ctrader_order, wheel_smoke
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


def test_wheel_smoke_imports_account_discovery_and_propagates_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel_path = tmp_path / "trading_platform-0.1.0-py3-none-any.whl"
    entries = (*wheel_smoke.PACKAGE_ENTRIES, *wheel_smoke.RESOURCE_ENTRIES)
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, b"")
    observed_command: list[str] = []
    observed_code = ""

    def checked_run(command: list[str], *, check: bool, **_kwargs):
        nonlocal observed_command, observed_code
        observed_command = command
        observed_code = command[-2]
        if not check:
            return subprocess.CompletedProcess(command, returncode=1)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(wheel_smoke.subprocess, "run", checked_run)

    with pytest.raises(subprocess.CalledProcessError):
        wheel_smoke.smoke(wheel_path)

    assert "-E" in observed_command
    assert wheel_smoke.APPLICATION_ENTRYPOINT in observed_code
    assert "'live_runner.account_discovery'" in observed_code
    assert repr(wheel_smoke.DEEP_IMPORTS) in observed_code


def test_wheel_smoke_propagates_missing_account_discovery_import(tmp_path: Path) -> None:
    wheel_path = tmp_path / "trading_platform-0.1.0-py3-none-any.whl"
    entries = (*wheel_smoke.PACKAGE_ENTRIES, *wheel_smoke.RESOURCE_ENTRIES)
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, b"")

    with pytest.raises(subprocess.CalledProcessError):
        wheel_smoke.smoke(wheel_path)


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


def test_verify_report_writer_fsyncs_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_fsync = verify_ctrader_order.os.fsync
    real_replace = verify_ctrader_order.os.replace

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(verify_ctrader_order.os, "fsync", tracked_fsync)
    monkeypatch.setattr(verify_ctrader_order.os, "replace", tracked_replace)

    verify_ctrader_order._write_report_atomic(
        {"verified": True},
        tmp_path / "report.json",
    )

    assert events == ["fsync", "replace"]


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_verify_report_writer_removes_temp_file_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    if failure_point == "fsync":

        def fail_fsync(_fd: int) -> None:
            raise OSError("fsync failed")

        monkeypatch.setattr(verify_ctrader_order.os, "fsync", fail_fsync)
    else:

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(verify_ctrader_order.os, "replace", fail_replace)

    out_path = tmp_path / "report.json"
    out_path.write_text('{"verified": false}\n', encoding="utf-8")

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        verify_ctrader_order._write_report_atomic({"verified": True}, out_path)

    assert out_path.read_text(encoding="utf-8") == '{"verified": false}\n'
    assert list(tmp_path.glob(f".{out_path.name}.*.tmp")) == []


def test_verify_report_writer_closes_unbound_descriptor_and_temp_file_on_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, object] = {}
    closed_fds: list[int] = []
    real_mkstemp = verify_ctrader_order.tempfile.mkstemp
    real_close = verify_ctrader_order.os.close

    def tracked_mkstemp(*args, **kwargs):
        fd, temp_name = real_mkstemp(*args, **kwargs)
        created["fd"] = fd
        created["path"] = Path(temp_name)
        return fd, temp_name

    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("chmod failed")

    def tracked_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(verify_ctrader_order.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(verify_ctrader_order.os, "chmod", fail_chmod)
    monkeypatch.setattr(verify_ctrader_order.os, "close", tracked_close)

    with pytest.raises(OSError, match="chmod failed"):
        verify_ctrader_order._write_report_atomic({"verified": True}, tmp_path / "report.json")

    temp_path = created["path"]
    assert isinstance(temp_path, Path)
    assert created["fd"] in closed_fds
    assert not temp_path.exists()


def test_verify_report_writer_cleans_temp_file_after_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Interrupted(BaseException):
        pass

    def interrupt_replace(_source: Path, _destination: Path) -> None:
        raise _Interrupted

    monkeypatch.setattr(verify_ctrader_order.os, "replace", interrupt_replace)
    out_path = tmp_path / "report.json"
    out_path.write_text('{"verified": false}\n', encoding="utf-8")

    with pytest.raises(_Interrupted):
        verify_ctrader_order._write_report_atomic({"verified": True}, out_path)

    assert out_path.read_text(encoding="utf-8") == '{"verified": false}\n'
    assert list(tmp_path.glob(f".{out_path.name}.*.tmp")) == []


def test_verify_report_writer_preserves_primary_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "report.json"
    out_path.write_text('{"verified": false}\n', encoding="utf-8")
    temp_path: Path | None = None
    cleanup_attempts: list[Path] = []
    real_unlink = Path.unlink

    def fail_replace(source: Path, _destination: Path) -> None:
        nonlocal temp_path
        temp_path = source
        raise OSError("replace failed")

    def fail_temp_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".tmp":
            cleanup_attempts.append(path)
            raise OSError("cleanup failed")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(verify_ctrader_order.os, "replace", fail_replace)
        patch.setattr(Path, "unlink", fail_temp_unlink)
        with pytest.raises(OSError, match="replace failed"):
            verify_ctrader_order._write_report_atomic({"verified": True}, out_path)

    assert temp_path is not None
    assert cleanup_attempts == [temp_path]
    assert out_path.read_text(encoding="utf-8") == '{"verified": false}\n'
    real_unlink(temp_path)


def test_live_deployment_file_is_not_tracked_and_example_is_symbolic() -> None:
    assert not (ROOT / "live_deployments.yaml").exists()
    example = (ROOT / "live_deployments.example.yaml").read_text(encoding="utf-8")
    assert "${ALPACA_PAPER_ACCOUNT_REFERENCE}" in example
    assert "${CTRADER_DEMO_CTID}" in example
    assert re.search(r"\bPA[0-9A-Z]{10,}\b", example) is None
