"""The suite must stay inside its sandbox.

In the main checkout this repo is the live platform: ``data/`` is its database and market data,
``logs/`` is the log read during a live incident, and ``.accounts.env`` / ``.env`` hold the real
broker and Discord credentials. A test that resolves any of those reads or writes production state,
and - worse - a test that *passes* because of a real credential is a result nobody can reproduce.

``conftest`` therefore points every one of those at a throwaway session directory and fails the run
at session end if the suite escaped. These tests pin both halves, plus the two guards that have no
directory to watch: the network must stay on localhost, and the integration suite must not discover
over HTTP while it is being imported or collected.
"""

from __future__ import annotations

import ast
import importlib
import io
import os
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import dotenv
import pytest

import tests.conftest as suite_conftest
from tests.conftest import (
    ISOLATED_ACCOUNTS_ENV,
    ISOLATED_DOTENV_FILE,
    REPO_ACCOUNTS_ENV,
    REPO_DATA_DIR,
    REPO_ENV_FILE,
    REPO_LOG_DIR,
    TEST_DATA_DIR,
    TEST_LOG_DIR,
    diff_snapshots,
    sandboxed_env,
    snapshot_tree,
)


def _is_under(path: Path, root: Path) -> bool:
    return path.resolve() == root or root in path.resolve().parents


def _called_name(node: ast.AST) -> str:
    """The bare name of a call target, e.g. ``_discover_and_register_discord_services``."""
    if isinstance(node, ast.Call):
        target = node.func
    else:  # pragma: no cover - only calls are passed today
        target = node
    if isinstance(target, ast.Attribute):
        return target.attr
    return getattr(target, "id", "")


class _Config:  # no ``workerinput`` attribute: behaves like the controller
    pass


class _Session:
    config = _Config()
    exitstatus = pytest.ExitCode.OK


# --- the watched directories ------------------------------------------------------------------ #


def test_the_watched_repo_directories_are_data_and_logs() -> None:
    """``logs/`` was not watched before, and a run wrote 18 files into the live one."""
    assert [Path(root).name for root in suite_conftest.WATCHED_REPO_DIRS] == ["data", "logs"]


def test_env_data_dir_is_not_the_repo_data_dir() -> None:
    for var in ("DATA_DIR", "TRADING_DATA_DIR"):
        resolved = Path(os.environ[var]).resolve()
        assert not _is_under(resolved, REPO_DATA_DIR), f"{var} points into the repo data dir"


def test_env_log_dir_is_not_the_repo_log_dir() -> None:
    resolved = Path(os.environ["LOG_DIR"]).resolve()
    assert not _is_under(resolved, REPO_LOG_DIR), "LOG_DIR points into the repo logs dir"


def test_env_data_dir_is_the_conftest_session_dir() -> None:
    """Forced, not ``setdefault``: a production DATA_DIR exported in the shell must not survive."""
    assert Path(os.environ["DATA_DIR"]).resolve() == TEST_DATA_DIR


def test_env_log_dir_is_the_conftest_session_dir() -> None:
    """Same rule as DATA_DIR: the viewer's LOG_DIR must not survive into the suite."""
    assert Path(os.environ["LOG_DIR"]).resolve() == TEST_LOG_DIR


def test_platform_constants_resolve_outside_the_repo_data_and_log_dirs() -> None:
    import shared.constants as constants
    import shared.database as database
    import shared.logging as shared_logging
    import viewer.main as viewer

    for name, path in (
        ("constants.DATA_DIR", constants.DATA_DIR),
        ("constants.OPTIMIZATIONS_DIR", constants.OPTIMIZATIONS_DIR),
        ("constants.LOG_DIR", constants.LOG_DIR),
        ("shared.logging.LOG_DIR", shared_logging.LOG_DIR),
        ("viewer.LOG_DIR", viewer.LOG_DIR),
        ("viewer.TASK_LOGS_DIR", viewer.TASK_LOGS_DIR),
        ("database.DATA_DIR", database.DATA_DIR),
        ("database.DB_PATH", database.DB_PATH),
    ):
        assert not _is_under(Path(path), REPO_DATA_DIR), f"{name} points into the repo data dir"
        assert not _is_under(Path(path), REPO_LOG_DIR), f"{name} points into the repo log dir"


def test_portfolio_optimizer_resolves_optimizations_dir_from_data_dir() -> None:
    """Issue #315: portfolio_optimizer must not hardcode ``<repo>/data/optimizations``."""
    import runner.portfolio_optimizer as portfolio_optimizer
    import shared.constants as constants

    assert portfolio_optimizer.OPTIMIZATIONS_DIR == constants.OPTIMIZATIONS_DIR
    assert not _is_under(portfolio_optimizer.OPTIMIZATIONS_DIR, REPO_DATA_DIR)


