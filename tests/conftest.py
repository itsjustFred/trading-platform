from __future__ import annotations

import atexit
import importlib
import ipaddress
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import dotenv
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_DATA_DIR = (PROJECT_ROOT / "data").resolve()
REPO_LOG_DIR = (PROJECT_ROOT / "logs").resolve()
REPO_ACCOUNTS_ENV = (PROJECT_ROOT / ".accounts.env").resolve()
REPO_ENV_FILE = (PROJECT_ROOT / ".env").resolve()

# What the suite must observe read-only. ``logs/`` belongs beside ``data/`` because a run that
# writes the live-runner log puts fabricated order events into the log read during a live
# incident, which is the same failure mode as writing the live database: a green run that lied.
WATCHED_REPO_DIRS = (REPO_DATA_DIR, REPO_LOG_DIR)
_REPO_SECRET_FILES = frozenset({REPO_ACCOUNTS_ENV, REPO_ENV_FILE})
_SECRET_FILE_NAMES = frozenset({REPO_ACCOUNTS_ENV.name, REPO_ENV_FILE.name})
_SECRET_FILE_NAMES_FOLDED = frozenset(name.casefold() for name in _SECRET_FILE_NAMES)
_REPO_SECRET_FILES_FOLDED = frozenset(str(path).casefold() for path in _REPO_SECRET_FILES)
# NTFS and a default APFS volume resolve a differently-cased name to the same file, so the
# comparisons in ``_repo_secret_path`` fold case there; on a case-sensitive file system ``.ENV``
# is a different file and the exact match is both correct and cheaper.
_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"

# The suite must never write into the repo's ``data/``: in the main checkout that is the live
# platform's database, catalog and market data. Every process (the xdist controller and each
# worker imports this file) gets its own throwaway sandbox, forced rather than ``setdefault`` so
# an exported DATA_DIR pointing at production cannot leak in either. Both directories are set
# before any ``shared`` import, because ``shared.constants`` resolves DATA_DIR and LOG_DIR from
# the environment at import time and the platform writes through both.
_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="tp-pytest-session-")).resolve()
atexit.register(shutil.rmtree, _SESSION_ROOT, ignore_errors=True)
TEST_DATA_DIR = _SESSION_ROOT / "data"
TEST_LOG_DIR = _SESSION_ROOT / "logs"
# Where a caller that names no accounts-env file resolves to. It deliberately does not exist: a
# test that needs credentials writes its own under ``tmp_path``, and a file that existed here
# would be the same leak one directory further away from the checkout.
ISOLATED_ACCOUNTS_ENV = _SESSION_ROOT / ".accounts.env"
# The substitute for the checkout's real ``.env``. ``shared.constants`` and ``viewer.main`` both
# load it at import time, before any test can intervene, so the load is redirected here rather
# than prevented: the call still happens, the content is the empty session file.
ISOLATED_DOTENV_FILE = _SESSION_ROOT / ".env"
# A stale TP_ACCOUNTS_ENV names a real credential file. Production no longer reads it, but it is
# scrubbed anyway: a variable that points at the checkout's secrets has no business being set for
# a test, and keeping it out means a reintroduced reader cannot be fed by the shell.
ACCOUNTS_ENV_ENV_VAR = "TP_ACCOUNTS_ENV"
# Exported broker, Discord, Sentry and database configuration is as much a leak as the files
# above: ``load_accounts_env_values`` overlays ``os.environ`` under the accounts file, so a real
# ``ALPACA_API_KEY`` in the shell would make a test pass for a reason CI cannot reproduce.
_EXTERNAL_ENV_NAMES = {ACCOUNTS_ENV_ENV_VAR, "DATABASE_URL"}
_EXTERNAL_ENV_NAMESPACES = ("ALPACA_", "CTRADER_", "DISCORD_", "SENTRY_")
_EXTERNAL_ENV_MARKERS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "DSN",
)


def _is_external_credential_env(name: str) -> bool:
    return (
        name in _EXTERNAL_ENV_NAMES
        or name.startswith(_EXTERNAL_ENV_NAMESPACES)
        or any(marker in name for marker in _EXTERNAL_ENV_MARKERS)
    )


def _clear_external_credential_env() -> None:
    for name in [name for name in os.environ if _is_external_credential_env(name)]:
        os.environ.pop(name, None)


