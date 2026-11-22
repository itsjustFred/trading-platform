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


def test_a_remote_name_lookup_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    with pytest.raises(suite_conftest.SuiteHermeticityViolation, match="api.alpaca.markets"):
        socket.getaddrinfo("api.alpaca.markets", 443)
    assert suite_conftest.network_violations() == ("name lookup 'api.alpaca.markets'",)


def test_a_remote_connect_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(suite_conftest, "_NETWORK_VIOLATIONS", [])
    probe = socket.socket()
    try:
        with pytest.raises(suite_conftest.SuiteHermeticityViolation, match="35.221.23.121"):
            probe.connect(("35.221.23.121", 443))
    finally:
        probe.close()
    assert suite_conftest.network_violations() == ("connect '35.221.23.121'",)


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
        env={**os.environ, "TP_INTEGRATION_BASE_URL": "http://127.0.0.1:5556"},
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ATTEMPTS []" in result.stdout, result.stdout + result.stderr
    assert "IMPORTED tests.integration.test_strategy_pipeline" in result.stdout


def test_the_integration_module_is_skipped_without_the_opt_in() -> None:
    """No default port: an unset TP_INTEGRATION_BASE_URL skips the module instead of guessing."""
    env = {k: v for k, v in os.environ.items() if k != "TP_INTEGRATION_BASE_URL"}
    env.pop("BASE_URL", None)
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
        env=env,
        timeout=300,
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
            env={**os.environ, "TP_INTEGRATION_BASE_URL": base_url},
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
    env = {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)}
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
        env=env,
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