def test_sessionfinish_fails_the_run_when_a_watched_repo_dir_changed(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Both watched directories, not just ``data/``: writing the live-runner log is the same lie."""
    fake_data = tmp_path / "data"
    fake_data.mkdir()
    fake_logs = tmp_path / "logs"
    fake_logs.mkdir()
    watched = {str(fake_data): snapshot_tree(fake_data), str(fake_logs): snapshot_tree(fake_logs)}
    monkeypatch.setattr(suite_conftest, "_REPO_TREES_BEFORE", watched)

    session = _Session()
    suite_conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.OK

    (fake_logs / "live-runner.log").write_text("submitted order AAPL qty=1", encoding="utf-8")
    suite_conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    err = capsys.readouterr().err
    assert str(fake_logs) in err and "created: live-runner.log" in err
    # The guard cannot attribute the change to the suite, so the message must not claim it did.
    assert "the test session modified" not in err
    assert "cannot tell which" in err


def test_the_directory_guard_is_not_run_by_an_xdist_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker would snapshot a tree its siblings are still writing into.

    The other guards do run in workers: a worker is a separate process in which the tests
    really ran, so a violation recorded there must still fail the run - through ``workeroutput``,
    because xdist ignores a worker's ``session.exitstatus``.
    """

    class _WorkerConfig:
        workerinput = {"id": "gw0"}
        workeroutput: dict[str, object] = {}

    class _WorkerSession:
        config = _WorkerConfig()
        exitstatus = pytest.ExitCode.OK

    watched = tmp_path / "data"
    watched.mkdir()
    (watched / "written-during-the-session.txt").write_text("x", encoding="utf-8")
    original = suite_conftest._REPO_TREES_BEFORE
    suite_conftest._REPO_TREES_BEFORE = {str(watched): {}}
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", ["connect 'api.alpaca.markets'"])
    try:
        session = _WorkerSession()
        suite_conftest.pytest_sessionfinish(session)
    finally:
        suite_conftest._REPO_TREES_BEFORE = original
    # The directory change is not the worker's business...
    assert session.exitstatus == pytest.ExitCode.OK
    # ...but the network violation is reported to the controller, not dropped.
    reported = session.config.workeroutput["tp_violations"]
    assert any("attempted to reach the network" in message for message in reported)


def test_an_xdist_workers_violations_reach_the_controller_sessionfinish(
    monkeypatch, capsys
) -> None:
    """``pytest_testnodedown`` is the only path a worker's findings have to the controller."""
    monkeypatch.setattr(suite_conftest, "_WORKER_VIOLATIONS", [])
    node = types.SimpleNamespace(
        workeroutput={
            "tp_violations": ["\n\nERROR: the test session attempted to reach the network"]
        }
    )
    suite_conftest.pytest_testnodedown(node, None)

    session = _Session()
    suite_conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "attempted to reach the network" in capsys.readouterr().err


def test_snapshot_diff_reports_created_modified_and_deleted(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("a")
    (tmp_path / "change.txt").write_text("a")
    (tmp_path / "gone.txt").write_text("a")
    before = snapshot_tree(tmp_path)

    (tmp_path / "change.txt").write_text("longer")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("x")

    assert diff_snapshots(before, snapshot_tree(tmp_path)) == [
        "created: sub",
        "created: sub/new.txt",
        "deleted: gone.txt",
        "modified: change.txt",
    ]


def test_snapshot_of_an_unchanged_or_missing_tree_is_clean(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("a")
    assert diff_snapshots(snapshot_tree(tmp_path), snapshot_tree(tmp_path)) == []
    assert snapshot_tree(tmp_path / "missing") == {}


# --- .accounts.env ---------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("module_name", "attribute"), suite_conftest.ACCOUNTS_ENV_MODULES, ids=lambda case: str(case)
)
def test_every_accounts_env_module_points_at_the_session_sandbox(
    module_name: str, attribute: str
) -> None:
    """Every copy, not just the one a test happens to import.

    Each of these modules resolves the checkout's real credential file, and a copy imported into
    another module (or into a function's default argument) binds the value at import time.
    """
    module = importlib.import_module(module_name)
    value = Path(getattr(module, attribute))
    assert value == ISOLATED_ACCOUNTS_ENV, f"{module_name}.{attribute} is {value}"
    assert not _is_under(value, REPO_ACCOUNTS_ENV.parent)
    # A file that exists here would be the same leak in a different directory: a test that needs
    # credentials writes its own under tmp_path.
    assert not value.exists(), f"{module_name}.{attribute} resolves to an existing file"


def test_the_resolver_defaults_are_not_frozen_at_the_repo_path() -> None:
    """The defect that made the leak possible: a default *argument* binds at import time.

    ``def f(accounts_env=DEFAULT_ACCOUNTS_ENV)`` keeps the path the module resolved when it was
    imported, so patching the module constant afterwards leaves the signature reading the real
    file. These three entry points therefore resolve at call time instead.
    """
    import inspect

    import live_runner.account_discovery as account_discovery
    import live_runner.daemon as daemon
    import live_runner.preflight as preflight

    for module, function_name, parameter in (
        (account_discovery, "discover_configured_accounts", "accounts_env"),
        (preflight, "run_live_preflight", "accounts_env_path"),
        (daemon, "sync_services_loop", "accounts_env"),
        (daemon, "sync_deployments_loop", "accounts_env"),
        (daemon, "run_daemon", "accounts_env"),
    ):
        default = inspect.signature(getattr(module, function_name)).parameters[parameter].default
        assert default is None, f"{module.__name__}.{function_name} froze a default: {default!r}"


def test_the_suite_drops_an_inherited_accounts_env_override() -> None:
    """Removed, not ``setdefault``: an exported TP_ACCOUNTS_ENV names a real credential file, and
    nothing in the suite may read one."""
    assert suite_conftest.ACCOUNTS_ENV_ENV_VAR not in os.environ
    # The one redirect that stays in play is the roster of module constants, so a test that patches
    # a module default still changes what the code reads.
    assert os.environ["DATA_DIR"] == str(TEST_DATA_DIR)


def test_the_credential_sweep_knows_every_integration_prefix() -> None:
    """Every broker, chat and monitoring family the platform integrates with.

    A var under a prefix this tuple does not know survives the sweep and reaches tests,
    where a test could pass against a real account. The families are pinned here so a
    removal fails loudly; a new integration adds its own in the same change that reads it.
    """
    assert set(suite_conftest._EXTERNAL_ENV_NAMESPACES) == {
        "ALPACA_",
        "CTRADER_",
        "DISCORD_",
        "SENTRY_",
    }


def test_no_allowlist_prefix_can_carry_a_renamed_token() -> None:
    """The CI context is allowed by exact name, so a renamed token has no way in.

    ``GITHUB_TOKEN`` is dropped by the credential sweep; a renamed token (``GITHUB_PAT``)
    would not match its marker, so the prefix that would carry it is not allowed at all -
    the allowed names are the context variables the tests and the workflow steps read.
    """
    # Only the two true prefixes may be prefix-matched at all: every other allowed name is
    # matched exactly, so GITHUB_REPOSITORY_EXTRA cannot ride in on GITHUB_REPOSITORY's stem.
    assert set(suite_conftest._SANDBOX_ENV_ALLOWLIST_PREFIXES) == {"LC_", "PYTEST_XDIST"}
    assert not suite_conftest._is_allowlisted_env("GITHUB_REPOSITORY_EXTRA")
    assert suite_conftest._is_allowlisted_env("GITHUB_REPOSITORY")
    assert "GITHUB_TOKEN" in suite_conftest._EXTERNAL_ENV_NAMES or (
        suite_conftest._is_external_credential_env("GITHUB_TOKEN")
    )
    assert not suite_conftest._is_external_credential_env("GITHUB_REPOSITORY")


@pytest.mark.parametrize(
    "name",
    [
        "TP_ACCOUNTS_ENV",
        "DATABASE_URL",
        "ALPACA_API_KEY",
        "ALPACA_INTRADAY_START",
        "CTRADER_APP_SECRET",
        "DISCORD_BOT_RAMJET_TOKEN",
        "DISCORD_TRUSTED_AUTHOR_IDS",
        "SENTRY_DSN",
        "GITHUB_TOKEN",
        "NIM_API_KEY",
    ],
)
def test_external_credential_shaped_env_vars_are_cleared(name: str) -> None:
    assert suite_conftest._is_external_credential_env(name) is True
    assert name not in os.environ


@pytest.mark.parametrize(
    "name",
    [
        "DATA_DIR",
        "TRADING_DATA_DIR",
        "LOG_DIR",
        "TP_INTEGRATION_BASE_URL",
        "TP_TEST_OHLCV_DIR",
        "TRADING_PRODUCTION_DB",
        "PATH",
    ],
)
def test_suite_configuration_env_vars_are_not_credential_shaped(name: str) -> None:
    assert suite_conftest._is_external_credential_env(name) is False


def test_no_external_credential_env_var_is_set() -> None:
    """The autouse fixture cleared before this test; a direct ``os.environ`` write cannot leak."""
    leaked = [
        name for name in os.environ if suite_conftest._is_external_credential_env(name)
    ]
    assert leaked == []


def test_the_module_default_is_used_when_nothing_is_named(tmp_path: Path) -> None:
    """With no named path the resolver returns the caller's default - and nothing else.

    There is deliberately no environment override: an env var honoured by only some readers gave
    two components different views of the accounts file. A reintroduced ``TP_ACCOUNTS_ENV`` read
    here would fail this test.
    """
    from live_runner.credential_env import resolve_accounts_env_path

    module_default = tmp_path / ".accounts.env"
    assert resolve_accounts_env_path(default=module_default) == module_default
    os.environ["TP_ACCOUNTS_ENV"] = "/somewhere/else/.accounts.env"
    try:
        assert resolve_accounts_env_path(default=module_default) == module_default
    finally:
        os.environ.pop("TP_ACCOUNTS_ENV", None)


def test_a_caller_named_accounts_env_is_used_verbatim(tmp_path: Path) -> None:
    """A named path is the caller's own configuration: the default must not replace it."""
    from live_runner.credential_env import resolve_accounts_env_path

    named = tmp_path / "named.accounts.env"
    assert resolve_accounts_env_path(named, default=named) == named


def test_the_sandbox_accounts_env_carries_no_credentials() -> None:
    """The outcome the isolation exists for: no test can pass because of a real account.

    The file view is asserted, not the overlaid one: ``load_accounts_env_values`` also fills keys
    the file does not define from the process environment, which is not the checkout's.
    """
    from live_runner.credential_env import load_accounts_env_values, read_accounts_env_file
    from live_runner.service_discovery import discover_discord_bots_from_env

    assert read_accounts_env_file(ISOLATED_ACCOUNTS_ENV) == {}
    assert load_accounts_env_values(ISOLATED_ACCOUNTS_ENV, environ={}) == {}
    # Discovery reads the patched constant, so it sees no bot the checkout could have configured.
    assert discover_discord_bots_from_env() == []


def test_no_imported_module_still_resolves_the_real_accounts_env() -> None:
    offenders = suite_conftest._accounts_env_readers()
    assert offenders == [], (
        "these modules still resolve the real .accounts.env: " + ", ".join(offenders)
    )


def test_the_reader_sweep_detects_a_planted_reference(monkeypatch) -> None:
    """The positive control: a sweep with nothing to find must be able to find something."""
    planted = types.ModuleType("planted_accounts_env_leak")
    planted.DEFAULT_ACCOUNTS_ENV = REPO_ACCOUNTS_ENV
    planted.SOME_ACCOUNTS_ENV_NOTE = REPO_ACCOUNTS_ENV.parent / "notes.txt"
    monkeypatch.setitem(sys.modules, planted.__name__, planted)
    try:
        assert suite_conftest._accounts_env_readers() == [
            "planted_accounts_env_leak.DEFAULT_ACCOUNTS_ENV"
        ]
    finally:
        sys.modules.pop(planted.__name__, None)


def test_sessionfinish_fails_the_run_when_a_module_resolves_the_real_accounts_env(
    monkeypatch, capsys
) -> None:
    planted = types.ModuleType("planted_sessionfinish_leak")
    planted.DEFAULT_ACCOUNTS_ENV = str(REPO_ACCOUNTS_ENV)
    monkeypatch.setitem(sys.modules, planted.__name__, planted)
    try:
        session = _Session()
        suite_conftest.pytest_sessionfinish(session)
    finally:
        sys.modules.pop(planted.__name__, None)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "still resolve the real" in capsys.readouterr().err


# --- .env ------------------------------------------------------------------------------------- #


def test_the_repo_dotenv_file_is_redirected_to_the_session_file(
    monkeypatch, tmp_path: Path
) -> None:
    """``shared.constants`` and ``viewer.main`` load ``PROJECT_ROOT/.env`` at import time.

    Nothing can intervene before that load, so it is redirected rather than prevented: the call
    still happens, on the empty session file. An unrelated path must reach the real loader.
    """
    loaded: list[tuple[Path | None, object]] = []

    def _record(path=None, stream=None, *_args, **_kwargs):
        target = path if path is not None else _kwargs.get("dotenv_path")
        loaded.append((Path(target) if target is not None else None, stream))
        return {}

    monkeypatch.setattr(suite_conftest, "_real_load_dotenv", _record)
    dotenv.load_dotenv(REPO_ENV_FILE)
    dotenv.load_dotenv(dotenv_path=REPO_ENV_FILE)
    dotenv.load_dotenv(tmp_path / "unrelated.env")
    assert loaded == [
        (ISOLATED_DOTENV_FILE, None),
        (ISOLATED_DOTENV_FILE, None),
        (tmp_path / "unrelated.env", None),
    ]


def test_a_dotenv_stream_of_none_still_redirects_discovery(monkeypatch) -> None:
    """``stream=None`` is not a stream: discovery still has to find the redirected file."""
    loaded: list[Path | None] = []

    def _record(path=None, stream=None, *_args, **_kwargs):
        loaded.append(Path(path) if path is not None else None)
        return {}

    monkeypatch.setattr(suite_conftest, "_real_load_dotenv", _record)
    monkeypatch.setattr(dotenv, "find_dotenv", lambda usecwd: str(REPO_ENV_FILE))

    dotenv.load_dotenv(stream=None)
    dotenv.load_dotenv(None, None)

    assert loaded == [ISOLATED_DOTENV_FILE, ISOLATED_DOTENV_FILE]


def test_a_supplied_dotenv_stream_skips_file_discovery(monkeypatch) -> None:
    """A real stream is the caller's file: no path discovery and no redirect."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    def _no_discovery(*_args, **_kwargs):
        raise AssertionError("file discovery must not run for a supplied stream")

    monkeypatch.setattr(suite_conftest, "_real_load_dotenv", _record)
    monkeypatch.setattr(dotenv, "find_dotenv", _no_discovery)
    stream = io.StringIO("X=1\n")

    dotenv.load_dotenv(stream=stream)

    assert calls == [((None, stream), {})]


def test_a_symlink_to_a_secret_file_is_matched_by_its_target(
    monkeypatch, tmp_path: Path
) -> None:
    """A link is caught by what it points at, not by the name someone gave it."""
    link = tmp_path / "innocent-name"
    link.symlink_to(REPO_ACCOUNTS_ENV)
    assert suite_conftest._repo_secret_path(link) == REPO_ACCOUNTS_ENV
    # An attempt is refused *and* recorded, so the deliberate one below must not reach the
    # session-level guard.
    monkeypatch.setattr(suite_conftest, "_REPO_SECRET_OPENS", [])
    with pytest.raises(suite_conftest.SuiteHermeticityViolation, match=r"\.accounts.env"):
        link.read_text(encoding="utf-8")



def test_the_session_dotenv_substitute_is_empty() -> None:
    """If it held anything, the suite would run against invented configuration."""
    assert ISOLATED_DOTENV_FILE.read_text(encoding="utf-8") == ""


def test_the_secret_file_matcher_only_names_the_real_files(tmp_path: Path) -> None:
    """The positive control for the read guard: same file name, different tree, no match."""
    assert suite_conftest._repo_secret_path(REPO_ACCOUNTS_ENV) == REPO_ACCOUNTS_ENV
    assert suite_conftest._repo_secret_path(REPO_ENV_FILE) == REPO_ENV_FILE
    assert suite_conftest._repo_secret_path(f"{REPO_ACCOUNTS_ENV}") == REPO_ACCOUNTS_ENV
    assert suite_conftest._repo_secret_path(REPO_ACCOUNTS_ENV.as_uri()) is None
    assert suite_conftest._repo_secret_path(tmp_path / ".accounts.env") is None
    assert suite_conftest._repo_secret_path(tmp_path / ".env") is None
    assert suite_conftest._repo_secret_path(REPO_ACCOUNTS_ENV.with_name("accounts.env")) is None
    assert suite_conftest._repo_secret_path(3) is None
    assert suite_conftest._repo_secret_path(None) is None


def test_the_secret_file_matcher_is_case_insensitive_on_case_insensitive_file_systems(
    monkeypatch,
) -> None:
    """On NTFS and default APFS ``.ENV`` *is* ``.env``: the same file under another spelling.

    The early name check must not wave a differently-cased name through, or the guard misses the
    open on exactly the platforms where the casing cannot protect the file. The platform flag is
    patched rather than skipped on because the comparison logic is what is under test, and it must
    hold wherever the flag says the file system folds case.
    """
    monkeypatch.setattr(suite_conftest, "_CASE_INSENSITIVE_FS", True)
    differently_cased = REPO_ENV_FILE.with_name(".ENV")
    assert suite_conftest._repo_secret_path(differently_cased) is not None
    monkeypatch.setattr(suite_conftest, "_CASE_INSENSITIVE_FS", False)
    assert suite_conftest._repo_secret_path(differently_cased) is None


def test_sessionfinish_fails_the_run_when_a_repo_secret_file_was_opened(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(suite_conftest, "_REPO_SECRET_OPENS", [str(REPO_ACCOUNTS_ENV)])
    session = _Session()
    suite_conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    err = capsys.readouterr().err
    assert "tried to open the checkout's real credential file" in err
    assert "write its own under tmp_path" in err


def test_no_repo_secret_file_has_been_touched_so_far() -> None:
    """The live half of the guard, asserted at the point the suite is furthest along.

    The session-end check is the one that fails a run; this makes the same assertion inside a test,
    so a leak introduced earlier in this very module fails here with the offending paths.
    """
    assert suite_conftest.repo_secret_opens() == ()


# --- the integration suite must not discover over HTTP while importing ------------------------ #


def test_the_integration_module_names_no_default_port() -> None:
    """A hardcoded port is what made a bare ``pytest`` talk to a live viewer."""
    source = (Path(__file__).resolve().parents[1] / "tests" / "integration").joinpath(
        "test_strategy_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "TP_INTEGRATION_BASE_URL" in source
    assert "5556" not in source, "the integration module still carries a default port"
    assert "_discover_strategy_matrix" in source
    assert "pytest_generate_tests" not in source, (
        "discovery must wait for test setup: collection runs even for deselected tests"
    )


# --- the network ------------------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "::1", "127.0.0.2", "0.0.0.0", "::", None],
)
def test_a_local_address_is_not_a_network_violation(host: str | None) -> None:
    assert suite_conftest._is_local_host(host) is True


@pytest.mark.parametrize(
    "host", ["api.alpaca.markets", "35.221.23.121", "live.ctrader.com", "example.com", "10.0.0.1"]
)
def test_a_remote_address_is_a_network_violation(host: str) -> None:
    assert suite_conftest._is_local_host(host) is False


def test_a_loopback_name_lookup_is_allowed() -> None:
    # Real resolver call: the guard must not stand between the suite and localhost.
    socket.getaddrinfo("localhost", 80)


def test_a_loopback_connect_is_allowed_through_to_the_os(monkeypatch) -> None:
    """The guard stops the *network*, not localhost: a real localhost connect still succeeds."""
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        probe = socket.socket()
        try:
            probe.connect(("127.0.0.1", port))
            accepted, _ = listener.accept()
            accepted.close()
        finally:
            probe.close()
    finally:
        listener.close()
    assert suite_conftest.network_violations() == ()


def test_a_remote_name_lookup_is_refused(monkeypatch, request) -> None:
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    with pytest.raises(suite_conftest.SuiteHermeticityViolation, match=r"api\.alpaca\.markets"):
        socket.getaddrinfo("api.alpaca.markets", 443)
    assert suite_conftest.network_violations() == (
        f"name lookup 'api.alpaca.markets' during test {request.node.nodeid} "
        f"(thread {threading.current_thread().name})",
    )


def test_a_remote_connect_is_refused(monkeypatch, request) -> None:
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    probe = socket.socket()
    try:
        with pytest.raises(suite_conftest.SuiteHermeticityViolation, match=r"35\.221\.23\.121"):
            probe.connect(("35.221.23.121", 443))
    finally:
        probe.close()
    assert suite_conftest.network_violations() == (
        f"connect '35.221.23.121' during test {request.node.nodeid} "
        f"(thread {threading.current_thread().name})",
    )


def test_a_unix_socket_connect_is_not_a_network_violation(monkeypatch) -> None:
    """A filesystem socket has no network in it, and pytest's own transports use one."""
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    probe = socket.socket(socket.AF_UNIX)
    try:
        with pytest.raises(OSError):  # no such socket, refused by the OS and not by the guard
            probe.connect("/definitely/not/a/socket.sock")
    finally:
        probe.close()
    assert suite_conftest.network_violations() == ()


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.gethostbyname("api.alpaca.markets"),
        lambda: socket.gethostbyaddr(b"193.123.232.1"),
        lambda: socket.getnameinfo(("35.221.23.121", 443), 0),
        lambda: socket.socket().sendto(b"x", ("35.221.23.121", 443)),
    ],
    ids=["gethostbyname", "gethostbyaddr", "getnameinfo", "sendto"],
)
def test_a_datagram_or_bare_lookup_is_refused_too(monkeypatch, call) -> None:
    """UDP and a DNS-only lookup reach the network without a ``connect``."""
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    with pytest.raises(suite_conftest.SuiteHermeticityViolation):
        call()
    assert len(suite_conftest.network_violations()) == 1


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg is Unix-only")
def test_a_remote_sendmsg_is_refused(monkeypatch) -> None:
    """``sendmsg`` carries its destination in a different argument shape than ``sendto``."""
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(suite_conftest.SuiteHermeticityViolation):
            probe.sendmsg([b"x"], (), 0, ("35.221.23.121", 443))
    finally:
        probe.close()
    assert len(suite_conftest.network_violations()) == 1


