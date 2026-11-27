from __future__ import annotations

import atexit
import contextlib
import importlib
import ipaddress
import os
import shutil
import sys
import tempfile
import threading
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
#
# A subprocess a test starts is the one process that must NOT create a session of its own:
# ``sandboxed_env()`` marks it (``TP_TEST_CHILD_SANDBOX``) and names the parent's session
# directory (``TP_TEST_SESSION_ROOT``), so the child shares the sandbox instead of building a
# second one - and never deletes the parent's on its way out, which an ``atexit`` rmtree of a
# directory the parent is still using would do.
_CHILD_PROCESS = os.environ.get("TP_TEST_CHILD_SANDBOX") == "1"
if _CHILD_PROCESS:
    _SESSION_ROOT = Path(os.environ["TP_TEST_SESSION_ROOT"]).resolve()
else:
    _SESSION_ROOT = Path(tempfile.mkdtemp(prefix="tp-pytest-session-")).resolve()
    # A child that shares this sandbox (sandboxed_env()'s default) owns no lifetime of its own:
    # the session directory is removed here, so such a child must be finished before the pytest
    # process exits. A child that has to outlive the session is the sandbox's one case that has
    # no owner, and the tests that leave one running must not exist - the watchdog and lock
    # holders are all waited on or killed.
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
# The names ``sandboxed_env()`` applied as overrides for one child, so the child's own
# sweeps - the allowlist one and the credential one - keep what the parent chose for it.
# Without the handoff a ``JFOREX_JARS`` or ``TP_REPO_ROOT`` a test passed to its child would
# be deleted by the very guard meant to protect that child.
CHILD_ENV_OVERRIDES_ENV_VAR = "TP_TEST_CHILD_ENV"
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
    """Drop the caller's credential-shaped variables, keeping the ones a parent named.

    A parent that handed a child a variable it chose (``sandboxed_env`` records those names
    in ``TP_TEST_CHILD_ENV``) is not the developer the sweep exists to keep out: a dummy
    ``SENTRY_RELEASE``, a ``NVIDIA_API_KEY`` the redaction test plants or a ``JFOREX_JARS``
    fixture path is exactly what that child is meant to see.
    """
    inherited = os.environ.get(CHILD_ENV_OVERRIDES_ENV_VAR, "").split(",")
    for name in [name for name in os.environ if _is_external_credential_env(name)]:
        if name in inherited:
            continue
        os.environ.pop(name, None)


_clear_external_credential_env()
for _session_dir in (TEST_DATA_DIR, TEST_LOG_DIR):
    _session_dir.mkdir(parents=True, exist_ok=True)
if not _CHILD_PROCESS:
    # The parent owns the substitute .env; a child reads the same file through the
    # redirect and must not rewrite it while the parent is still running.
    ISOLATED_DOTENV_FILE.write_text("", encoding="utf-8")

# A child process gets its directories from ``sandboxed_env()`` (the parent's
# session root, or the directory the naming test chose), so here they are filled
# in rather than forced: forcing them would overwrite the value the test meant the
# child to see.
if _CHILD_PROCESS:
    os.environ.setdefault("DATA_DIR", str(TEST_DATA_DIR))
    os.environ.setdefault("TRADING_DATA_DIR", str(TEST_DATA_DIR))
else:
    os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
    os.environ["TRADING_DATA_DIR"] = str(TEST_DATA_DIR)
# A developer's shell or .env must never turn the test suite into an alert
# source: the ops alert sink reads OPS_ALERT_WEBHOOK_URL from the environment.
# Set it blank rather than popping it: shared.constants load_dotenv() fills in
# missing variables from .env, but never overrides one that is already set, and
# create_sink_from_env treats blank as disabled.
os.environ["OPS_ALERT_WEBHOOK_URL"] = ""
# Same reasoning for the alert filters: a developer's environment must not
# change which records the suite's alert-sink tests see.
os.environ["OPS_ALERT_MIN_LEVEL"] = ""
os.environ["OPS_ALERT_EVENTS"] = ""
os.environ["OPS_ALERT_IGNORE"] = ""
if _CHILD_PROCESS:
    os.environ.setdefault("LOG_DIR", str(TEST_LOG_DIR))
else:
    os.environ["LOG_DIR"] = str(TEST_LOG_DIR)
# Removed rather than redirected: an exported TP_ACCOUNTS_ENV names a real credential file, and
# nothing in the suite may read one. Removing it leaves exactly one redirect in play - the roster
# of module constants below - so a test that patches a module default still changes what the code
# reads.
os.environ.pop(ACCOUNTS_ENV_ENV_VAR, None)


# --- the environment the suite keeps of its caller's ------------------------------------------- #