_clear_external_credential_env()
for _session_dir in (TEST_DATA_DIR, TEST_LOG_DIR):
    _session_dir.mkdir(parents=True, exist_ok=True)
ISOLATED_DOTENV_FILE.write_text("", encoding="utf-8")

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["TRADING_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["LOG_DIR"] = str(TEST_LOG_DIR)
# Removed rather than redirected: an exported TP_ACCOUNTS_ENV names a real credential file, and
# nothing in the suite may read one. Removing it leaves exactly one redirect in play - the roster
# of module constants below - so a test that patches a module default still changes what the code
# reads.
os.environ.pop(ACCOUNTS_ENV_ENV_VAR, None)


class SuiteHermeticityViolation(RuntimeError):
    """A test reached outside its sandbox.

    Raised inside an audit hook, so it surfaces as an ordinary exception in the offending
    frame. A ``RuntimeError`` subclass rather than an ``OSError`` on purpose: the failure must
    not be mistakable for the network error the call would have produced, and broad
    ``except Exception`` handlers in the code under test swallow it - which is why every
    violation is also recorded and fails the session at
    :func:`pytest_sessionfinish`.
    """


_REPO_SECRET_OPENS: list[str] = []
_NETWORK_VIOLATIONS: list[str] = []


def repo_secret_opens() -> tuple[str, ...]:
    """Every attempt this session made to open the checkout's real ``.accounts.env`` / ``.env``.

    An attempt is refused before the system call, so this is the backstop for the case where the
    refusal is swallowed by an ``except Exception`` in the code under test.
    """
    return tuple(_REPO_SECRET_OPENS)


def network_violations() -> tuple[str, ...]:
    """Every non-localhost socket operation refused this session."""
    return tuple(_NETWORK_VIOLATIONS)


def _is_secret_file_name(name: str) -> bool:
    """Whether ``name`` spells a secret file's name, folding case where the FS does."""
    if _CASE_INSENSITIVE_FS:
        return name.casefold() in _SECRET_FILE_NAMES_FOLDED
    return name in _SECRET_FILE_NAMES


def _repo_secret_path(raw_path: object) -> Path | None:
    """The checkout's real secret file ``raw_path`` names, or ``None``.

    The path is resolved before the name is compared, so a symlink to a secret file is caught by its
    target and not by the name someone gave the link. ``open`` is the hottest audit event in the
    process, so the resolve - a ``realpath`` that stats every component - runs only when it can
    change the answer: the resolved name differs from the given one only when the final component
    is a symlink, and a match is only possible when that name (given or resolved) is a secret file
    name. A plain file named anything else cannot resolve to a secret, so it returns ``None``
    after one ``lstat`` instead of a full resolution. Nothing is cached: a cache keyed on the path
    alone would hand back the previous answer for a path that is a symlink now, and the cwd a
    relative path resolves against can change inside a session.
    """
    if isinstance(raw_path, int) or raw_path is None:
        return None
    try:
        if isinstance(raw_path, bytes):
            raw_path = os.fsdecode(raw_path)
        candidate = Path(os.fspath(raw_path))
    except (TypeError, ValueError):
        return None
    if not _is_secret_file_name(candidate.name) and not os.path.islink(candidate):
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    # Both spellings of the name: a symlink keeps its own, its target keeps the real one.
    if not _is_secret_file_name(candidate.name) and not _is_secret_file_name(resolved.name):
        return None
    if _CASE_INSENSITIVE_FS:
        # ``Path`` equality stays case-sensitive on posix even where the file system is not.
        return resolved if str(resolved).casefold() in _REPO_SECRET_FILES_FOLDED else None
    return resolved if resolved in _REPO_SECRET_FILES else None


def _is_local_host(host: object) -> bool:
    """Whether ``host`` names this machine.

    ``None`` is the wildcard lookup (a bind, or "any" address), never a remote name.
    """
    if host is None:
        return True
    if isinstance(host, bytes):
        # The socket calls accept a name either way round, and a name stays a name when decoded.
        host = os.fsdecode(host)
    if not isinstance(host, str):
        return False
    candidate = host.strip("[]").strip().lower()
    if candidate in ("", "localhost", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # A name that is not an IP literal stays unproven, and an unproven host is not local.
        return False


def _refuse_remote_host(host: object, action: str) -> None:
    if _is_local_host(host):
        return
    _NETWORK_VIOLATIONS.append(f"{action} {host!r}")
    raise SuiteHermeticityViolation(
        f"the suite refused a {action} to {host!r}: tests must not reach the network. "
        "Point them at a fixture, a stub transport or localhost."
    )


def _audit_hook(event: str, args: tuple) -> None:
    """Record the two leaks that are invisible in a green run, and stop the network one.

    Installed at import time of this file, which is before collection: that is what makes it cover
    the import-time readers too (a module that discovers over HTTP while being imported is
    collected, not run).
    """
    if event == "open":
        secret = _repo_secret_path(args[0] if args else None)
        if secret is not None:
            _REPO_SECRET_OPENS.append(str(secret))
            raise SuiteHermeticityViolation(
                f"the suite refused to open the checkout's real {secret.name}: tests must not read"
                " or write it. A test that needs credentials writes its own under tmp_path."
            )
        return
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if isinstance(address, (str, bytes)):
            return  # AF_UNIX path: no network
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        _refuse_remote_host(host, "connect")
        return
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        _refuse_remote_host(args[0] if args else None, "name lookup")
        return
    if event == "socket.getnameinfo":
        address = args[0] if args else None
        if isinstance(address, (str, bytes)):
            return  # AF_UNIX path
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        _refuse_remote_host(host, "reverse name lookup")
        return
    if event in ("socket.sendto", "socket.sendmsg"):
        address = args[1] if len(args) > 1 else None
        if isinstance(address, (str, bytes)):
            return  # AF_UNIX path
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        _refuse_remote_host(host, "send")


sys.addaudithook(_audit_hook)


# ``shared.constants`` and ``viewer.main`` load the checkout's real ``.env`` at import time, so
# the load is redirected to the empty session file. This has to be installed *before* the roster
# below is imported: importing it pulls in ``shared.constants``, which would otherwise load the
# real file before the redirect exists.
_real_load_dotenv = dotenv.load_dotenv


def _redirected_load_dotenv(path=None, stream=None, *args, **kwargs):
    """``dotenv.load_dotenv`` with the checkout's ``.env`` redirected to the session file.

    The path is read from either calling convention - ``load_dotenv(p)`` and
    ``load_dotenv(dotenv_path=p)`` are the same call, and a redirect that only saw the first would
    let the second load the real file. A caller that supplies a stream reads from it, so the file
    is not discovered for one - and ``stream=None`` is not a stream. A caller that names neither
    is resolved through the library's own discovery, which walks up from this file and would find
    the checkout's.
    """
    named = kwargs.pop("dotenv_path", None)
    target = path if path is not None else named
    if stream is None and target is None:
        try:
            discovered = dotenv.find_dotenv(usecwd=False)
        except Exception:  # pragma: no cover - discovery is best effort
            discovered = ""
        target = discovered or None
    # One expression of the path, forwarded once: a positional and a keyword copy of the same
    # argument is how a redirect ends up applied to one of them and not the other.
    if _repo_secret_path(target) == REPO_ENV_FILE:
        target = ISOLATED_DOTENV_FILE
    return _real_load_dotenv(target, stream, *args, **kwargs)


dotenv.load_dotenv = _redirected_load_dotenv


# ``DEFAULT_ACCOUNTS_ENV`` is the path of the checkout's real credential file, and it is a module
# constant in six modules plus a name imported from them in two more. A copy in a test
# (``from live_runner.service_discovery import DEFAULT_ACCOUNTS_ENV``) binds the value the module
# held at import time, so every copy is pointed at the session sandbox here, before collection:
# patching one module's attribute leaves the others reading the real file.
_ACCOUNTS_ENV_DEFINERS: tuple[tuple[str, str], ...] = (
    ("live_runner.credential_env", "DEFAULT_ACCOUNTS_ENV"),
    ("live_runner.service_discovery", "DEFAULT_ACCOUNTS_ENV"),
    ("live_runner.account_discovery", "DEFAULT_ACCOUNTS_ENV"),
    ("live_runner.ctrader_adapter", "DEFAULT_ACCOUNTS_ENV"),
    ("live_runner.daemon", "DEFAULT_ACCOUNTS_ENV"),
    ("scripts.live_smoke", "DEFAULT_ACCOUNTS_ENV"),
    ("scripts.verify_ctrader_order", "ACCOUNTS_ENV"),
    ("live_runner.test_signal_trigger", "DEFAULT_ACCOUNTS_ENV"),
)
# These import the name from a module above, and the value they bind lands in function default
# arguments, which are frozen at import time. They are therefore imported *after* the definers
# have been patched: that is the only moment the frozen copy can be given the sandbox path.
_ACCOUNTS_ENV_IMPORTERS: tuple[tuple[str, str], ...] = (
    ("live_runner.preflight", "DEFAULT_ACCOUNTS_ENV"),
    ("scripts.discover_discord_authors", "DEFAULT_ACCOUNTS_ENV"),
)
ACCOUNTS_ENV_MODULES = _ACCOUNTS_ENV_DEFINERS + _ACCOUNTS_ENV_IMPORTERS


def _isolate_accounts_env() -> None:
    for module_name, attribute in ACCOUNTS_ENV_MODULES:
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            raise AssertionError(
                f"{module_name} no longer defines {attribute}; the accounts-env roster in "
                "tests/conftest.py is stale and the real .accounts.env would be read again"
            )
        setattr(module, attribute, ISOLATED_ACCOUNTS_ENV)


_isolate_accounts_env()


def snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` for every file and directory under ``root``."""
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snapshot
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            snapshot[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    changes = [f"created: {p}" for p in sorted(after.keys() - before.keys())]
    changes += [f"deleted: {p}" for p in sorted(before.keys() - after.keys())]
    modified = sorted(p for p in before.keys() & after.keys() if before[p] != after[p])
    changes += [f"modified: {p}" for p in modified]
    return changes


_REPO_TREES_BEFORE: dict[str, dict[str, tuple[int, int]]] = {}


def pytest_configure(config: pytest.Config) -> None:
    # Controller only: xdist workers would each snapshot a tree the others are racing against.
    if hasattr(config, "workerinput"):
        return
    global _REPO_TREES_BEFORE
    _REPO_TREES_BEFORE = {str(root): snapshot_tree(root) for root in WATCHED_REPO_DIRS}


def _fail_session(session: pytest.Session, message: str) -> None:
    print(message, file=sys.stderr)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _watched_repo_dirs_changed() -> list[str]:
    if not _REPO_TREES_BEFORE:
        # Nothing to compare against: this process is an xdist worker (it does not snapshot, because
        # its siblings are still writing the tree), or a direct call outside a run.
        return []
    changes: list[str] = []
    for root, before in _REPO_TREES_BEFORE.items():
        for change in diff_snapshots(before, snapshot_tree(Path(root))):
            changes.append(f"{root}: {change}")
    return changes


def _is_suite_module(module: object) -> bool:
    """Whether ``module`` lives under this repo's ``tests/`` tree.

    The suite's own modules legitimately hold the real path - they compare against it - so the
    sweep skips them. It is a guard for *production* readers: the audit hook is what stops a test
    from using the path.
    """
    source = getattr(module, "__file__", None)
    if not source:
        return False
    try:
        return Path(source).resolve().is_relative_to((PROJECT_ROOT / "tests").resolve())
    except (OSError, ValueError):
        return False


def _accounts_env_readers() -> list[str]:
    """Every imported module whose ``*ACCOUNTS_ENV*`` attribute is still the real file.

    Swept at session end, when ``sys.modules`` holds everything the run imported: a *new* module
    that resolves its own copy of the path is a leak the roster cannot know about, and it would
    otherwise stay invisible until the file's owner noticed a fake account in discovery.
    """
    offenders: list[str] = []
    for module_name, module in list(sys.modules.items()):
        if module is None or _is_suite_module(module):
            continue
        try:
            attributes = list(vars(module).items())
        except Exception:  # pragma: no cover - a module without a usable __dict__
            continue
        for name, value in attributes:
            if "ACCOUNTS_ENV" not in name or isinstance(value, (bool, int, float)):
                continue
            try:
                candidate = Path(os.fspath(value))
            except (TypeError, ValueError):
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved == REPO_ACCOUNTS_ENV:
                offenders.append(f"{module_name}.{name}")
    return offenders


# Violations an xdist worker recorded and handed to the controller through ``workeroutput``.
# A worker's own ``session.exitstatus`` is ignored by xdist, so without this handoff a swallowed
# violation on a worker would fail nothing.
_WORKER_VIOLATIONS: list[str] = []


def _hermeticity_failure_messages(session: pytest.Session) -> list[str]:
    """Every sandbox escape this process recorded, as session-failure messages."""
    failures: list[str] = []

    # The directory check runs in the controller only - an xdist worker would snapshot a tree its
    # siblings are still writing. The other guards run in every process, because a worker is a
    # separate process in which the tests really ran.
    if not hasattr(session.config, "workerinput"):
        changes = _watched_repo_dirs_changed()
        if changes:
            shown = "\n  ".join(changes[:25])
            more = f"\n  ... and {len(changes) - 25} more" if len(changes) > 25 else ""
            failures.append(
                "\n\nERROR: the repo directories the suite must leave alone changed during the test"
                f" session:\n  {shown}{more}"
                "\nTests must write under tmp_path / the session DATA_DIR / LOG_DIR only. This"
                " check cannot tell which process made the change: if a running viewer or daemon"
                " in this checkout wrote it, stop that process or run the suite from a separate"
                " checkout."
            )

    secret_opens = repo_secret_opens()
    if secret_opens:
        shown = "\n  ".join(dict.fromkeys(secret_opens))
        failures.append(
            "\n\nERROR: the test session tried to open the checkout's real credential"
            f" file(s):\n  {shown}"
            "\nconftest points every DEFAULT_ACCOUNTS_ENV at the session sandbox and redirects"
            " the .env load. Something named the file directly: a test that needs credentials must"
            " write its own under tmp_path. Each attempt also raised SuiteHermeticityViolation,"
            " which broad exception handlers can swallow."
        )

    readers = _accounts_env_readers()
    if readers:
        shown = "\n  ".join(dict.fromkeys(readers))
        failures.append(
            f"\n\nERROR: these modules still resolve the real {REPO_ACCOUNTS_ENV}:\n  {shown}"
            "\nAdd them to ACCOUNTS_ENV_MODULES in tests/conftest.py, or stop building the path."
        )

    network = network_violations()
    if network:
        shown = "\n  ".join(dict.fromkeys(network))
        failures.append(
            f"\n\nERROR: the test session attempted to reach the network:\n  {shown}"
            "\nTests must run against a fixture, a stub transport or localhost. The offending call"
            " also raised SuiteHermeticityViolation, which broad exception handlers can swallow."
        )

    return failures


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run loudly when the suite escaped its sandbox.

    Three independent guards, because each one fails silently on its own:

    * a watched repo directory (``data/``, then ``logs/``) changed;
    * the checkout's real ``.accounts.env`` or ``.env`` was opened;
    * a module still resolves an ``*ACCOUNTS_ENV*`` attribute to the real ``.accounts.env``;
    * a non-localhost socket operation was attempted (see ``_audit_hook``).

    Limits, by design: the window is ``pytest_configure`` to here, so writes after the session
    (atexit handlers, threads still running after the last test) are not seen. The directory check
    cannot attribute a change to the suite: any process writing ``data/`` during the run (e.g. a
    live viewer or daemon in the same checkout) fails it too. The remedy is to stop that process or
    run the suite elsewhere. Python subprocesses a test starts inherit the environment but not the
    audit hook, the ``load_dotenv`` redirect or the patched module constants, so a child that
    imports ``shared.constants`` loads the checkout's real ``.env`` again; tests that spawn
    children must pass them a sandboxed environment explicitly.

    Under xdist the picture splits: the directory check runs in the controller only (a worker
    would snapshot a tree its siblings are still writing), while the other guards run in every
    process because a worker is where the tests really ran. A worker's ``exitstatus`` is ignored
    by the controller, so the worker hands its findings to the controller through
    ``config.workeroutput`` and :func:`pytest_testnodedown` collects them there.
    """
    failures = _hermeticity_failure_messages(session)
    if hasattr(session.config, "workerinput"):
        workeroutput = getattr(session.config, "workeroutput", None)
        if isinstance(workeroutput, dict):
            workeroutput["tp_violations"] = failures
        for message in failures:
            print(message, file=sys.stderr)
        return
    for message in dict.fromkeys((*failures, *_WORKER_VIOLATIONS)):
        _fail_session(session, message)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object) -> None:
    """Collect the hermeticity violations an xdist worker could not fail the session with.

    ``optionalhook`` because ``pytest_testnodedown`` is an xdist hookspec: without the plugin
    (``-p no:xdist``, or a ``dev`` extra that was never installed) pluggy would otherwise reject
    the unknown hook and the whole run would die in plugin validation.
    """
    workeroutput = getattr(node, "workeroutput", None)
    if isinstance(workeroutput, dict):
        reported = workeroutput.get("tp_violations")
        if isinstance(reported, list):
            _WORKER_VIOLATIONS.extend(str(item) for item in reported)


def _requested_browser_name(item: pytest.Item) -> str:
    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        browser_name = callspec.params.get("browser_name")
        if isinstance(browser_name, str) and browser_name:
            return browser_name

    configured = item.config.getoption("browser", default=None)
    if isinstance(configured, (list, tuple)):
        for candidate in configured:
            if isinstance(candidate, str) and candidate:
                return candidate
    if isinstance(configured, str) and configured:
        return configured
    return "chromium"


def _is_missing_browser_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "executable doesn't exist" in normalized
        or ("browser" in normalized and "not found" in normalized)
    )


def _make_sandbox_db(monkeypatch, tmp_path: Path, *, create_schema: bool):
    """Build a throwaway ``shared.database`` sandbox and restore state after.

    The production engine URL is built from ``DB_PATH`` at import time, so the
    engine swap below is what actually isolates database access; patching
    ``DB_PATH`` alongside keeps any code that resolves paths from the module
    consistent, and ``DATA_DIR`` stays patched because ``init_db()`` mkdirs it
    and the artifact writers (``DATA_DIR / "backtests"``) must land in the
    sandbox too.
    """
    import shared.database as database

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "backtests.db"

    engine = database.create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    if create_schema:
        database.SQLModel.metadata.create_all(engine)

    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    # False on purpose: tests (and production code paths) must still be able to
    # call init_db() and run _migrate_schema() — e.g. the per-deployment UNIQUE
    # index migrations in shared/database.py.
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    try:
        yield database, engine, data_dir
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_external_credentials():
    """Keep a credential-shaped variable set without ``monkeypatch`` out of the next test."""
    _clear_external_credential_env()


@pytest.fixture(autouse=True)
def _no_jforex_jvm_prewarm():
    """Keep ``TestClient(app)`` lifespans from starting the JForex JVM.

    The viewer's startup prewarm resolves the JForex SDK classpath and, when no jars are found,
    *downloads* ~35 jars from Dukascopy over the network into ``DATA_DIR/.jforex-sdk``. Any test
    that enters the real lifespan would otherwise do that (slow, network-dependent, and into the
    live data dir before DATA_DIR was isolated). Tests exercising the downloader call its helpers
    directly and are unaffected.

    Uses its own ``MonkeyPatch`` rather than the ``monkeypatch`` fixture: an autouse fixture that
    requested it would instantiate it before every test's other fixtures and so delay its undo until
    after them, which breaks fixtures that assert state restored by their own ``monkeypatch`` use.
    """
    from data_tools.downloaders import jforex_dl

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jforex_dl, "prewarm_jvm", lambda: None)
        yield


@pytest.fixture
def isolated_db_sandbox(monkeypatch, tmp_path: Path):
    """Redirect ``shared.database`` at a throwaway SQLite database (pre-created
    schema). Yields ``(database_module, engine, data_dir)``."""
    yield from _make_sandbox_db(monkeypatch, tmp_path, create_schema=True)


@pytest.fixture
def isolated_db(isolated_db_sandbox):
    """Alias of :func:`isolated_db_sandbox`."""
    return isolated_db_sandbox


@pytest.fixture
def isolated_db_no_schema(monkeypatch, tmp_path: Path):
    """Sandbox without pre-created schema: for tests that exercise
    ``init_db()``'s migration path over a hand-built legacy layout."""
    yield from _make_sandbox_db(monkeypatch, tmp_path, create_schema=False)


def seed_live_deployments(
    database, deployment_ids, *, account_id: str = "fixture-account"
) -> None:
    """Create the parent rows that a fixture's synthetic signals point at.

    ``live_signals.deployment_id`` carries a foreign key, so a fixture writing a signal has to write
    its deployment first, exactly like production code does. One definition, because several modules
    write signals against synthetic ids and every one of them predates the constraint.
    """
    database.init_db()
    with database.get_session() as session:
        if session.get(database.LiveAccountModel, account_id) is None:
            session.add(
                database.LiveAccountModel(id=account_id, broker="fixture", name=account_id)
            )
            session.flush()
        for deployment_id in deployment_ids:
            if session.get(database.LiveDeploymentModel, deployment_id) is None:
                session.add(
                    database.LiveDeploymentModel(
                        id=deployment_id,
                        strategy_name=f"fixture-{deployment_id}",
                        account_id=account_id,
                        symbols_json=[],
                        timeframe="1m",
                        status="PAUSED",
                    )
                )
        session.commit()


@lru_cache(maxsize=None)
def _playwright_browser_launchable(browser_name: str) -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser_type = getattr(playwright, browser_name, None)
            if browser_type is None:
                return False, f"Unsupported Playwright browser: {browser_name}"
            browser = browser_type.launch(headless=True)
            browser.close()
            return True, ""
    except Exception as exc:  # plugin raises PlaywrightError, but keep message-based skip gating.
        return False, str(exc)


@pytest.fixture
def viewer_client(monkeypatch, tmp_path):
    """Shared viewer TestClient with DATA_DIR-scoped storage paths."""
    from fastapi.testclient import TestClient as _TestClient

    import viewer.main as viewer_app_module

    legacy_module = viewer_app_module.legacy_viewer

    for module in (viewer_app_module, legacy_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
        monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
        monkeypatch.setattr(module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
        monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")

    monkeypatch.setattr(viewer_app_module, "list_backtest_ids", lambda: [])
    monkeypatch.setattr(viewer_app_module, "load_backtest_result", lambda _backtest_id: None)
    monkeypatch.setattr(viewer_app_module, "save_result_to_sqlite", lambda _result: None)
    monkeypatch.setattr(
        viewer_app_module,
        "migrate_json_to_sqlite",
        lambda: {"migrated": 0, "failed": 0, "total": 0},
    )
    viewer_app_module.DATA_TASKS.clear()
    viewer_app_module._COALESCED_CLEANUP_PENDING.clear()
    viewer_app_module._COALESCED_CLEANUP_ORDER.clear()
    viewer_app_module._COALESCED_CLEANUP_ATTEMPTS.clear()
    viewer_app_module._COALESCED_CLEANUP_EXHAUSTED.clear()
    return _TestClient(viewer_app_module.app)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("e2e") is None:
        return

    launchable, error_message = _playwright_browser_launchable(_requested_browser_name(item))
    if launchable:
        return
    if _is_missing_browser_error(error_message):
        pytest.skip(
            "Playwright browser binaries not installed. "
            "Run: python -m playwright install firefox"
        )


@pytest.fixture(autouse=True)
def _restore_console_log_streams():
    """Undo console-stream routing a test triggered (e.g. through a CLI ``main()``).

    ``shared.logging.route_console_logs_to_stderr()`` rebinds the console handlers
    of every cached logger to the ``sys.stderr`` in effect at the call. Under pytest
    that is a per-test capture stream which is closed afterwards, so a later test
    whose CLI reroutes again flushes a closed file (``ValueError: I/O operation on
    closed file``). Restore the routing global and each console handler's stream
    without flushing.
    """
    from shared import logging as shared_logging

    stream_name = shared_logging._console_stream_name
    saved = [
        (handler, handler.stream)
        for logger in list(shared_logging._loggers.values())
        for handler in logger.handlers
        if getattr(handler, "_trading_console", False)
    ]
    yield
    import sys

    shared_logging._console_stream_name = stream_name
    restored = {id(handler) for handler, _ in saved}
    for handler, stream in saved:
        handler.stream = stream
    # Loggers created during the test may be bound to its (now closed) capture
    # stream; point them at the real process stream, which never closes.
    real_stream = getattr(sys, f"__{stream_name}__")
    for logger in list(shared_logging._loggers.values()):
        for handler in logger.handlers:
            if getattr(handler, "_trading_console", False) and id(handler) not in restored:
                handler.stream = real_stream