@pytest.mark.parametrize("host", [b"localhost", b"127.0.0.1"])
def test_a_byte_form_local_name_stays_local(host: bytes) -> None:
    assert suite_conftest._is_local_host(host) is True


@pytest.mark.parametrize("host", [b"api.alpaca.markets", b"35.221.23.121"])
def test_a_byte_form_remote_name_is_still_refused(host: bytes) -> None:
    assert suite_conftest._is_local_host(host) is False


def test_a_network_violation_names_the_test_that_attempted_it(
    monkeypatch, request
) -> None:
    """The guard must attribute a refused socket call to a test node id (#381).

    The session-finish report is the only place a swallowed
    ``SuiteHermeticityViolation`` surfaces, so the recorded entry has to carry
    enough context to find the offender without a bisect.
    """
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    monkeypatch.setattr(suite_conftest, "_CURRENT_TEST_NODEID", request.node.nodeid)
    with pytest.raises(suite_conftest.SuiteHermeticityViolation):
        socket.getaddrinfo("platform.dukascopy.com", 443)
    (violation,) = suite_conftest.network_violations()
    assert "platform.dukascopy.com" in violation
    assert request.node.nodeid in violation


def _consume_stub_hits(edge: str, nodeid: str) -> list[str]:
    """Remove and return the stub hits a test deliberately triggered.

    The session-end failure exists to expose *accidental* leaks; a test that
    exercises the blocked edge on purpose must not leave its own hit behind.
    Only hits recorded on the calling thread are consumed: a leaked worker
    thread firing during this test is attributed to this node id too, and it
    must stay behind to fail the session.
    """
    hits = suite_conftest._DUKASCOPY_STUB_HITS
    thread_label = f"(thread {threading.current_thread().name})"
    mine = [
        hit
        for hit in hits
        if edge in hit and nodeid in hit and thread_label in hit
    ]
    hits[:] = [hit for hit in hits if hit not in mine]
    return mine