# A denylist of credential-shaped names was the first cut of this guard, and it leaves the rest of
# the caller's environment in place: a base URL pointing at the live broker, a PYTHONPATH that
# shadows a package, a HOME that reads the developer's own configuration, a TZ that moves a date
# boundary. None of that is the suite's business, so the environment is cleared down to an
# explicit allowlist instead - in the pytest process and in every child a test starts.
#
# What survives is the minimum the suite and its children need to run:
#   * PATH, TMP*, PATHEXT, COMSPEC, SystemRoot, windir and
#     NoDefaultCurrentDirectoryInExePath: the interpreter and the tools a test spawns must still
#     start, on every supported platform.
#   * locale and TZ: the platform reads them; a test needing another zone sets it itself.
#   * PYTHON*: the interpreter's own switches, plus the PYTHONPATH sandboxed_env() rebuilds.
#   * PYTEST_XDIST*: an xdist worker's own identity, set by the controller before this import.
#   * CI, GITHUB_*, RUNNER_TEMP: the CI job's context, which the workflows and the
#     workflow-simulation tests read.
#   * the documented opt-ins (TP_TEST_OHLCV_DIR, TP_INTEGRATION_BASE_URL, TRADING_PRODUCTION_DB,
#     DIFFERENTIAL_REAL_SLICE_CSV, REQUIRE_REAL_DATA_SLICE, PLAYWRIGHT_BROWSERS_PATH): each names
#     a path or flag a maintainer chose deliberately, and each test that reads one skips cleanly
#     when it is unset.
# HOME is not on the list: it is pointed at the session sandbox instead, so a test (or a child)
# never reads the caller's home directory. The sandbox's own variables (DATA_DIR, LOG_DIR, the
# OPS_ALERT_* filters) are, because the sweep runs after they were forced: they hold this
# process's values by then, and a child that inherited them from sandboxed_env() must keep them
# - together with the names sandboxed_env() itself lists in TP_TEST_CHILD_ENV (see
# :func:`sandboxed_env`).
_SANDBOX_ENV_ALLOWLIST = frozenset(
    {
        "CI",
        "COMSPEC",
        "DATA_DIR",
        "DIFFERENTIAL_REAL_SLICE_CSV",
        "LANG",
        "LANGUAGE",
        "LOG_DIR",
        "NoDefaultCurrentDirectoryInExePath",
        "OPS_ALERT_EVENTS",
        "OPS_ALERT_IGNORE",
        "OPS_ALERT_MIN_LEVEL",
        "OPS_ALERT_WEBHOOK_URL",
        "PATHEXT",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "REQUIRE_REAL_DATA_SLICE",
        "RUNNER_TEMP",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TP_INTEGRATION_BASE_URL",
        "TP_TEST_OHLCV_DIR",
        "TRADING_DATA_DIR",
        "TRADING_PRODUCTION_DB",
        "TZ",
        "windir",
    }
)
_SANDBOX_ENV_ALLOWLIST_PREFIXES = ("PYTHON", "LC_", "GITHUB_", "PYTEST_XDIST")
# Where a sandboxed HOME points: inside the session sandbox, never the caller's home.
SANDBOX_HOME = _SESSION_ROOT / "home"



def _is_allowlisted_env(name: str) -> bool:
    """Whether *name* is part of the environment the suite may observe."""
    if name in _SANDBOX_ENV_ALLOWLIST or name.startswith(_SANDBOX_ENV_ALLOWLIST_PREFIXES):
        return True
    if name == CHILD_ENV_OVERRIDES_ENV_VAR:
        return True
    inherited = os.environ.get(CHILD_ENV_OVERRIDES_ENV_VAR, "")
    return bool(inherited) and name in inherited.split(",")


def _enforce_env_allowlist() -> None:
    """Clear the caller's environment down to the allowlist.

    Runs in every process that imports this file, xdist workers included: an
    xdist worker inherits the controller's already-swept environment, so sweeping
    again changes nothing but HOME, while an environment variable *inherited
    further down* (a child of a worker, through a shell script) must not be able
    to silence the sweep - a process that merely carries ``PYTEST_XDIST_WORKER``
    is not a worker, and treating it like one would hand every descendant the
    unswept environment. The identity variables xdist's bootstrap installs are
    on the allowlist (``PYTEST_XDIST*``), so they survive it either way.
    """
    for name in [name for name in os.environ if not _is_allowlisted_env(name)]:
        os.environ.pop(name, None)
    os.environ["HOME"] = str(SANDBOX_HOME)
    if os.name == "nt":
        os.environ["USERPROFILE"] = str(SANDBOX_HOME)
    else:
        os.environ.pop("USERPROFILE", None)


