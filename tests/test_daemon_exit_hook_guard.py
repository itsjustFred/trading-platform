"""Guard for the live daemon's ``os._exit`` contract: no teardown hooks.

``live_runner.daemon.main`` leaves through ``os._exit`` on every path - clean
shutdown, timed-out phase and crash alike - because interpreter teardown joins
every thread and executor worker the daemon left alive, with no deadline, past
the unit's ``TimeoutStopSec``. ``os._exit`` runs none of that teardown, which is
the point: nothing the daemon needs on the way out may live there.

That makes every interpreter-teardown hook a silent break of the contract: the
hook simply never runs, and whatever it was supposed to flush, close or report
is lost without an error anywhere. A maintainer decision on the graceful
shutdown work accepted the ``os._exit`` contract on the condition that a guard
test fails if ``atexit.register`` - or a similar exit hook - appears in daemon
code, so the contract cannot be broken silently.

This file imports ``live_runner.daemon`` in a clean interpreter and scans every
repo module that lands in ``sys.modules`` - the whole process, not just
``live_runner/``: the daemon also runs ``shared/``, ``risk/``, ``scheduler/``,
``signal_bots/`` and the ``runner/``/``data_tools/`` modules they pull in. A
teardown hook in any of them never runs either. Only first-party modules are
scanned: third-party packages (which the probe finds whenever the venv lives
inside the checkout, e.g. ``.venv/lib/.../site-packages/``) are excluded -
their exit hooks are equally skipped under ``os._exit``, but policing
their internals is not this guard's job. It fails on:

* ``atexit`` imports and registrations (``atexit.register``, ``__import__`` or
  ``importlib`` loading of the module);
* ``weakref.finalize`` and ``*.addfinalizer(...)`` callbacks;
* ``__del__`` methods, which interpreter teardown would otherwise run;
* signal-handler installation outside ``live_runner/shutdown.py`` - the one
  place the ``ShutdownCoordinator`` deliberately installs SIGINT/SIGTERM via
  ``signal.signal`` - including ``loop.add_signal_handler`` and self-signals
  (``raise_signal``/``pthread_kill``) anywhere;
* ``sys.exit``/``exit()``/``quit()``/``raise SystemExit`` and ``os._exit``
  outside the two files that own the bounded exit path (``daemon.py``'s
  ``_leave_the_process`` and ``shutdown.py``'s second-signal ``force_exit``);
* ``multiprocessing`` and ``ProcessPoolExecutor``, whose pools register their
  own interpreter-teardown joins;
* ``threading._register_atexit``, CPython's private atexit hook.

The checks are AST-based so the contract's own docstrings - which name
``atexit`` and ``__del__`` to explain why they never run - do not trip them.
Code inside ``if __name__ == "__main__":`` blocks is exempt: it only runs when
the file is executed as a script, never when the daemon imports the module.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_RUNNER = REPO_ROOT / "live_runner"

# First-party top-level packages: directories in the checkout that are Python
# packages. Anything else under the repo root - ``.venv/`` when the venv lives
# inside the checkout (CI's ``uv sync`` layout), an editable-install target, a
# vendored tree - is third-party or tooling code, not daemon surface.
_FIRST_PARTY_PACKAGES = frozenset(
    p.name
    for p in REPO_ROOT.iterdir()
    if p.is_dir() and (p / "__init__.py").is_file()
)

# ``os._exit`` is the contract itself; it may only be reached from the bounded
# exit path and the second-signal force exit. Keyed by repo-relative path, not
# bare filename: the scan is process-wide, so any other ``*/daemon.py`` must
# not inherit the exemption.
OS_EXIT_ALLOWED = {"live_runner/daemon.py", "live_runner/shutdown.py"}

# ``signal.signal`` is the shutdown coordinator's deliberate choice (it works on
# Windows and while the loop is busy); no other daemon module may install a
# handler, and ``add_signal_handler`` is banned everywhere because the
# coordinator's docstring records why it was rejected. Self-signals are held to
# the same rule: they exist to drive the handler path, which only the
# coordinator owns. Keyed by repo-relative path for the same reason as above.
SIGNAL_INSTALL_ALLOWED = {"live_runner/shutdown.py"}
_SIGNAL_FUNCTIONS = {"signal", "raise_signal", "pthread_kill"}

_BANNED_MODULES = {"atexit", "multiprocessing"}


def _dotted(node: ast.expr, module_aliases: dict[str, str]) -> str | None:
    """The dotted path an expression refers to, or None if not module-based."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = module_aliases.get(node.id)
    if base is None:
        return None
    parts.append(base)
    return ".".join(reversed(parts))