def test_jforex_classpath_resolution_cannot_reach_the_jnlp_endpoint(
    monkeypatch, tmp_path: Path, request
) -> None:
    """The autouse stub turns an accidental JNLP bootstrap into a test failure (#381).

    With no ``JFOREX_*`` override and no local jars, classpath resolution falls
    through to ``_download_jforex_jars_from_jnlp``; the stubbed ``urlopen``
    must stop it before any socket is opened.
    """
    from data_tools.downloaders import jforex_dl

    monkeypatch.setattr(jforex_dl, "DATA_DIR", tmp_path)
    for name in ("JFOREX_CLASSPATH", "JFOREX_JARS", "JFOREX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "DUKASCOPY_JNLP_URL", "https://platform.dukascopy.com/demo/jforex.jnlp"
    )

    with pytest.raises(RuntimeError, match="must not fetch JForex SDK jars"):
        jforex_dl._resolve_jforex_classpath_details()

    (hit,) = _consume_stub_hits("jforex_dl.urlopen", request.node.nodeid)
    assert request.node.nodeid in hit


def test_dukascopy_native_fetch_is_stubbed_in_tests(request) -> None:
    """``_fetch_one`` is the single aiohttp edge to ``datafeed.dukascopy.com``."""
    import asyncio

    from data_tools.downloaders import dukascopy_native as native

    with pytest.raises(RuntimeError, match="must not fetch Dukascopy"):
        asyncio.run(
            native._fetch_one(
                None, None, "https://datafeed.dukascopy.com/datafeed/x", None
            )
        )

    (hit,) = _consume_stub_hits("dukascopy_native._fetch_one", request.node.nodeid)
    assert request.node.nodeid in hit


def test_every_daemon_discovery_call_names_the_resolved_accounts_env() -> None:
    """The file the daemon resolved must reach every discovery call.

    ``run_daemon`` and the credential-reload loop each dispatch discovery on a worker thread. A
    dispatch that omits the file falls back to the module default, which an operator who redirected
    the accounts env has already said is not the file to use. Read from the AST because
    ``run_daemon`` is an unbounded loop: its first statement cannot be driven and then stopped.
    """
    tree = ast.parse((_PROJECT_ROOT / "live_runner" / "daemon.py").read_text(encoding="utf-8"))
    dispatches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == "to_thread"
    ]
    discovery = [
        call
        for call in dispatches
        if any(
            ast.unparse(argument) == "_discover_and_register_discord_services"
            for argument in call.args
        )
    ]
    assert discovery, "live_runner/daemon.py never dispatches Discord service discovery"
    for call in discovery:
        arguments = [ast.unparse(argument) for argument in call.args]
        position = arguments.index("_discover_and_register_discord_services")
        following = arguments[position + 1 :]
        assert following, f"{ast.unparse(call)} names no accounts env"
        assert following[0] == "accounts_env", ast.unparse(call)