_enforce_env_allowlist()
SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
# Holds the ``sitecustomize`` a sandboxed child imports at startup - inside the session sandbox,
# so the suite leaves nothing behind in the checkout and a child cannot pick a different one.
CHILD_SANDBOX_SHIM_DIR = _SESSION_ROOT / "child-sandbox"


def sandboxed_env(*, shares_sandbox: bool = True, **overrides: str) -> dict[str, str]:
    """The environment a subprocess a test starts must be handed.

    A child inherits this process's environment but none of its guards: the audit
    hook, the ``load_dotenv`` redirect and the patched ``*ACCOUNTS_ENV*`` constants
    all live in this process's memory, so a plain ``subprocess.run(...)`` hands the
    child the caller's whole environment and the child loads the checkout's real
    ``.env`` and resolves the real ``.accounts.env`` path when it imports a
    platform module.

    The returned mapping is the allowlisted environment plus the sandbox's own
    forced values, with *overrides* (``sandboxed_env(DATA_DIR=str(child_dir))``)
    applied last, and with a ``sitecustomize`` first on PYTHONPATH: Python imports
    it before any ``-c``/``-m`` code runs, it imports this conftest, and that
    installs the suite's guards inside the child as well. A ``PYTHONPATH``
    override is prepended rather than replaced, so a test that deliberately puts
    another tree on the path (the import-origin probe) keeps that shadowing while
    the shim stays in front of it.

    ``shares_sandbox=False`` is for a child that runs the repo's own suite as a
    pytest process: such a child loads this conftest through pytest, and sharing
    the parent's session directory would give it the parent's session instead of
    its own - so it gets the swept environment and sets up its own sandbox, the
    way a standalone run does.

    Lifetime: a child that shares the sandbox is owned by this pytest process.
    The session directory is removed when that process exits, so such a child must
    be finished before then - started, driven and joined (or killed) inside the
    test that started it.
    """
    env = {name: value for name, value in os.environ.items() if _is_allowlisted_env(name)}
    env["DATA_DIR"] = os.environ["DATA_DIR"]
    env["TRADING_DATA_DIR"] = os.environ["TRADING_DATA_DIR"]
    env["LOG_DIR"] = os.environ["LOG_DIR"]
    env["OPS_ALERT_WEBHOOK_URL"] = ""
    env["OPS_ALERT_MIN_LEVEL"] = ""
    env["OPS_ALERT_EVENTS"] = ""
    env["OPS_ALERT_IGNORE"] = ""
    env.pop(ACCOUNTS_ENV_ENV_VAR, None)
    env["HOME"] = str(SANDBOX_HOME)
    if os.name == "nt":
        env["USERPROFILE"] = str(SANDBOX_HOME)
    else:
        env.pop("USERPROFILE", None)
    caller_pythonpath = overrides.pop("PYTHONPATH", None)
    pythonpath: list[str] = []
    if shares_sandbox:
        # The shim goes first, always: ``sitecustomize`` is the first one Python finds on
        # sys.path, and a caller-provided one (a fixture tree, or a stray file in a tree a
        # test happens to pass) must not be able to pre-empt the guard this child starts with.
        pythonpath.append(str(CHILD_SANDBOX_SHIM_DIR))
    if caller_pythonpath:
        # The caller's own path next, so it still shadows the checkout's packages - the
        # import-origin probe depends on that - but not the shim.
        pythonpath.extend(part for part in caller_pythonpath.split(os.pathsep) if part)
    pythonpath.append(str(PROJECT_ROOT))
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env.update(overrides)
    if not shares_sandbox:
        return env
    if overrides:
        # Named, not just applied: the child imports this file through the shim, and its sweep
        # must keep the variables the naming test meant it to see. A child that starts its own
        # child adds to the list rather than replacing it.
        inherited = [name for name in env.get(CHILD_ENV_OVERRIDES_ENV_VAR, "").split(",") if name]
        env[CHILD_ENV_OVERRIDES_ENV_VAR] = ",".join(dict.fromkeys([*inherited, *overrides]))
    # The xdist identity belongs to the worker xdist started, never to a child of it: a
    # plain child that carries the marker would look like a worker to anything that
    # checks, and the marker has no meaning in a process execnet did not start.
    for name in [name for name in env if name.startswith("PYTEST_XDIST")]:
        env.pop(name, None)
    env["TP_TEST_CHILD_SANDBOX"] = "1"
    env["TP_TEST_SESSION_ROOT"] = str(_SESSION_ROOT)
    env["TP_TEST_REPO_ROOT"] = str(PROJECT_ROOT)
    return env


