"""Endpoint-level pins for path containment (proposal G26).

Each test records the status or error a viewer endpoint returns *today* for a
rejected path, so consolidating the hand-written checks onto
:func:`shared.paths.safe_join` cannot quietly change what a caller sees. The
helper's own table lives in ``tests/test_shared_paths.py``; the pins for the
asset/backtest/portfolio/strategy/matrix/data-file routes already live in
``tests/test_viewer_path_containment.py`` and
``tests/test_viewer_matrix_result_path.py``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import viewer.main as viewer_app_module


@pytest.fixture
def symlink_or_skip():
    def _make(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
            pytest.skip(f"symlinks unavailable on this platform: {exc}")

    return _make


# ---------------------------------------------------------------------------
# Static SPA middleware: a symlinked asset must not be served from outside
# ---------------------------------------------------------------------------


def test_static_middleware_does_not_serve_a_symlink_escape(
    viewer_client, tmp_path, monkeypatch, symlink_or_skip
):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>index</html>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-MARKER", encoding="utf-8")
    symlink_or_skip(static_dir / "leak.js", secret)
    symlink_or_skip(static_dir / "leak.txt", secret)
    symlink_or_skip(static_dir / "leakpage", secret)
    monkeypatch.setattr(viewer_app_module, "STATIC_DIR", static_dir)

    served = viewer_client.get("/app.js")
    assert served.status_code == 200
    assert "console.log('ok')" in served.text

    # A static asset suffix falls through to the router (404), never the file.
    for path in ("/leak.js", "/leak.txt"):
        escaped_asset = viewer_client.get(path)
        assert escaped_asset.status_code == 404
        assert "SECRET-MARKER" not in escaped_asset.text

    # Anything else falls back to index.html, not the symlink target.
    escaped_page = viewer_client.get("/leakpage")
    assert escaped_page.status_code == 200
    assert "SECRET-MARKER" not in escaped_page.text


# ---------------------------------------------------------------------------
# /api/logs/{log_name}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("log_name", ["../secret.log", ".."])
def test_log_endpoint_rejects_a_traversing_name(viewer_client, tmp_path, log_name):
    (tmp_path / "secret.log").write_text("SECRET-MARKER", encoding="utf-8")

    with pytest.raises(viewer_app_module.HTTPException) as exc_info:
        asyncio.run(viewer_app_module.get_log(log_name))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Access denied"


def test_log_endpoint_rejects_a_nul_byte_with_400(viewer_client, tmp_path):
    with pytest.raises(viewer_app_module.HTTPException) as exc_info:
        asyncio.run(viewer_app_module.get_log("x\x00y.log"))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid log path"


# ---------------------------------------------------------------------------
# Task log files
# ---------------------------------------------------------------------------


def test_task_log_path_accepts_a_symlinked_task_logs_dir(
    viewer_client, tmp_path, monkeypatch, symlink_or_skip
):
    """The log directory may be reached through a symlink (deployment shape)."""
    real = tmp_path / "real-task-logs"
    real.mkdir()
    link = tmp_path / "task-logs"
    symlink_or_skip(link, real, target_is_directory=True)
    monkeypatch.setattr(viewer_app_module, "TASK_LOGS_DIR", link)

    assert viewer_app_module._validated_task_log_path("task-1234abcd") == (
        real / "task_task-1234abcd.log"
    ).resolve()


def test_task_log_path_rejects_a_symlinked_log_file(
    viewer_client, tmp_path, monkeypatch, symlink_or_skip
):
    task_logs = tmp_path / "task-logs"
    task_logs.mkdir()
    monkeypatch.setattr(viewer_app_module, "TASK_LOGS_DIR", task_logs)
    secret = tmp_path / "secret.log"
    secret.write_text("SECRET-MARKER", encoding="utf-8")
    symlink_or_skip(task_logs / "task_task-1234abcd.log", secret)

    with pytest.raises(viewer_app_module.HTTPException) as exc_info:
        viewer_app_module._validated_task_log_path("task-1234abcd")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid task log path"


# ---------------------------------------------------------------------------
# Strategy file paths (codegen gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_file",
    ["/etc/passwd", "../escape.py", "..", "", "   "],
)
def test_codegen_gate_strategy_path_rejects_escapes(monkeypatch, tmp_path, relative_file):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    monkeypatch.setattr(viewer_app_module, "STRATEGIES_DIR", strategies_dir)

    with pytest.raises(viewer_app_module.HTTPException) as exc_info:
        viewer_app_module._codegen_gate_strategy_path(relative_file)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid strategy file path"


def test_codegen_gate_strategy_path_rejects_a_nul_byte_with_400(monkeypatch, tmp_path):
    """A NUL byte used to reach ``resolve()`` and raise an unhandled ValueError.

    The route answered 500 for a request value it should have rejected with its
    documented 400; this test fails on the unfixed code.
    """
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    monkeypatch.setattr(viewer_app_module, "STRATEGIES_DIR", strategies_dir)

    with pytest.raises(viewer_app_module.HTTPException) as exc_info:
        viewer_app_module._codegen_gate_strategy_path("a\x00b.py")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid strategy file path"


# ---------------------------------------------------------------------------
# Asset files by id (backtests / portfolios / optimizations)
# ---------------------------------------------------------------------------


def test_contained_asset_path_accepts_a_symlinked_base(tmp_path, symlink_or_skip):
    """The data directory may be reached through a symlink (deployment shape)."""
    real = tmp_path / "backtests"
    real.mkdir()
    (real / "abc.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "linked-backtests"
    symlink_or_skip(link, real, target_is_directory=True)

    assert viewer_app_module._contained_asset_path(link, "abc", ".json") == (
        real / "abc.json"
    ).resolve()


def test_contained_asset_path_rejects_a_symlinked_file_below_the_base(
    tmp_path, symlink_or_skip
):
    base = tmp_path / "backtests"
    base.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    symlink_or_skip(base / "abc.json", secret)

    assert viewer_app_module._contained_asset_path(base, "abc", ".json") is None


# ---------------------------------------------------------------------------
# Data files (client-supplied path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "x\x00y.csv", str(Path(os.sep) / "etc" / "passwd")])
def test_contained_client_data_file_rejects_unusable_paths(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)

    assert (
        viewer_app_module._contained_client_data_file(raw, allowed_suffix=".csv") is None
    )


def test_contained_client_data_file_resolves_a_relative_path_from_cwd(
    monkeypatch, tmp_path
):
    """A relative client path is cwd-relative, not DATA_DIR-relative.

    The viewer's cwd is not the data directory, so a relative value never
    resolved inside it and was refused. Anchoring it at ``DATA_DIR`` instead
    would accept ``ohlcv/x.parquet`` on four routes.
    """
    data_dir = tmp_path / "data"
    (data_dir / "ohlcv").mkdir(parents=True)
    (data_dir / "ohlcv" / "x.parquet").write_text("", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(viewer_app_module, "DATA_DIR", data_dir)
    monkeypatch.chdir(elsewhere)

    assert (
        viewer_app_module._contained_client_data_file(
            "ohlcv/x.parquet", allowed_suffix=".parquet"
        )
        is None
    )


def test_contained_client_data_file_accepts_a_dotdot_that_resolves_inside(
    monkeypatch, tmp_path
):
    """An absolute path with a ``..`` that resolves inside was accepted before."""
    data_dir = tmp_path / "data"
    (data_dir / "ohlcv").mkdir(parents=True)
    target = data_dir / "ohlcv" / "x.parquet"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(viewer_app_module, "DATA_DIR", data_dir)
    with_dotdot = str(data_dir / "ohlcv" / ".." / "ohlcv" / "x.parquet")

    assert (
        viewer_app_module._contained_client_data_file(with_dotdot, allowed_suffix=".parquet")
        == target.resolve()
    )