def test_the_daemon_discovers_bots_from_the_file_it_was_given(tmp_path, monkeypatch) -> None:
    """Discovery must not read the module default when the daemon resolved another file.

    Otherwise an operator who redirected the accounts env (or named one on the command line) still
    gets bots from the checkout's default, which is the file they said not to use.
    """
    import live_runner.daemon as daemon
    import live_runner.service_discovery as service_discovery

    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        "DISCORD_BOT_RAMJET_TOKEN=token\nDISCORD_BOT_RAMJET_CHANNEL_ID=1431465140905185452\n",
        encoding="utf-8",
    )
    # The daemon hands the file it resolved to discovery, rather than letting discovery pick its
    # own. Patched in the daemon's namespace, where the name is imported, so this asserts the call
    # and not the reader.
    monkeypatch.setattr(daemon, "list_services", lambda *args, **kwargs: [])
    monkeypatch.setattr(daemon, "create_service", lambda service: service)
    seen: list[Path | None] = []

    def _discover(accounts_env=None):
        seen.append(Path(accounts_env) if accounts_env is not None else None)
        return []

    monkeypatch.setattr(daemon, "discover_discord_bots_from_env", _discover)
    daemon._discover_and_register_discord_services(accounts_env)
    assert seen == [accounts_env]

    # ... and the reader itself uses the file it is handed, not the module default. The real
    # function is called, so a discovery that ignored its argument reads the (empty) sandbox path.
    monkeypatch.undo()
    discovered = service_discovery.discover_discord_bots_from_env(accounts_env)
    assert discovered == [
        {
            "name": "RAMJET",
            "token_env_key": "DISCORD_BOT_RAMJET_TOKEN",
            "channel_id": "1431465140905185452",
        }
    ]
    # The same call through the module default finds nothing: that path is the session sandbox.
    assert service_discovery.discover_discord_bots_from_env() == []


def test_sessionfinish_fails_the_run_when_the_network_was_reached(monkeypatch, capsys) -> None:
    """A guard that only raises is swallowed by the first ``except Exception`` it meets."""
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", ["connect 'api.alpaca.markets'"])
    session = _Session()
    suite_conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "attempted to reach the network" in capsys.readouterr().err


def test_no_network_violation_has_been_recorded_so_far() -> None:
    violations = suite_conftest.network_violations()
    assert violations == (), (
        "the suite already reached outside localhost: " + ", ".join(violations)
    )


# --- the integration suite must not discover over HTTP while importing ------------------------ #