def _write_child_sandbox_shim() -> None:
    """Write the ``sitecustomize`` every sandboxed child imports at startup.

    Importing this conftest is what gives the child the suite's guards: the audit
    hook that refuses the checkout's secret files and the network, the
    ``load_dotenv`` redirect, and - under the child flag - the lazy
    accounts-env isolation below instead of the suite's eager one. A child that
    starts without this environment (one a test forgot to hand it to) is exactly
    the leak ``tests/test_subprocess_env_contract.py`` fails on.
    """
    CHILD_SANDBOX_SHIM_DIR.mkdir(parents=True, exist_ok=True)
    (CHILD_SANDBOX_SHIM_DIR / "sitecustomize.py").write_text(
        "import importlib.util\n"
        "import os\n"
        "import sys\n"
        "\n"
        'if os.environ.get("TP_TEST_CHILD_SANDBOX") == "1":\n'
        "    _repo = os.environ['TP_TEST_REPO_ROOT']\n"
        "    _shim = os.path.dirname(os.path.abspath(__file__))\n"
        "    _path = os.path.join(_repo, 'tests', 'conftest.py')\n"
        "    _spec = importlib.util.spec_from_file_location('tp_test_conftest', _path)\n"
        "    _module = importlib.util.module_from_spec(_spec)\n"
        "    sys.modules['tp_test_conftest'] = _module\n"
        "    sys.modules['tests.conftest'] = _module\n"
        "    _spec.loader.exec_module(_module)\n"
        "    # A caller-provided sitecustomize runs after the guard, never before it: this file\n"
        "    # is first on the child's PYTHONPATH so a stray one cannot pre-empt the audit\n"
        "    # hook, the .env redirect or the accounts-env guard. The first caller entry that\n"
        "    # holds one gets it run here, which is the one Python itself would have imported.\n"
        "    for _entry in os.environ.get('PYTHONPATH', '').split(os.pathsep):\n"
        "        if _entry in (_shim, _repo):\n"
        "            continue\n"
        "        _candidate = os.path.join(_entry, 'sitecustomize.py')\n"
        "        if os.path.isfile(_candidate):\n"
        "            _spec = importlib.util.spec_from_file_location(\n"
        "                'caller_sitecustomize', _candidate\n"
        "            )\n"
        "            _module = importlib.util.module_from_spec(_spec)\n"
        "            sys.modules['caller_sitecustomize'] = _module\n"
        "            _spec.loader.exec_module(_module)\n"
        "            break\n",
        encoding="utf-8",
    )


if not _CHILD_PROCESS:
    # Only the parent writes it: every child starts from the same bytes, and a child
    # rewriting the file another child is about to import is a race for no gain.
    _write_child_sandbox_shim()


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

# The node id of the test currently in its setup/call/teardown window, or ``None``
# for collection, session hooks and work a leaked thread does between tests. The
# audit hook runs on whichever thread made the socket call, so a violation is
# attributed to the test *running* on this thread only implicitly: a background
# thread spawned by a test fires while some other test (or no test) is current.
# The thread name is recorded alongside so a leaked worker stands out.
_CURRENT_TEST_NODEID: str | None = None

# Hits on the Dukascopy network-edge stubs (``_no_dukascopy_sdk_network``).
# Recorded and failed at session end like ``_NETWORK_VIOLATIONS``: the stub
# already turned what would have been a hermeticity violation into an ordinary
# downloader error, so without this handoff a swallowed stub hit would fail
# nothing. A test that exercises a stub on purpose consumes its own hits (see
# ``_consume_stub_hits`` in tests/test_suite_isolation.py).
_DUKASCOPY_STUB_HITS: list[str] = []


def dukascopy_stub_hits() -> tuple[str, ...]:
    """Every blocked Dukascopy/JForex network-edge call this session saw."""
    return tuple(_DUKASCOPY_STUB_HITS)


def _test_phase_label() -> str:
    if _CURRENT_TEST_NODEID is None:
        return "no test running"
    return f"test {_CURRENT_TEST_NODEID}"


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
    _NETWORK_VIOLATIONS.append(
        f"{action} {host!r} during {_test_phase_label()} "
        f"(thread {threading.current_thread().name})"
    )
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
    ("live_runner.loss_breaker", "_ACCOUNTS_ENV"),
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