def _is_main_guard(test: ast.expr) -> bool:
    """``__name__ == "__main__"`` (either operand order)."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left_name = isinstance(test.left, ast.Name) and test.left.id == "__name__"
    right_main = (
        isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )
    left_main = (
        isinstance(test.left, ast.Constant)
        and test.left.value == "__main__"
    )
    right_name = (
        isinstance(test.comparators[0], ast.Name)
        and test.comparators[0].id == "__name__"
    )
    return (left_name and right_main) or (left_main and right_name)


def _scan_source(rel_path: str, source: str) -> list[str]:
    """Every teardown-hook violation in one daemon-process module's source."""
    tree = ast.parse(source, filename=rel_path)
    # Allowlist keys are repo-relative paths with ``/`` separators, matching
    # what ``_daemon_process_modules`` emits on every platform.
    rel_key = rel_path.replace("\\", "/")

    # Nodes inside ``if __name__ == "__main__":`` only run when the file is the
    # process entry point; the daemon imports the module, so they are not part
    # of the surface this guard protects.
    main_guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            for child in node.body:
                main_guarded.update(id(sub) for sub in ast.walk(child))

    # `import a.b [as c]`: c (or top package a) -> dotted module path.
    module_aliases: dict[str, str] = {}
    # `from a.b import c [as d]`: d -> (dotted module, original name).
    from_names: dict[str, tuple[str, str]] = {}
    violations: list[str] = []

    for node in ast.walk(tree):
        node_guarded = id(node) in main_guarded
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BANNED_MODULES and not node_guarded:
                    violations.append(
                        f"{rel_path}:{node.lineno} imports {alias.name}"
                    )
                module_aliases[alias.asname or top] = (
                    alias.name if alias.asname else top
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            for alias in node.names:
                local = alias.asname or alias.name
                from_names[local] = (module, alias.name)
                if node_guarded:
                    continue
                if top in _BANNED_MODULES:
                    violations.append(
                        f"{rel_path}:{node.lineno} imports from {module}"
                    )
                elif module == "weakref" and alias.name == "finalize":
                    violations.append(
                        f"{rel_path}:{node.lineno} imports weakref.finalize"
                    )
                elif (
                    module == "threading"
                    and alias.name == "_register_atexit"
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno} imports "
                        "threading._register_atexit"
                    )

    for node in ast.walk(tree):
        if id(node) in main_guarded:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__del__":
                violations.append(f"{rel_path}:{node.lineno} defines __del__")

        if isinstance(node, ast.Attribute):
            # Any load of these attributes is the hook itself (or its handle);
            # a call adds nothing the load did not already flag.
            if _dotted(node.value, module_aliases) == "weakref" and (
                node.attr == "finalize"
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} uses weakref.finalize"
                )
            if _dotted(node.value, module_aliases) == "atexit" and (
                node.attr == "register"
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} uses atexit.register"
                )
            if node.attr == "_register_atexit":
                violations.append(
                    f"{rel_path}:{node.lineno} uses "
                    "threading._register_atexit"
                )

        if isinstance(node, ast.Raise):
            # ``raise SystemExit(...)`` is ``sys.exit`` with a different
            # spelling; the bounded exit path owns every exit.
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(raised, ast.Name) and raised.id == "SystemExit":
                violations.append(
                    f"{rel_path}:{node.lineno} raises SystemExit"
                )

        if not isinstance(node, ast.Call):
            continue

        func = node.func
        name: str | None = None
        module: str | None = None
        func_is_attribute = isinstance(func, ast.Attribute)
        if func_is_attribute:
            assert isinstance(func, ast.Attribute)
            name = func.attr
            module = _dotted(func.value, module_aliases)
            # `x.y.addfinalizer(...)` / `loop.add_signal_handler(...)`:
            # receiver is not a module we track, but the method name is the
            # hook regardless of what it is bound to.
            if module is None and name in {"addfinalizer", "add_signal_handler"}:
                violations.append(
                    f"{rel_path}:{node.lineno} calls .{name}()"
                )
            if name == "ProcessPoolExecutor":
                violations.append(
                    f"{rel_path}:{node.lineno} creates a ProcessPoolExecutor"
                )
        elif isinstance(func, ast.Name):
            if func.id in {"exit", "quit", "_register_atexit"}:
                violations.append(f"{rel_path}:{node.lineno} calls {func.id}()")
            resolved = from_names.get(func.id)
            if resolved is not None:
                module, name = resolved

        if module is None or name is None:
            # Dynamic module loads smuggling a banned module past the
            # import checks: __import__("atexit"), importlib.import_module(...).
            if (
                func_is_attribute
                and isinstance(func, ast.Attribute)
                and func.attr in {"__import__", "import_module"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _BANNED_MODULES
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} loads "
                    f"{node.args[0].value} dynamically"
                )
            continue

        if func_is_attribute and module in {"atexit", "weakref"}:
            # `atexit.register(...)` / `weakref.finalize(...)`: the attribute
            # load above already flagged the hook; a from-imported bare name
            # reaches here with func_is_attribute False and is flagged below.
            continue
        if module in _BANNED_MODULES:
            violations.append(f"{rel_path}:{node.lineno} calls {module}.{name}()")
        elif module == "weakref" and name == "finalize":
            violations.append(f"{rel_path}:{node.lineno} calls weakref.finalize()")
        elif name == "addfinalizer":
            violations.append(f"{rel_path}:{node.lineno} calls .addfinalizer()")
        elif name == "add_signal_handler":
            violations.append(
                f"{rel_path}:{node.lineno} calls add_signal_handler()"
            )
        elif (
            module == "signal"
            and name in _SIGNAL_FUNCTIONS
            and rel_key not in SIGNAL_INSTALL_ALLOWED
        ):
            violations.append(
                f"{rel_path}:{node.lineno} installs a signal handler"
            )
        elif module == "os" and name == "_exit":
            if rel_key not in OS_EXIT_ALLOWED:
                violations.append(f"{rel_path}:{node.lineno} calls os._exit()")
        elif module == "sys" and name == "exit":
            violations.append(f"{rel_path}:{node.lineno} calls sys.exit()")
        elif module == "concurrent.futures" and name == "ProcessPoolExecutor":
            violations.append(
                f"{rel_path}:{node.lineno} creates a ProcessPoolExecutor"
            )

    return violations