def test_the_integration_module_makes_no_connection_while_importing() -> None:
    """Discovery at import time ran during collection, deselected or not.

    A bare ``pytest`` (no ``-m "not slow"``) therefore POSTed real backtest runs to whatever
    viewer was listening on the default port. Discovery now needs TP_INTEGRATION_BASE_URL and
    happens inside a fixture, so importing the module must touch no socket at all.
    """
    source = (
        "import socket\n"
        "attempts = []\n"
        "real_connect = socket.socket.connect\n"
        "real_lookup = socket.getaddrinfo\n"
        "def _connect(self, address):\n"
        "    attempts.append(('connect', address))\n"
        "    raise OSError('blocked')\n"
        "def _lookup(host, *rest):\n"
        "    attempts.append(('lookup', host))\n"
        "    raise OSError('blocked')\n"
        "socket.socket.connect = _connect\n"
        "socket.getaddrinfo = _lookup\n"
        "import tests.integration.test_strategy_pipeline as module\n"
        "print('ATTEMPTS', attempts)\n"
        "print('IMPORTED', module.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env=sandboxed_env(TP_INTEGRATION_BASE_URL="http://127.0.0.1:5556"),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ATTEMPTS []" in result.stdout, result.stdout + result.stderr
    assert "IMPORTED tests.integration.test_strategy_pipeline" in result.stdout


def test_the_integration_module_is_skipped_without_the_opt_in() -> None:
    """No default port: an unset TP_INTEGRATION_BASE_URL skips the module instead of guessing."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/integration/test_strategy_pipeline.py",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=300,
        # The child runs the repo's suite as its own pytest process, so it builds its
        # own sandbox instead of sharing this session.
        env=sandboxed_env(shares_sandbox=False),
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "TP_INTEGRATION_BASE_URL is not set" in combined, combined
    assert "1 skipped" in combined, combined


def test_a_deselected_integration_test_makes_no_http_call() -> None:
    """``-m "not slow"`` still collects the module; it must not still discover over HTTP."""
    import http.server
    import threading

    hits: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _record(self) -> None:
            hits.append(self.path)
            body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _record
        do_POST = _record

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                "not slow",
                "-p",
                "no:cacheprovider",
                "tests/integration/test_strategy_pipeline.py",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            # Same as the opt-in case above: a pytest child builds its own sandbox.
            env=sandboxed_env(TP_INTEGRATION_BASE_URL=base_url, shares_sandbox=False),
            timeout=300,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    combined = result.stdout + result.stderr
    assert hits == [], combined
    assert "deselected" in combined, combined


# --- the inherited environment and the children a test spawns ---------------------------------- #


def test_a_parent_env_var_outside_the_allowlist_never_reaches_a_test(tmp_path: Path) -> None:
    """S16: the sandbox clears to an allowlist, so an inherited variable is gone.

    The sandbox used to clear only credential-shaped names. Everything else a
    developer exported (or a CI runner set) reached every test and every child a
    test spawned, and a name the allowlist does not carry has no meaning inside
    the suite: either a test sets it itself, or nothing may observe it. The child
    is a real pytest process, so this pin covers the sweep itself and not only
    the helper a test would use.
    """
    result = _run_throwaway_suite(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def test_sees_no_inherited_variable():\n"
        "    assert 'TP_SANDBOX_PROBE' not in os.environ\n"
        "def test_home_is_the_session_sandbox():\n"
        "    session_root = Path(os.environ['DATA_DIR']).parent\n"
        "    assert Path(os.environ['HOME']) == session_root / 'home'\n",
        env_extra={"TP_SANDBOX_PROBE": "leaked"},
    )

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module_name",
    ["live_runner.credential_env", "scripts.live_smoke"],
    ids=lambda case: str(case),
)
def test_a_subprocess_resolves_the_accounts_env_into_the_session_sandbox(module_name: str) -> None:
    """S16: a child resolves the accounts env to the session sandbox, not the checkout.

    The sandbox patches the module constant in this process only, so a child
    imported the real value and would read the checkout's real credential file.
    The path is printed, never read, so the pin holds in a checkout with or
    without that file. Both packages that define the constant today are pinned,
    because a child's guard wraps modules by file, not by package name.
    """
    from tests.conftest import sandboxed_env

    body = (
        f"import {module_name}\n"
        f"print({module_name}.DEFAULT_ACCOUNTS_ENV)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    resolved = Path(result.stdout.strip())
    assert resolved == ISOLATED_ACCOUNTS_ENV, (
        f"a child resolved the accounts env to {resolved}, not the session sandbox"
        f" {ISOLATED_ACCOUNTS_ENV}"
    )
    assert not resolved.exists(), "the session sandbox file must not exist"


def test_a_child_guard_wraps_every_first_party_module() -> None:
    """The child's accounts-env guard is not a package list.

    The constant lives in ``live_runner`` and ``scripts`` today; a guard that only
    wraps those two packages would resolve the checkout's real path for a constant
    added in another package tomorrow. A module from a third package is pinned here:
    its loader in a sandboxed child is the guard's wrapper, so a future constant in
    that file is patched the day it appears.
    """
    from tests.conftest import sandboxed_env

    expected = suite_conftest._ChildAccountsEnvLoader.__name__
    body = (
        "import shared.constants\n"
        "print('LOADER', type(shared.constants.__loader__).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"LOADER {expected}" in result.stdout, (
        f"a child did not wrap a first-party module outside live_runner/scripts: "
        f"{result.stdout}{result.stderr}"
    )


def test_a_subprocess_does_not_load_the_repo_dotenv() -> None:
    """S16: a child importing ``shared.constants`` loads the session substitute.

    ``shared.constants`` loads ``<repo>/.env`` at import time. This process
    redirects that load; a child had no redirect and filled its environment from
    the checkout's real file - broker credentials included. The redirect is what
    the child reports, so the pin names the loader a sandboxed child gets rather
    than the raw library call.
    """
    from tests.conftest import sandboxed_env

    body = (
        "import dotenv\n"
        "print('LOADER', dotenv.load_dotenv.__name__)\n"
        "import shared.constants\n"
        "print('IMPORTED', bool(shared.constants.DATA_DIR))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IMPORTED True" in result.stdout, result.stdout + result.stderr
    assert "LOADER _redirected_load_dotenv" in result.stdout, (
        "a child loads .env through the raw loader: " + result.stdout + result.stderr
    )


def test_a_caller_sitecustomize_runs_after_the_guard(tmp_path: Path) -> None:
    """A caller-provided ``sitecustomize`` runs, but never before the child's guard.

    The shim is first on the child's PYTHONPATH so a stray one cannot pre-empt the audit
    hook, the ``.env`` redirect or the accounts-env guard; a caller that provides one on
    purpose (the golden run's fixture tree) still gets it, run after the guard is installed.
    """
    from tests.conftest import sandboxed_env

    tree = tmp_path / "caller-tree"
    tree.mkdir()
    (tree / "sitecustomize.py").write_text(
        "print('CALLER-SITECUSTOMIZE-RAN')\n",
        encoding="utf-8",
    )
    body = (
        "import dotenv\n"
        "print('LOADER', dotenv.load_dotenv.__name__)\n"
        "import shared.constants\n"
        "print('IMPORTED', bool(shared.constants.DATA_DIR))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(PYTHONPATH=str(tree)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CALLER-SITECUSTOMIZE-RAN" in result.stdout, (
        "a caller sitecustomize did not run in the sandboxed child: "
        + result.stdout
        + result.stderr
    )
    assert "LOADER _redirected_load_dotenv" in result.stdout, (
        "a caller sitecustomize ran before the guard: " + result.stdout + result.stderr
    )
    assert "IMPORTED True" in result.stdout, result.stdout + result.stderr


def test_a_child_keeps_the_overrides_its_parent_named() -> None:
    """The child's own sweeps keep what the naming parent handed it.

    ``SENTRY_*`` is credential-shaped, so the child's credential sweep drops it - and a
    ``SENTRY_RELEASE`` a test names for its child (the golden run does) is the test's own
    fixture value, not a developer's credential. A sweep that dropped it sent the child
    into git detection to find a release, which is a subprocess the child's own guards
    then refuse.
    """
    from tests.conftest import sandboxed_env

    os.environ["SENTRY_DSN"] = "dummy-not-a-real-dsn"
    try:
        child_env = sandboxed_env(SENTRY_RELEASE="golden-fixture-release")
    finally:
        os.environ.pop("SENTRY_DSN", None)
    # The unnamed one never reaches the child at all: the parent-side construction is the
    # first sweep, and this is the variable the credential guard exists for.
    assert "SENTRY_DSN" not in child_env

    body = "import os\nprint('RELEASE', os.environ.get('SENTRY_RELEASE'))\n"
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RELEASE golden-fixture-release" in result.stdout, (
        "a child dropped an override its parent named: " + result.stdout + result.stderr
    )


def test_a_child_guard_covers_the_checkout_through_any_spelling(tmp_path: Path) -> None:
    """The child's wrap follows the file, not the spelling of the path it was found through.

    A child reaches the checkout through whatever path entry its PYTHONPATH names - a
    symlink to the repo, an editable install - and a prefix comparison alone would miss
    those, leaving a module's accounts-env constant resolved against the checkout.
    """
    from tests.conftest import sandboxed_env

    link = tmp_path / "checkout-link"
    link.symlink_to(_PROJECT_ROOT, target_is_directory=True)
    expected = suite_conftest._ChildAccountsEnvLoader.__name__
    body = (
        "import shared.constants\n"
        "print('LOADER', type(shared.constants.__loader__).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(PYTHONPATH=str(link)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"LOADER {expected}" in result.stdout, (
        f"a child importing through a symlinked checkout root was not guarded: "
        f"{result.stdout}{result.stderr}"
    )


def test_sandboxed_env_marks_and_names_a_sharing_child_together() -> None:
    """The marker and the session directory are one act of ``sandboxed_env()``.

    A child that was marked but not named - or the reverse - would take the wrong branch
    of the sandbox for a reason nobody can see, so the pair is pinned here as well as
    asserted at child startup.
    """
    sharing = suite_conftest.sandboxed_env()
    assert sharing[suite_conftest.CHILD_SANDBOX_ENV_VAR] == suite_conftest.CHILD_SANDBOX_FLAG_VALUE
    assert sharing["TP_TEST_SESSION_ROOT"] == str(suite_conftest._SESSION_ROOT)
    own = suite_conftest.sandboxed_env(shares_sandbox=False)
    assert suite_conftest.CHILD_SANDBOX_ENV_VAR not in own
    assert "TP_TEST_SESSION_ROOT" not in own


def test_no_allowlist_entry_is_credential_shaped() -> None:
    """The allowlist must not become a way past the credential sweep.

    An entry added to make a test pass that spells like a credential - a token, a key, a
    DSN, a broker's prefix - would hand every test and every child the variable the sweep
    exists to remove, and a test could then pass against a real account. The documented
    entries are paths and flags; this fails the day one of them stops being one.
    """
    entries = (
        *suite_conftest._SANDBOX_ENV_ALLOWLIST,
        *suite_conftest._SANDBOX_ENV_ALLOWLIST_PREFIXES,
    )
    credential_shaped = [
        name for name in entries if suite_conftest._is_external_credential_env(name)
    ]
    assert credential_shaped == [], (
        "these allowlist entries spell like credentials and would defeat the sweep: "
        + ", ".join(sorted(credential_shaped))
    )


def test_the_child_marker_is_one_spelling_everywhere() -> None:
    """Builder, generated shim and teardown guard read one constant.

    A single-site rename of the marker would leave a sharing child unrecorded - or, worse,
    treated as a parent that removes a session directory still in use. The constant is shared,
    and the shim's generated text is checked against it here.
    """
    marker = suite_conftest.CHILD_SANDBOX_ENV_VAR
    value = suite_conftest.CHILD_SANDBOX_FLAG_VALUE
    shim = (suite_conftest.CHILD_SANDBOX_SHIM_DIR / "sitecustomize.py").read_text(encoding="utf-8")
    assert f'os.environ.get("{marker}") == "{value}"' in shim, (
        "the generated shim reads a different marker than the constant that builds it"
    )
    assert suite_conftest.sandboxed_env()[marker] == value
    assert marker not in suite_conftest.sandboxed_env(shares_sandbox=False)


def test_the_documented_test_opt_ins_are_on_the_allowlist() -> None:
    """Every variable the docs name as a test opt-in survives the sweep.

    DATABASE_URL and the accounts env are cleared from the environment on purpose; these are
    the opposite - paths and flags a maintainer chose, which the tests read and skip without.
    Dropping one from the allowlist would turn a deliberate opt-in into a silent skip.
    """
    opt_ins = (
        "TP_TEST_OHLCV_DIR",  # AGENTS.md: the dataset-backed strategy runs
        "TP_INTEGRATION_BASE_URL",  # tests/integration: the live-pipeline opt-in
        "TRADING_PRODUCTION_DB",  # the corpus checks against a production copy
        "DIFFERENTIAL_REAL_SLICE_CSV",  # CI's differential slice cache
        "REQUIRE_REAL_DATA_SLICE",  # CI turns a missing slice into a failure
        "PLAYWRIGHT_BROWSERS_PATH",  # a browser install outside the sandbox HOME
    )
    for name in opt_ins:
        assert suite_conftest._is_allowlisted_env(name), (
            f"{name} is documented as a test opt-in but is no longer on the allowlist"
        )


def test_a_credential_shaped_override_needs_its_reason() -> None:
    """A credential-shaped override is the shape a leaked real credential would take.

    The redaction tests plant a dummy key in a child on purpose; anything else handing a
    child a credential-shaped variable has to say why in the same call, so the exception is
    a reviewed one and not a convenience.
    """
    from tests.conftest import sandboxed_env

    with pytest.raises(AssertionError, match="credential_override_reason"):
        sandboxed_env(ALPACA_API_KEY="not-a-real-key")
    named = sandboxed_env(
        ALPACA_API_KEY="not-a-real-key",
        credential_override_reason="the leak-guard's own fixture key",
    )
    assert named["ALPACA_API_KEY"] == "not-a-real-key"


def test_every_environment_variable_production_reads_is_accounted_for() -> None:
    """Every env var the platform reads is swept, allowed, or named with its reason.

    A variable that reaches production code reaches every test too. The three ways it can be
    accounted for: the credential sweep clears it, the allowlist keeps it (the documented
    opt-ins and the platform's own paths), or it is on this accepted list with the reason it
    is safe to let through. A new reader adds a name here, and that is where the decision is
    made deliberately rather than by finding out a test silently saw nothing.

    Read from source, not from the environment: the point is the names production *can* read.
    """
    import re

    readers: dict[str, str] = {}
    pattern = re.compile(
        r"(?:os\.environ\.get\(\s*|os\.getenv\(\s*|os\.environ\[\s*)"
        r"([\"\'])([A-Za-z_][A-Za-z0-9_]*)\2"
    )
    packages = (
        "data_tools",
        "live_runner",
        "risk",
        "runner",
        "scheduler",
        "scripts",
        "shared",
        "signal_bots",
        "validation",
        "viewer",
    )
    for package in packages:
        for path in sorted((_PROJECT_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                readers.setdefault(match.group(3), f"{package}/{path.name}")
    unaccounted = sorted(
        f"{name} ({readers[name]})"
        for name in readers
        if not suite_conftest._is_external_credential_env(name)
        and not suite_conftest._is_allowlisted_env(name)
        and name not in _ACCEPTED_PRODUCTION_ENV_READS
    )
    assert unaccounted == [], (
        "these production environment variables are neither swept nor allowed nor accepted "
        "with a reason - add each to _ACCEPTED_PRODUCTION_ENV_READS in this file or to the "
        "allowlist, or make it credential-shaped so the sweep clears it:\n  "
        + "\n  ".join(unaccounted)
    )


#: Environment variables production reads that the suite lets through, each with the reason it
#: is safe to: none of them carries a credential, and each is a path, a timeout, a model or
#: tool selection, or a job input the tests set deliberately.
_ACCEPTED_PRODUCTION_ENV_READS = frozenset(
    {
        "APP_ENV",  # the deployment's own name; tests set it where they test it
        "BACKTEST_TIMEOUT_SECONDS",  # the CLI's own timeout
        "COPILOT_CLI_EXECUTABLE",  # which copilot CLI to run
        "COPILOT_CLI_PATH",  # ditto
        "COPILOT_SDK_MODEL",  # model selection
        "CTRADER_CONNECT_BACKOFF_S",  # retry tuning
        "CTRADER_CONNECT_MAX_ATTEMPTS",  # retry tuning
        "CTRADER_VERIFY_ACCOUNT_ID",  # a job's input for the verify script
        "CTRADER_VERIFY_ORDER_ID",  # a job's input for the verify script
        "DISCORD_AUTH_SCHEME",  # per-bot auth scheme selection
        "DISCORD_CHANNEL_ID",  # a channel the tests set themselves
        "DOLT_TIMEOUT_SECONDS",  # dolthub request timeout
        "DUKASCOPY_JNLP_URL",  # the JForex bootstrap URL; the download edge is stubbed
        "DUKASCOPY_LOGIN",  # a Dukascopy demo login; the download edge is stubbed
        "DUKASCOPY_PASSWORD",  # see DUKASCOPY_LOGIN: same stubbing, never a live path
        "DUKASCOPY_USERNAME",  # see DUKASCOPY_LOGIN: same stubbing, never a live path
        "GIT_SHA",  # release detection metadata
        "HF_INTRADAY_COVERAGE_END",  # a dataset's coverage bound
        "JAVA_HOME",  # which JDK the JVM starts from
        "JFOREX_ALLOW_CUSTOM_JVM",  # a 1/0 escape hatch for a custom JVM path
        "JFOREX_CLASSPATH",  # the SDK's own classpath
        "JFOREX_HOME",  # the SDK's home
        "JFOREX_JARS",  # the SDK's jars
        "JFOREX_JVM_PATH",  # which JVM to start
        "JFOREX_TRUSTED_JAVA_HOME",  # the JDK roots a JVM may come from
        "JFOREX_TRUSTED_JAVA_HOMES",  # ditto
        "LEAN_PROJECT_DIR",  # the Lean engine's project directory
        "LOG_LEVEL",  # the log level, which tests set per test
        "MATRIX_ENGINE",  # the matrix runner's engine selection
        "MATRIX_MAX_WORKERS",  # the matrix runner's worker count
        "MATRIX_OUTPUT_DIR",  # the matrix runner's output directory
        "MATRIX_TIMEOUT_SECONDS",  # the matrix runner's timeout
        "PYTHON_BIN",  # which interpreter the matrix runner spawns
        "TRADING_PLATFORM_REPO",  # the repo root the matrix runner works in
        "TRADING_PLATFORM_VERSION",  # release detection metadata
    }
)


def test_the_audit_hook_refuses_a_secret_file_opened_through_os_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.open``/``os.openat`` raise the same ``open`` audit event ``open()`` does.

    The guard is on the event, not on the API, so a read that reaches for the lower-level call
    is refused the same way - pinned here, because the event's argument shape is the contract
    and a reader reaching around ``open`` is the obvious next attempt.
    """
    import os as _os

    # An attempt is refused *and* recorded, so the deliberate one here goes into a list this
    # test throws away - exactly as the symlink test below does. Without that the session-end
    # guard would fail a green run for the attempt this pin made on purpose.
    monkeypatch.setattr(suite_conftest, "_REPO_SECRET_OPENS", [])
    with pytest.raises(suite_conftest.SuiteHermeticityViolation, match=r"\.accounts\.env"):
        _os.open(str(REPO_ACCOUNTS_ENV), _os.O_RDONLY)


def test_a_child_never_sees_a_credential_variable_the_sweep_removed() -> None:
    """An allowlist prefix cannot re-admit what the credential sweep removed.

    The CI job's context is allowed through by prefix (``GITHUB_*``), and a token in that
    context - ``GITHUB_TOKEN`` - spells like the credentials the sweep exists to remove. The
    credential sweep runs first, so the name is gone before any allowlist could carry it,
    and this pins the interaction rather than trusting the order.
    """
    from tests.conftest import sandboxed_env

    os.environ["GITHUB_TOKEN"] = "ghp_secret_probe"
    try:
        child_env = sandboxed_env(GITHUB_REPOSITORY="itsjustFred/trading-platform")
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
    # The allowed one the child needs, the credential-shaped one gone.
    assert child_env["GITHUB_REPOSITORY"] == "itsjustFred/trading-platform"
    assert "GITHUB_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in os.environ

    body = "import os\nprint('TOKEN', os.environ.get('GITHUB_TOKEN'))\n"
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "TOKEN None" in result.stdout, result.stdout + result.stderr


def test_a_sandbox_sharing_child_must_be_finished_by_its_test(tmp_path: Path) -> None:
    """The lifetime contract, enforced: a sharing child still alive at teardown fails.

    A child that shares the session sandbox owns no lifetime of its own - the session
    directory is removed when the pytest process exits - so a test that leaves one running
    is reported rather than allowed to write into a directory about to disappear. The probe
    child sleeps far longer than this test does.
    """
    from tests.conftest import sandboxed_env

    marker = tmp_path / "child-started"
    leak = (
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", leak],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(200):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), "the probe child never started; this pin proves nothing"
    with pytest.raises(pytest.fail.Exception, match="left sandbox-sharing child"):
        suite_conftest.finish_shared_sandbox_children()
    # The enforcement killed it, so this test's own teardown finds nothing left to report.
    assert child.poll() is not None, "the enforcement did not finish the child it reported"


def test_a_child_keeps_a_home_its_parent_named(tmp_path: Path) -> None:
    """A sweep that clobbered a named HOME would fail the test that named it.

    A config-step probe hands its child the HOME its fixture tree lives in; the child's
    own sweep points HOME at the session sandbox by default, and has to step aside for
    the name the parent recorded.
    """
    from tests.conftest import sandboxed_env

    home = tmp_path / "child-home"
    home.mkdir()
    body = "import os\nprint('HOME', os.environ.get('HOME'))\n"
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"HOME {home}" in result.stdout, (
        "a child clobbered the HOME its parent named: " + result.stdout + result.stderr
    )


def test_a_self_sandboxing_child_keeps_the_overrides_it_named() -> None:
    """``shares_sandbox=False`` records its overrides too - the child sweeps them itself.

    Such a child runs the repo's suite (or imports this conftest) without the shim, and
    its own sweep still runs: a variable the naming test meant it to see must survive that
    sweep, not only survive the parent's environment construction.
    """
    from tests.conftest import sandboxed_env

    body = (
        "import tests.conftest  # noqa: F401 - importing it runs the sweep this pins\n"
        "import os\n"
        "print('PROBE', os.environ.get('TP_CHILD_PROBE_VAR'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(shares_sandbox=False, TP_CHILD_PROBE_VAR="named-for-this-child"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROBE named-for-this-child" in result.stdout, (
        "a self-sandboxing child dropped an override its parent named: "
        + result.stdout
        + result.stderr
    )


def test_a_child_guard_patches_by_value_not_by_name() -> None:
    """The child's guard matches the path an attribute holds, not the attribute's name.

    A future rename of the accounts-env constant to a name without ``ACCOUNTS_ENV`` in it
    would slip past a name filter and leave that child resolving the checkout's real
    credential path. The loader is exercised directly here, on a module-shaped object: the
    patch pass is the same one a real import runs.
    """
    probe = types.ModuleType("probe")
    probe.CREDENTIALS_FILE = REPO_ACCOUNTS_ENV
    probe.REPO_DOTENV = REPO_ENV_FILE
    probe.UNRELATED_PATH = REPO_ACCOUNTS_ENV.parent / "notes.txt"
    probe.NOT_A_PATH = "some string"

    class _NoopLoader:
        def create_module(self, spec: object) -> object:
            return None

        def exec_module(self, module: object) -> None:
            return None

    suite_conftest._ChildAccountsEnvLoader(_NoopLoader()).exec_module(probe)

    assert probe.CREDENTIALS_FILE == ISOLATED_ACCOUNTS_ENV
    assert probe.REPO_DOTENV == ISOLATED_DOTENV_FILE
    assert probe.UNRELATED_PATH == REPO_ACCOUNTS_ENV.parent / "notes.txt"
    assert probe.NOT_A_PATH == "some string"


def test_a_subprocess_refuses_the_network() -> None:
    """S16: the network guard covers a child too, or a call escapes the audit hook.

    The hook is installed in the pytest process only; a child that resolved a
    remote host did so unguarded. The child reports what its lookup raised, so
    the pin holds without this machine (or CI) having network access.
    """
    from tests.conftest import sandboxed_env

    body = (
        "import socket\n"
        "try:\n"
        "    socket.getaddrinfo('api.alpaca.markets', 443)\n"
        "    print('REACHED')\n"
        "except Exception as exc:\n"
        "    print('BLOCKED', type(exc).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(_PROJECT_ROOT),
        env=sandboxed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BLOCKED SuiteHermeticityViolation" in result.stdout, result.stdout + result.stderr


# --- end-to-end: the real pytest process must exit non-zero ----------------------------------- #
#
# The tests above assert that ``pytest_sessionfinish`` mutates a fake session's exit status. That
# does not prove pytest honours the mutation, which is what makes a leak fail CI. These run a real
# pytest process in a throwaway project whose ``tests/conftest.py`` is this file's conftest, so
# ``PROJECT_ROOT`` (and therefore the guarded directories and files) is the throwaway tree.

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_throwaway_suite(
    tmp_path: Path,
    test_source: str,
    extra_args: tuple[str, ...] = (),
    seed_files: dict[str, str] | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "data").mkdir()
    (project / "logs").mkdir()
    for name, content in (seed_files or {}).items():
        (project / name).write_text(content, encoding="utf-8")
    (project / "tests" / "conftest.py").write_text(
        Path(suite_conftest.__file__).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "tests" / "test_throwaway.py").write_text(test_source, encoding="utf-8")
    # Not sandboxed_env(): the throwaway project's own conftest is the copy under test, and a
    # child shim would pre-load the repo's conftest into the child before pytest loads the
    # copy - the run must exercise the copy exactly as a real run loads it. The inherited
    # environment is already the swept one, and the copy sweeps it again.
    # sandbox-env: throwaway suite loads its own copied conftest
    env = {
        **os.environ,
        "PYTHONPATH": str(_PROJECT_ROOT),
        **(env_extra or {}),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *extra_args,
            "tests/test_throwaway.py",
        ],
        cwd=project,
        env=env,  # sandbox-env: the throwaway suite loads its own copied conftest
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("secret", [".accounts.env", ".env"])
@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_nonzero_when_a_test_reads_a_repo_secret_file(
    tmp_path: Path, secret: str, extra_args: tuple[str, ...]
) -> None:
    """The guard the audit hook exists for: a real run fails, in one process and across xdist."""
    leak = (
        "from pathlib import Path\n"
        "def test_reads_a_secret_file():\n"
        f"    (Path(__file__).resolve().parents[1] / {secret!r}).read_text(encoding='utf-8')\n"
    )
    result = _run_throwaway_suite(
        tmp_path,
        leak,
        extra_args,
        seed_files={secret: "ALPACA_API_KEY=fake\n"},
    )

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "tried to open the checkout's real credential file" in result.stderr
    assert secret in result.stderr


@pytest.mark.parametrize("secret", [".accounts.env", ".env"])
@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_nonzero_when_a_test_swallows_the_secret_guard(
    tmp_path: Path, secret: str, extra_args: tuple[str, ...]
) -> None:
    """The audit hook raises inside ``open``, but a broad ``except`` in the code under test
    swallows it - which is why the session-end backstop exists. It must still fail the run in
    one process, and across xdist workers, where only ``workeroutput`` carries the worker's
    findings to the controller (a worker's own ``exitstatus`` is ignored)."""
    leak = (
        "from pathlib import Path\n"
        "def test_swallows_the_guard():\n"
        "    try:\n"
        f"        (Path(__file__).resolve().parents[1] / {secret!r}).read_text(encoding='utf-8')\n"
        "    except Exception:\n"
        "        pass\n"
    )
    result = _run_throwaway_suite(
        tmp_path,
        leak,
        extra_args,
        seed_files={secret: "ALPACA_API_KEY=fake\n"},
    )

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "tried to open the checkout's real credential file" in result.stderr


@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_nonzero_when_a_test_reaches_the_network(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    """The Alpaca discovery test that made this necessary: it resolved a live host and connected."""
    leak = (
        "import socket\n"
        "def test_reaches_the_network():\n"
        "    socket.getaddrinfo('api.alpaca.markets', 443)\n"
    )
    result = _run_throwaway_suite(tmp_path, leak, extra_args)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "attempted to reach the network" in result.stderr
    assert "api.alpaca.markets" in result.stderr


@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_nonzero_when_a_test_writes_the_repo_logs_dir(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    leak = (
        "from pathlib import Path\n"
        "def test_leaks():\n"
        "    (Path(__file__).resolve().parents[1] / 'logs' / 'live-runner.log').write_text('x')\n"
    )
    result = _run_throwaway_suite(tmp_path, leak, extra_args)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "ERROR: the repo directories" in result.stderr
    assert "created: live-runner.log" in result.stderr
    assert "logs" in result.stderr


@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_nonzero_when_a_test_writes_the_repo_data_dir(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    leak = (
        "from pathlib import Path\n"
        "def test_leaks():\n"
        "    (Path(__file__).resolve().parents[1] / 'data' / 'backtests.db').write_text('x')\n"
    )
    result = _run_throwaway_suite(tmp_path, leak, extra_args)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "ERROR: the repo directories" in result.stderr and "changed during" in result.stderr
    assert "created: backtests.db" in result.stderr


@pytest.mark.parametrize("extra_args", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_real_pytest_process_exits_zero_when_nothing_leaves_the_sandbox(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    """The control: a run that touches neither the watched trees nor the network stays green."""
    clean = (
        "import os, socket\n"
        "from pathlib import Path\n"
        "def test_writes_only_to_the_session_dir():\n"
        "    (Path(os.environ['DATA_DIR']) / 'ok.txt').write_text('x')\n"
        "    (Path(os.environ['LOG_DIR']) / 'ok.log').write_text('x')\n"
        "def test_names_localhost_only():\n"
        "    socket.getaddrinfo('localhost', 5556)\n"
    )
    result = _run_throwaway_suite(tmp_path, clean, extra_args)

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "ERROR" not in result.stderr


def test_real_pytest_process_runs_the_suite_without_xdist(tmp_path: Path) -> None:
    """``pytest_testnodedown`` is an xdist hookspec, so the conftest must mark it optional.

    Without the marker a plain run - ``-p no:xdist`` while debugging, or an environment whose
    ``dev`` extra was never installed - dies in plugin validation with INTERNALERROR before a
    single test runs.
    """
    clean = (
        "def test_ok():\n"
        "    assert True\n"
    )
    result = _run_throwaway_suite(tmp_path, clean, ("-p", "no:xdist"))

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "INTERNALERROR" not in result.stderr