class _ChildAccountsEnvLoader:
    """Wrap one module loader so its accounts-env constants are patched after import.

    Everything but ``exec_module`` is delegated, through ``__getattr__`` for the parts this
    class does not name: ``python -m`` reads a loader's ``get_code``, and a wrapper that
    only spoke the import protocol would break every ``-m`` child.
    """

    def __init__(self, loader: object) -> None:
        self._loader = loader

    def create_module(self, spec: object) -> object:
        return self._loader.create_module(spec)  # type: ignore[attr-defined]

    def exec_module(self, module: object) -> None:
        self._loader.exec_module(module)  # type: ignore[attr-defined]
        for name, value in list(vars(module).items()):  # type: ignore[arg-type]
            if "ACCOUNTS_ENV" not in name or isinstance(value, (bool, int, float)):
                continue
            if _repo_secret_path(value) == REPO_ACCOUNTS_ENV:
                setattr(module, name, ISOLATED_ACCOUNTS_ENV)

    def __getattr__(self, name: str) -> object:
        return getattr(self._loader, name)


class _ChildAccountsEnvFinder:
    """Patch the accounts-env constants of every first-party module, lazily.

    A child cannot run the suite's eager isolation: importing the roster's eleven modules before
    the child's own code starts would make a ``--help`` probe as heavy as the daemon it is meant
    to avoid importing. Wrapping the loader instead patches each module as it finishes importing,
    in dependency order, so a module that binds the name from another one binds the patched value
    exactly like the suite's own sweep.

    The wrap is by file, not by package name: the accounts-env constant lives in ``live_runner``
    and ``scripts`` today, and a constant that appears in another first-party package tomorrow
    (``shared``, ``viewer``) must be patched the same way. Only modules whose file is inside this
    checkout are wrapped, so stdlib and site-packages imports keep their own loaders.
    """

    #: The checkout root, spelled the way the path entries a child imports through are. The
    #: resolved spelling is what the sandbox builds its PYTHONPATH from, so it hits on the
    #: fast path in every ordinary child.
    _REPO_PREFIXES = tuple(
        str(root) + os.sep for root in (PROJECT_ROOT, Path(__file__).parent.parent)
    )

    def _is_repo_file(self, origin: str) -> bool:
        """Whether *origin* is a file inside this checkout, by any spelling of the root.

        A child can reach the checkout through a path the prefixes do not match - a symlink,
        an editable install, a path with a redundant component - and an unwrapped module is a
        leak the audit hook only catches on open. The prefix check is the fast path for the
        ordinary spelling; anything else is resolved, which is a handful of stat calls per
        module the ordinary spelling never pays for.
        """
        if origin.startswith(self._REPO_PREFIXES):
            return True
        try:
            return Path(origin).resolve().is_relative_to(PROJECT_ROOT)
        except (OSError, ValueError):
            return False

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)  # type: ignore[attr-defined]
            if spec is None:
                continue
            origin = getattr(spec, "origin", None)
            if (
                origin
                and spec.loader is not None
                and hasattr(spec.loader, "exec_module")
                and self._is_repo_file(str(origin))
            ):
                spec.loader = _ChildAccountsEnvLoader(spec.loader)
            return spec
        return None


def _install_child_accounts_env_guard() -> None:
    """Give a child the accounts-env isolation the suite applies to itself.

    The audit hook refuses the checkout's real ``.accounts.env`` in a child too, but refusing an
    open is a backstop: the child must resolve the *session sandbox* path in the first place, or a
    reader that never opens the file (a discovery that only lists what the path's parent holds)
    still looks at the checkout.
    """
    if not _CHILD_PROCESS:
        return
    finder = _ChildAccountsEnvFinder()
    if not any(existing is finder for existing in sys.meta_path):
        sys.meta_path.insert(0, finder)


if _CHILD_PROCESS:
    # A child imports this conftest from the shim, before its own code: the guard has to be in
    # place by then, and the eager roster would import eleven modules to do what the finder does
    # on the module the child actually needs.
    _install_child_accounts_env_guard()