def _is_first_party(rel: str) -> bool:
    """Whether a repo-root-relative path is a first-party daemon module.

    First-party means the top-level segment is one of the checkout's Python
    packages (``live_runner/``, ``shared/``, ...). Everything else under the
    root - ``.venv/lib/.../site-packages/`` when CI's ``uv sync`` puts the venv
    inside the checkout, a stray ``venv/``, an editable install - is excluded,
    so third-party teardown hooks (Sentry's flush, certifi's temp files) are
    not policed here.
    """
    parts = rel.split("/")
    return (
        parts[0] in _FIRST_PARTY_PACKAGES
        and "site-packages" not in parts
        and "__pycache__" not in parts
    )


def _daemon_process_modules() -> list[str]:
    """Repo-relative ``.py`` paths imported by ``live_runner.daemon``.

    Runs in a clean interpreter so ``sys.modules`` is exactly the daemon's
    import closure, independent of what this pytest session already loaded.
    Only first-party modules are returned (see ``_is_first_party``).
    Paths use ``/`` separators so the result is portable.
    """
    probe = (
        "import json, os, sys\n"
        "import live_runner.daemon  # noqa: F401\n"
        "root = os.path.realpath(os.getcwd())\n"
        "files = set()\n"
        "for mod in list(sys.modules.values()):\n"
        "    f = getattr(mod, '__file__', None)\n"
        "    if not f or not f.endswith('.py'):\n"
        "        continue\n"
        "    ap = os.path.realpath(f)\n"
        "    if ap.startswith(root + os.sep):\n"
        "        files.add(os.path.relpath(ap, root).replace(os.sep, '/'))\n"
        "print(json.dumps(sorted(files)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in json.loads(proc.stdout) if _is_first_party(p)]


def test_daemon_process_modules_have_no_interpreter_teardown_hooks() -> None:
    """No module the daemon process imports may rely on interpreter teardown.

    The daemon leaves through ``os._exit``, so a hook in *any* module it
    imported - ``shared/``, ``risk/``, ``scheduler/``, ``signal_bots/`` and the
    ``runner/``/``data_tools/`` modules they pull in, not only ``live_runner/``
    - would break silently. Scanning the import closure keeps the guard honest
    if the daemon starts importing a new package.
    """
    rel_paths = _daemon_process_modules()
    assert rel_paths, "live_runner.daemon imported no repo modules"
    # Pin the scope: the packages the daemon is known to run must be present,
    # so a broken probe cannot silently narrow the scan back to live_runner/.
    for prefix in (
        "live_runner/",
        "shared/",
        "risk/",
        "scheduler/",
        "signal_bots/",
    ):
        assert any(p.startswith(prefix) for p in rel_paths), (
            f"daemon closure is missing {prefix}* modules"
        )
    assert "live_runner/daemon.py" in rel_paths
    assert "live_runner/shutdown.py" in rel_paths
    violations: list[str] = []
    for rel in rel_paths:
        path = REPO_ROOT / rel
        violations.extend(_scan_source(rel, path.read_text(encoding="utf-8")))
    assert violations == [], (
        "the daemon leaves through os._exit, so interpreter-teardown hooks never "
        "run; these would break silently:\n" + "\n".join(violations)
    )


def test_the_probe_scans_only_first_party_modules() -> None:
    """The scan must never reach third-party code, wherever the venv lives.

    CI's ``uv sync`` creates ``<repo>/.venv``, so third-party modules sit under
    the repo root and a bare ``startswith(root)`` filter would scan them -
    their ``__del__``/``atexit`` hooks are none of this guard's business, and
    scanning them made CI red. Pin both the filter and the emitted paths.
    """
    # Synthetic paths the probe can emit when the venv is inside the checkout.
    for third_party in (
        ".venv/lib/python3.12/site-packages/anyio/_core/_sockets.py",
        ".venv/lib/python3.12/site-packages/numpy/__init__.py",
        "venv/Lib/site-packages/certifi/core.py",
        "live_runner/vendor/site-packages/pkg/mod.py",
        "scripts/one_off.py",
        "docs/conf.py",
    ):
        assert not _is_first_party(third_party), third_party
    for first_party in (
        "live_runner/daemon.py",
        "shared/database.py",
        "risk/sizing.py",
    ):
        assert _is_first_party(first_party), first_party

    rel_paths = _daemon_process_modules()
    for rel in rel_paths:
        assert "site-packages" not in rel.split("/"), rel
        assert not rel.startswith((".venv/", "venv/")), rel
        assert _is_first_party(rel), rel


def test_the_guard_catches_a_planted_exit_hook() -> None:
    """The detector itself is pinned: a planted hook must be reported."""
    planted = """
import atexit
import threading
import weakref
import signal
import os
import sys
from concurrent.futures import ProcessPoolExecutor


def cleanup():
    pass


def bail():
    raise SystemExit(0)


class Thing:
    def __del__(self):
        pass


atexit.register(cleanup)
weakref.finalize(obj, cleanup)
obj.addfinalizer(cleanup)
threading._register_atexit(cleanup)
signal.signal(signal.SIGTERM, handler)
loop.add_signal_handler(signal.SIGTERM, handler)
os._exit(0)
sys.exit(1)
exit()
ProcessPoolExecutor()
"""
    # Scanned as modules that own no exit path, so even os._exit is banned -
    # once under live_runner/ and once under shared/, which the daemon also
    # runs, so a hook planted outside live_runner/ is still reported.
    for rel in ("live_runner/config_parser.py", "shared/sentry.py"):
        found = _scan_source(rel, planted)
        joined = "\n".join(found)
        for marker in (
            "imports atexit",
            "defines __del__",
            "weakref.finalize",
            "addfinalizer",
            "_register_atexit",
            "installs a signal handler",
            "add_signal_handler",
            "raises SystemExit",
            "os._exit",
            "sys.exit",
            "exit()",
            "ProcessPoolExecutor",
        ):
            assert marker in joined, f"guard missed {marker!r} in {rel}:\n{joined}"


def test_the_guard_ignores___main___entry_point_blocks() -> None:
    """``if __name__ == "__main__":`` code is a script entry point, not daemon
    surface: the daemon imports the module, so hooks there never run either."""
    script_only = """
import sys


def cleanup():
    pass


if __name__ == "__main__":
    import atexit

    atexit.register(cleanup)
    sys.exit(0)
"""
    assert _scan_source("shared/sentry.py", script_only) == []


def test_the_coordinator_is_the_one_file_allowed_to_install_handlers() -> None:
    """``shutdown.py`` owns ``signal.signal``/``os._exit``; it must stay clean."""
    coordinator = (LIVE_RUNNER / "shutdown.py").read_text(encoding="utf-8")
    assert _scan_source("live_runner/shutdown.py", coordinator) == []
    daemon = (LIVE_RUNNER / "daemon.py").read_text(encoding="utf-8")
    assert _scan_source("live_runner/daemon.py", daemon) == []