else:
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
        changes.extend(
            f"{root}: {change}" for change in diff_snapshots(before, snapshot_tree(Path(root)))
        )
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

    stub_hits = dukascopy_stub_hits()
    if stub_hits:
        shown = "\n  ".join(dict.fromkeys(stub_hits))
        failures.append(
            "\n\nERROR: the Dukascopy/JForex network-edge stubs blocked calls that would have"
            f" reached the network:\n  {shown}"
            "\nEach became an ordinary downloader error instead of a socket violation, so nothing"
            " else would have failed. A test reached a real download path: give it a fixture, a"
            " stub transport or localhost, or an explicit patch of the fetch edge. A test that"
            " exercises a stub on purpose must consume its own hits."
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


@pytest.fixture(autouse=True)
def _no_dukascopy_sdk_network():
    """Keep tests off the two Dukascopy network edges: the JNLP/JAR bootstrap
    and the native ``.bi5`` datafeed fetch.

    Viewer download tasks run on background threads that can outlive the
    per-test monkeypatch window (and the JVM prewarm patch above only covers
    ``prewarm_jvm``), so under a randomized order an accidental real fetch
    fires while an unrelated test - or no test - is current. Stubbing the
    edges themselves turns that leak into an ordinary task failure inside the
    downloader, which its callers already handle - and every hit is recorded
    and fails the session at :func:`pytest_sessionfinish` like a network
    violation, so a stub hit can never pass silently. Tests that exercise the
    fetchers patch the same attributes themselves; a test-local
    ``monkeypatch.setattr`` wins for the duration of the test and this
    fixture's undo restores the real callable afterwards. Both modules are
    import-safe here: ``jforex_dl`` guards its ``jpype`` import and neither
    module touches the network or starts a JVM at import time.
    """
    from data_tools.downloaders import dukascopy_native, jforex_dl

    def _blocked_urlopen(*args, **kwargs):
        _DUKASCOPY_STUB_HITS.append(
            f"jforex_dl.urlopen during {_test_phase_label()} "
            f"(thread {threading.current_thread().name})"
        )
        raise RuntimeError(
            "the suite must not fetch JForex SDK jars over the network; "
            "patch jforex_dl.urlopen or seed JFOREX_CLASSPATH/JFOREX_JARS"
        )

    async def _blocked_fetch_one(*args, **kwargs):
        _DUKASCOPY_STUB_HITS.append(
            f"dukascopy_native._fetch_one during {_test_phase_label()} "
            f"(thread {threading.current_thread().name})"
        )
        raise RuntimeError(
            "the suite must not fetch Dukascopy .bi5 files over the network; "
            "patch dukascopy_native._fetch_one or download_bi5_files"
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jforex_dl, "urlopen", _blocked_urlopen)
        patch.setattr(dukascopy_native, "_fetch_one", _blocked_fetch_one)
        yield


@pytest.fixture(autouse=True)
def _no_viewer_interrupted_task_resume():
    """Keep ``TestClient(app)`` lifespans from resuming persisted download tasks.

    The viewer lifespan marks queued/running ``BackgroundTaskRecord`` rows
    interrupted and ``_resume_interrupted_tasks`` re-dispatches them - including
    Dukascopy/JForex downloads that spawn ``run_download`` threads. Task records
    live in the session-shared DATA_DIR database, so a lifespan entered by one
    test resumes whatever an earlier test left behind, and the resumed worker
    threads then run real fetches while unrelated tests are current (the leak
    ``_no_dukascopy_sdk_network`` can only blunt, not prevent).

    Only the lifespan entry point is stubbed: tests that exercise resume call
    ``viewer.main._resume_interrupted_tasks`` directly and still see the real
    function. ``app.router.lifespan_context`` is what ``TestClient`` runs on
    ``__enter__``, so wrapping it covers every ``with TestClient(app)`` in the
    suite without touching production code. ``viewer.main`` is imported here so
    a test that imports it lazily still gets the wrapped app.
    """
    import viewer.main as viewer_main_module

    app = viewer_main_module.app
    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_without_task_resume(lifespan_app):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(viewer_main_module, "_resume_interrupted_tasks", lambda: None)
            async with original_lifespan(lifespan_app):
                yield

    app.router.lifespan_context = _lifespan_without_task_resume
    try:
        yield
    finally:
        app.router.lifespan_context = original_lifespan


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


@contextlib.contextmanager
def _viewer_state_lock(lock, name: str):
    """Acquire a viewer state lock with a timeout so a leaked thread holding
    it fails the test loudly instead of hanging the whole suite."""
    if not lock.acquire(timeout=5):
        raise RuntimeError(
            f"viewer state reset timed out acquiring {name}: a thread leaked "
            "by an earlier test still holds the lock"
        )
    try:
        yield
    finally:
        lock.release()


def _join_viewer_task_threads(tasks: list, *, timeout: float = 2.0) -> None:
    """Signal and briefly join viewer worker threads a test left behind.

    Download/backtest/agent tasks run on daemon threads registered as
    ``task["thread"]``; a worker still alive at reset time would keep fetching
    or writing while later tests are current. ``stop_event`` asks the
    cooperative loops to exit, then each thread gets a short join - the threads
    are daemon, so one that ignores the signal cannot pin the interpreter, but
    joining forever would hang the suite instead.
    """
    for task in tasks:
        if not isinstance(task, dict):
            continue
        stop_event = task.get("stop_event")
        if stop_event is not None:
            stop_event.set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        thread = task.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            thread.join(timeout=timeout)


def _reset_viewer_runtime_state(viewer_main_module=None) -> None:
    """Clear viewer module state that must not cross test boundaries.

    The viewer keeps process-global caches for strategies, live accounts,
    backtest/portfolio metadata, data views and task hydration. Several of
    those caches are not keyed by the monkeypatched ``DATA_DIR`` /
    ``STRATEGIES_DIR`` a test is using, so a payload built against one
    fixture's tmp tree can otherwise be served to the next test for the
    cache TTL (this is the #236 strategy-list leak).
    """
    module = viewer_main_module or sys.modules.get("viewer.main")
    # ``viewer.core`` is the legacy module ``viewer.main`` wraps; a session
    # that imported only ``viewer.core`` still needs its OHLCV cache reset.
    legacy_module = (
        getattr(module, "legacy_viewer", None)
        if module is not None
        else sys.modules.get("viewer.core")
    )
    if module is None and legacy_module is None:
        return
    if module is not None:
        with _viewer_state_lock(module._STRATEGIES_CACHE_LOCK, "_STRATEGIES_CACHE_LOCK"):
            module._STRATEGIES_CACHE["payload"] = []
            module._STRATEGIES_CACHE["expires_at"] = 0.0
        with _viewer_state_lock(module._LIVE_ACCOUNTS_CACHE_LOCK, "_LIVE_ACCOUNTS_CACHE_LOCK"):
            module._LIVE_ACCOUNTS_CACHE["expires_at"] = 0.0
            module._LIVE_ACCOUNTS_CACHE["payload"] = []
            module._LIVE_ACCOUNTS_CACHE["provider_marker"] = None
        with _viewer_state_lock(module._BACKTEST_META_CACHE_LOCK, "_BACKTEST_META_CACHE_LOCK"):
            module._BACKTEST_META_CACHE.clear()
            module._BACKTEST_META_CACHE_SOURCE_MTIME_NS = 0
        with _viewer_state_lock(
            module._BACKTEST_EQUITY_PREVIEW_CACHE_LOCK,
            "_BACKTEST_EQUITY_PREVIEW_CACHE_LOCK",
        ):
            module._BACKTEST_EQUITY_PREVIEW_CACHE.clear()
        with _viewer_state_lock(module._PORTFOLIO_META_CACHE_LOCK, "_PORTFOLIO_META_CACHE_LOCK"):
            module._PORTFOLIO_META_CACHE.clear()
            module._PORTFOLIO_META_CACHE_WARMED = False
        with _viewer_state_lock(module._TASK_CACHE_HYDRATION_LOCK, "_TASK_CACHE_HYDRATION_LOCK"):
            module._TASK_CACHE_HYDRATED = False
        with _viewer_state_lock(module._DATA_VIEWS_CACHE_LOCK, "_DATA_VIEWS_CACHE_LOCK"):
            module._invalidate_data_views_cache()
            module._DATA_VIEWS_PREWARM_DONE = False
        with _viewer_state_lock(
            module._COALESCED_CLEANUP_STATE_LOCK, "_COALESCED_CLEANUP_STATE_LOCK"
        ):
            module._COALESCED_CLEANUP_PENDING.clear()
            module._COALESCED_CLEANUP_ORDER.clear()
            module._COALESCED_CLEANUP_ATTEMPTS.clear()
            module._COALESCED_CLEANUP_EXHAUSTED.clear()
    if legacy_module is not None:
        # DATA_TASKS lives in viewer.core (viewer.main re-exports it), so it
        # is cleared here to cover sessions that never import viewer.main.
        with _viewer_state_lock(legacy_module.DATA_TASKS_LOCK, "DATA_TASKS_LOCK"):
            leftover_tasks = list(legacy_module.DATA_TASKS.values())
            legacy_module.DATA_TASKS.clear()
        _join_viewer_task_threads(leftover_tasks)
        with _viewer_state_lock(legacy_module._OHLCV_DF_CACHE_LOCK, "_OHLCV_DF_CACHE_LOCK"):
            legacy_module._OHLCV_DF_CACHE.clear()
    from data_tools import cache_manager

    cache_manager.invalidate_cache()


@pytest.fixture(autouse=True)
def _viewer_runtime_state_reset():
    """Keep viewer caches/task state isolated between tests."""
    _reset_viewer_runtime_state()
    yield
    _reset_viewer_runtime_state()


@pytest.fixture(scope="session")
def viewer_app():
    """The shared viewer FastAPI app, with caches reset before first use."""
    import viewer.main as viewer_main_module

    _reset_viewer_runtime_state(viewer_main_module)
    return viewer_main_module.app


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
def viewer_client(monkeypatch, tmp_path, viewer_app):
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
    _reset_viewer_runtime_state(viewer_app_module)
    return _TestClient(viewer_app)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    global _CURRENT_TEST_NODEID
    _CURRENT_TEST_NODEID = item.nodeid
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


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Clear the attributed node id once the test's fixtures have torn down.

    ``trylast`` keeps the id set while fixture finalizers run, so a network
    attempt inside a ``yield`` fixture's teardown still names its test. A
    violation recorded after this point belongs to no test - the signature of
    a background thread a test leaked.
    """
    global _CURRENT_TEST_NODEID
    _CURRENT_TEST_NODEID = None


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


@pytest.fixture(autouse=True)
def _isolate_loss_breaker_state():
    """Give every test a clean ``live_loss_breaker_state``.

    The loss breaker (#301) latches on purpose: a daily latch holds until the
    UTC rollover and a drawdown latch until an operator reset, so one test's
    tripped breaker would refuse every later entry on the same account scope
    inside the worker's shared database. Tests that exercise the breaker set
    up and assert its state inside a single test, so clearing before each one
    is isolation, not a weakened assertion. Deletes through the same engine
    ``get_session`` uses; ``init_db`` runs first so the tables exist, and a
    cleanup failure fails the test rather than silently leaking breaker
    state into the next one.
    """
    from sqlalchemy import text

    import live_runner.loss_breaker as loss_breaker
    import shared.database as database

    # init_db first: the deletes below must hit real tables, and a failure
    # here is an isolation failure worth surfacing, not swallowing.
    database.init_db()
    with database.get_engine().begin() as conn:
        conn.execute(text("DELETE FROM live_loss_breaker_state"))
        # ``ledger_reconciliation_*`` audit rows also latch breaker behaviour
        # (deployment P&L fails closed) and, unlike the state table, nothing
        # else clears them between tests.
        conn.execute(
            text(
                "DELETE FROM live_control_audit "
                "WHERE action IN "
                "('ledger_reconciliation_required', 'ledger_reconciliation_resolved')"
            )
        )
    # Process-local breaker caches must not leak between tests either: a
    # latched unmeasurable-P&L alert or a cached pip value would silently
    # change a later test's behaviour.
    loss_breaker._PNL_UNMEASURABLE_ALERTED.clear()
    loss_breaker._PIP_VALUE_CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def _isolate_loss_breaker_accounts_env():
    """Keep the loss breaker's ``.accounts.env`` at the session sandbox path.

    The path itself is redirected for the whole session through
    ``ACCOUNTS_ENV_MODULES`` (``live_runner.loss_breaker._ACCOUNTS_ENV``), which
    resolves to a file that does not exist; this fixture only guarantees no test
    inherits a ``set_accounts_env_file`` override from an earlier one.

    ``refuse_entry_if_loss_breaker_tripped`` and the balance-poll observation
    resolve ``LOSS_BREAKER_*`` thresholds file-first through the repo's
    ``.accounts.env`` when no ``env_values`` mapping is passed. Without this
    isolation every daemon test that reaches ``prepare_order_with_risk`` would
    parse the live credentials file, and a ``LOSS_BREAKER_*`` typo there would
    fail unrelated tests - the same reason ``DATA_DIR`` is isolated above.
    Process-env ``LOSS_BREAKER_*`` variables still apply on top, matching
    production precedence.

    Uses its own ``MonkeyPatch`` rather than the ``monkeypatch`` fixture, for
    the same reason as ``_no_jforex_jvm_prewarm`` above: an autouse fixture
    that requested it would instantiate it before every test's other fixtures
    and delay its undo until after them.
    """
    import live_runner.loss_breaker as loss_breaker

    with pytest.MonkeyPatch.context() as patch:
        # ``run_daemon`` may point the breaker at a configured accounts file;
        # tests must never inherit that override from an earlier test.
        patch.setattr(loss_breaker, "_ACCOUNTS_ENV_OVERRIDE", None)
        yield


@pytest.fixture(autouse=True)
def _isolate_daemon_shutdown_gate():
    """Give every test a fresh daemon shutdown coordinator.

    ``run_daemon`` marks its coordinator requested on every exit path - that is
    the submission gate doing its job - and ``_ACTIVE_SHUTDOWN`` is restored to
    the process-wide ``_SHUTDOWN`` afterwards. Without isolation a test that ran
    the daemon leaves the gate closed for every later test in the same process
    (and, under xdist, the same worker), so submissions that should reach the
    broker are refused with "daemon is shutting down".

    Uses its own ``MonkeyPatch`` rather than the ``monkeypatch`` fixture, for
    the same reason as ``_isolate_loss_breaker_accounts_env`` above.
    """
    import live_runner.daemon as daemon
    from live_runner.shutdown import ShutdownCoordinator

    fresh = ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("forced exit"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "_SHUTDOWN", fresh)
        patch.setattr(daemon, "_ACTIVE_SHUTDOWN", fresh)
        yield
