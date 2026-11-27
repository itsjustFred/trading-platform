"""Every subprocess a test starts must get the sandboxed environment.

``tests/conftest.py`` guards only the pytest process: the audit hook, the
``load_dotenv`` redirect and the patched ``*ACCOUNTS_ENV*`` constants all live in
this process's memory. A child a test spawns inherits the environment but none of
that, so a plain ``subprocess.run(...)`` hands the child the caller's whole
environment - including anything the sandbox had not (yet) cleared - and the
child loads the checkout's real ``.env`` and resolves the real ``.accounts.env``
path when it imports the platform modules.

``sandboxed_env()`` is the one helper that gives a child the same sandbox the
suite runs in. This module is the static half of that contract: it fails when a
test file starts a process without it. The behavioural half (a child really does
resolve the accounts env into the session sandbox and refuses the network) is
pinned in ``tests/test_suite_isolation.py``.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = PROJECT_ROOT / "tests"

#: ``module.function`` pairs that start a process. ``os.fork``/``os.exec*``/
#: ``os.posix_spawn`` are listed too: they inherit the environment exactly like
#: ``subprocess`` and are the escapes a ``subprocess``-only scan would miss.
_SPAWN_CALLS = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("subprocess", "getoutput"),
        ("subprocess", "getstatusoutput"),
        ("os", "system"),
        ("os", "popen"),
        ("os", "spawnl"),
        ("os", "spawnle"),
        ("os", "spawnlp"),
        ("os", "spawnlpe"),
        ("os", "spawnv"),
        ("os", "spawnve"),
        ("os", "spawnvp"),
        ("os", "spawnvpe"),
        ("os", "execl"),
        ("os", "execle"),
        ("os", "execlp"),
        ("os", "execlpe"),
        ("os", "execv"),
        ("os", "execve"),
        ("os", "execvp"),
        ("os", "execvpe"),
        ("os", "posix_spawn"),
        ("os", "posix_spawnp"),
        # A pool is a set of processes, so it inherits the environment like one.
        ("multiprocessing", "Pool"),
        ("pty", "fork"),
    }
)

#: ``asyncio.create_subprocess_exec`` / ``..._shell``: reached as attributes, so
#: the dotted tail is matched instead of a fixed pair.
_ASYNCIO_SPAWN_TAILS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})

#: Process launchers whose call shape is not a dotted module name: a
#: ``multiprocessing.get_context("spawn").Process(...)`` reaches the machinery as a
#: bare attribute on a call result, and ``concurrent.futures``'s executor can be
#: spelled with or without its module. Matching the trailing name catches those
#: shapes - and the pairs below are matched on both names, so ``os.fork`` and
#: ``multiprocessing.Process(...)`` are covered by the pair list.
#: ``Parallel`` is joblib's worker launcher, matched the same way for the same
#: reason. Client-dispatched frameworks (a dask or ray client) have no single call
#: shape to name - they submit through a connection object - so a test reaching for
#: one has to say so through the documented marker, which is the escape hatch for
#: every launcher the list cannot express.
_LAUNCHER_ATTR_NAMES = frozenset(
    {"Process", "ProcessPoolExecutor", "Parallel", "fork", "forkpty"}
)

#: The marker that documents a deliberate exception on the spawn call, and the shape it must
#: take: ``# sandbox-env:`` followed by the reason. A bare marker would silence the scan with
#: nothing to review, which is the one way the contract could be talked out of.
_ENV_EXCEPTION_MARKER = "sandbox-env:"


def _test_files() -> list[Path]:
    """Every Python file under the suite, parseable ones included.

    Nothing is excluded by directory: the fixtures tree holds Pine Script and deliberately
    unparseable inputs (excluded from the repo's lint for that reason), and those are
    skipped per file by the parse below - but a Python test file under ``fixtures/`` that
    started a process would be exactly the escape this contract exists to close.
    """
    return sorted(path for path in TESTS_ROOT.rglob("*.py") if path.is_file())


def _dotted_call_name(node: ast.Call) -> str:
    """``a.b.c(...)`` as ``a.b.c``, or the trailing call on any other expression."""
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    return ".".join(parts)


def _dotted_target_of(expression: ast.AST, tree: ast.Module) -> str | None:
    """The dotted name *expression* stands for, or ``None`` when it stands for nothing known.

    ``run = subprocess.run`` binds a name to the function; ``from subprocess import run as r``
    binds it to the import. Both make the call's target a bare name, and a bare name matches
    no launcher list - so the name is resolved here instead of being waved through.
    """
    for candidate in (expression, _resolve_name_target(expression, tree)):
        if candidate is None:
            continue
        if isinstance(candidate, ast.Attribute):
            parts: list[str] = []
            current: ast.AST = candidate
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                parts.reverse()
                return ".".join(parts)
        if isinstance(candidate, ast.ImportFrom) and candidate.module:
            for alias in candidate.names:
                if alias.asname and alias.asname == getattr(expression, "id", None):
                    return f"{candidate.module}.{alias.name}"
        if isinstance(candidate, ast.Import):
            for alias in candidate.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == getattr(expression, "id", None):
                    return alias.name
    return None


def _resolve_name_target(expression: ast.AST, tree: ast.Module) -> ast.AST | None:
    """The value a bare call target is bound to, in the scope that reads it."""
    if isinstance(expression, ast.Name):
        return _resolve_name(expression.id, expression, tree)
    return None


def _starts_a_process(node: ast.Call, tree: ast.Module | None = None) -> bool:
    """Whether one call starts a process, by anything the scan can name.

    Fail closed where a name can say so: a launcher whose final call name contains
    ``fork`` or ``spawn`` is a launcher the list has never heard of, and the cost of
    being wrong about it is a scan entry a test author explains away with the marker -
    lower than the cost of a child inheriting an unswept environment. The exact lists
    above keep the shapes that carry no such substring.
    """
    dotted = _dotted_call_name(node)
    parts = dotted.split(".")
    if len(parts) >= 2 and (parts[-2], parts[-1]) in _SPAWN_CALLS:
        return True
    if parts[-1] in _ASYNCIO_SPAWN_TAILS:
        return True
    name = parts[-1].lower()
    if "fork" in name or "spawn" in name:
        return True
    if parts[-1] in _LAUNCHER_ATTR_NAMES:
        return True
    # A bare name is not a launcher on its own - unless the name reads as an alias of one.
    aliased = _dotted_target_of(node.func, tree) if len(parts) == 1 else None
    if aliased is None:
        return False
    alias_parts = aliased.split(".")
    if len(alias_parts) >= 2 and (alias_parts[-2], alias_parts[-1]) in _SPAWN_CALLS:
        return True
    return alias_parts[-1] in _ASYNCIO_SPAWN_TAILS or alias_parts[-1] in _LAUNCHER_ATTR_NAMES


@lru_cache(maxsize=None)
def _parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """Child node -> its parent, for one file's AST."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _scope_of(node: ast.AST, tree: ast.Module) -> ast.AST:
    """The innermost function, class or module whose body *node* is part of."""
    scope = tree
    current = node
    while True:
        parent = _parent_map(tree).get(id(current))
        if parent is None:
            return scope
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = parent
        current = parent


@lru_cache(maxsize=None)
def _names_defined_in_scope(scope: ast.AST) -> dict[str, ast.AST]:
    """Every name *scope* binds, mapped to the value it binds.

    A local ``_child_env`` helper, or an ``env`` built inside one function, is how
    most call sites name the child's environment, so the scan has to see a name's
    definition to decide whether an ``env=_child_env(...)`` is the sandboxed one or
    a hand-rolled copy. The whole scope is walked, loops and ``if`` arms included,
    but nested functions and classes are not descended into: they are scopes of
    their own, and the same name is bound differently in different functions - the
    binding a call site resolves against is the one in its own scope (or an
    enclosing one, outward).
    """
    definitions: dict[str, ast.AST] = {}
    body = [scope]
    while body:
        node = body.pop()
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if isinstance(alias.name, str):
                    definitions.setdefault(alias.asname or alias.name, node)
        elif isinstance(node, ast.Import):
            # ``import tests.conftest as tc`` binds tc; the dotted call through it is what the
            # scan follows (``tc.sandboxed_env()``), and the binding is recorded so a name the
            # import produced is resolved rather than treated as unknown.
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                definitions.setdefault(bound, node)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions.setdefault(target.id, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                definitions.setdefault(node.target.id, node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not scope:
                definitions.setdefault(node.name, node)
                continue  # a nested scope: its own bindings stay its own
        body.extend(ast.iter_child_nodes(node))
    return definitions


@lru_cache(maxsize=None)
def _repo_module_ast(module_name: str) -> ast.Module | None:
    """The parsed AST of a module inside this repo, or ``None`` when it is not one.

    Only repo-internal modules are followed: a name imported from a third-party package has
    no definition here to read, and the scan must not pretend to know what it does.
    """
    relative = Path(*module_name.split("."))
    for candidate in (relative.with_suffix(".py"), relative / "__init__.py"):
        path = PROJECT_ROOT / candidate
        if path.is_file():
            try:
                return ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                return None
    return None


def _imported_definition(
    module_name: str, original_name: str, seen: set[tuple[str, str]]
) -> ast.AST | None:
    """What *original_name* is bound to in a repo module this file imported it from."""
    key = (module_name, original_name)
    if key in seen:
        return None
    seen.add(key)
    module_ast = _repo_module_ast(module_name)
    if module_ast is None:
        return None
    return _names_defined_in_scope(module_ast).get(original_name)


def _resolve_name(name: str, node: ast.AST, tree: ast.Module) -> ast.AST | None:
    """The value *name* binds where *node* reads it, or ``None`` when unbound.

    The lookup walks outward from the scope the name is read in: a function-local
    ``env`` wins over the module-level one, and an unbound name answers ``None``
    rather than borrowing an unrelated binding from elsewhere in the file.
    """
    scope: ast.AST = _scope_of(node, tree)
    while True:
        definitions = _names_defined_in_scope(scope)
        if name in definitions:
            return definitions[name]
        if scope is tree:
            return None
        scope = _scope_of(scope, tree)


def _expr_uses_sandboxed_env(expression: ast.AST, tree: ast.Module) -> bool:
    """Whether *expression* is ``sandboxed_env(...)`` or a name that resolves to it.

    Follows same-file variables and helper functions transitively: a ``_child_env``
    that calls ``sandboxed_env`` (directly or through another local helper) is the
    contract, while ``dict(os.environ)`` is not. Each name is resolved in the scope
    that reads it, so two functions binding the same name differently do not
    confuse the scan.
    """
    seen: set[tuple[str, int, int]] = set()
    seen_imports: set[tuple[str, str]] = set()
    pending: list[tuple[ast.AST, ast.AST]] = [(expression, tree)]
    while pending:
        node, scope = pending.pop()
        if isinstance(node, ast.Call):
            if _dotted_call_name(node).endswith("sandboxed_env"):
                return True
            pending.append((node.func, scope))
            continue
        if isinstance(node, ast.Name):
            key = (node.id, node.lineno, node.col_offset)
            if key in seen:
                continue
            seen.add(key)
            definition = _resolve_name(node.id, node, tree)
            if definition is None:
                continue
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The helper's whole body: it may reach the sandboxed environment
                # through another local name, which this loop then follows.
                pending.extend((child, definition) for child in ast.walk(definition))
                continue
            if isinstance(definition, ast.ImportFrom) and definition.module:
                # A helper imported from a repo module: the definition lives there, so the
                # same walk runs on its body.
                alias = next(
                    (
                        alias
                        for alias in definition.names
                        if (alias.asname or alias.name) == node.id
                    ),
                    None,
                )
                if alias is not None:
                    target = _imported_definition(
                        definition.module, alias.name, seen_imports
                    )
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # The imported helper's body, walked the same way a local one is.
                        pending.extend((child, definition) for child in ast.walk(target))
                    elif target is not None:
                        pending.append((target, definition))
                continue
            pending.append((definition, _scope_of(definition, tree)))
            continue
    return False


def _env_keyword(node: ast.Call) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == "env":
            return keyword.value
    return None


def _spawn_call_is_sandboxed(node: ast.Call, tree: ast.Module, source_lines: list[str]) -> bool:
    """Whether one spawn call hands its child the sandboxed environment.

    Three shapes are the contract: ``env=sandboxed_env(...)``, an ``env=`` bound to
    a same-file name that resolves to it, and a call carrying the ``# sandbox-env:``
    marker that documents why this call is the exception. The marker is checked
    first and for every launcher, because some shapes take no ``env=`` at all -
    ``os.fork`` inherits the parent's environment and cannot be told otherwise.
    """
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    for line in source_lines[start:end]:
        marker = line.find(_ENV_EXCEPTION_MARKER)
        if marker == -1:
            continue
        if not line[marker + len(_ENV_EXCEPTION_MARKER) :].strip():
            return False  # a bare marker documents nothing; it is not an exception
        return True
    env_expression = _env_keyword(node)
    if env_expression is None:
        # The call names no environment, so the contract has to be inside what it reaches: a
        # same-file helper that builds the child's environment itself (``spawn_holder`` is
        # the shape in this suite) is compliant, and anything else - an imported helper, a
        # bare fork - is reported rather than waved through.
        return _expr_uses_sandboxed_env(node.func, tree)
    return _expr_uses_sandboxed_env(env_expression, tree)


def _display_path(path: Path) -> str:
    """The path as it reads in a failure message, relative to the checkout when it is in it."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _unsandboxed_spawn_calls(path: Path) -> list[str]:
    """Every spawn call in *path* that does not hand its child the sandboxed env."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - unreadable file is reported by the scan below
        return [f"{path}: unreadable"]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not Python - a Pine Script fixture, a deliberately unparseable input. A syntax
        # error in a real test file fails at collection, before the scan's verdict matters.
        return []
    source_lines = source.splitlines()
    return [
        f"{_display_path(path)}:{node.lineno}: {_dotted_call_name(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _starts_a_process(node, tree)
        and not _spawn_call_is_sandboxed(node, tree, source_lines)
    ]


def test_every_test_subprocess_uses_the_sandboxed_env() -> None:
    """Static half of the child-sandbox contract: a spawn without the helper fails."""
    offenders: list[str] = []
    for path in _test_files():
        offenders.extend(_unsandboxed_spawn_calls(path))
    assert offenders == [], (
        "these spawn calls hand a child an environment that is not sandboxed_env() "
        "(or a documented `# sandbox-env:` exception):\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reports_a_plain_subprocess_call(tmp_path: Path) -> None:
    """The positive control: the scan must be able to fail, or it guards nothing."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import subprocess\n"
        "def test_spawns():\n"
        "    subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    (call,) = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    source_lines = sample.read_text(encoding="utf-8").splitlines()
    assert _spawn_call_is_sandboxed(call, tree, source_lines) is False
    assert len(_unsandboxed_spawn_calls(sample)) == 1


def test_the_scan_accepts_sandboxed_env_and_its_helpers(tmp_path: Path) -> None:
    """``sandboxed_env(...)``, a local helper that calls it, and a marked exception."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import os\n"
        "import subprocess\n"
        "from tests.conftest import sandboxed_env\n"
        "def _child_env(root):\n"
        "    return sandboxed_env(PYTHONPATH=str(root))\n"
        "def test_direct():\n"
        "    subprocess.run(['git', 'status'], env=sandboxed_env())\n"
        "def test_helper():\n"
        "    subprocess.run(['git', 'status'], env=_child_env(os.getcwd()))\n"
        "def test_marked():\n"
        "    subprocess.run(['git', 'status'], env={'PATH': os.environ['PATH']}) "
        "# sandbox-env: PATH-only probe\n",
        encoding="utf-8",
    )
    assert _unsandboxed_spawn_calls(sample) == []


def test_the_scan_covers_the_other_process_launchers(tmp_path: Path) -> None:
    """``multiprocessing``, a pool executor and ``os.fork`` inherit the environment too.

    A spawn call through any of them hands the child the caller's environment exactly like
    ``subprocess`` does, so the scan has to name them - including the shapes whose target is an
    attribute on a call result, such as ``get_context("spawn").Process(...)``.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import multiprocessing\n"
        "import os\n"
        "import subprocess\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "def test_forks():\n"
        "    multiprocessing.Process(target=print)\n"
        "    multiprocessing.get_context('spawn').Process(target=print)\n"
        "    multiprocessing.Pool(2)\n"
        "    ProcessPoolExecutor(2)\n"
        "    joblib.Parallel(n_jobs=2)\n"
        "    os.fork()\n"
        "    pty.fork()\n"
        "def test_pty_fork():\n"
        "    pty.fork()\n"
        "def test_ok():\n"
        "    subprocess.run(['git', 'status'], env=sandboxed_env())\n",
        encoding="utf-8",
    )
    offenders = _unsandboxed_spawn_calls(sample)
    assert len(offenders) == 8, offenders
    assert all("test_sample.py:" in offender for offender in offenders)


def test_the_scan_accepts_the_other_launchers_with_the_sandboxed_env(tmp_path: Path) -> None:
    """`env=sandboxed_env()` for a launcher that takes one, a marker for one that does not.

    ``multiprocessing.Process``/``Pool``, ``concurrent.futures``' executor and ``joblib.Parallel``
    accept no ``env=`` at all: handing them one is a ``TypeError``, not a sandboxed child. They
    inherit the calling process's environment, which inside the suite is the swept one, so the
    documented marker is what they carry; a launcher that does take an environment is handed
    ``sandboxed_env()``. Both shapes are accepted by the scan.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import multiprocessing\n"
        "import os\n"
        "import subprocess\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "import joblib\n"
        "from tests.conftest import sandboxed_env\n"
        "def test_launchers():\n"
        "    subprocess.run(['git', 'status'], env=sandboxed_env())\n"
        "    subprocess.Popen(['git', 'status'], env=sandboxed_env())\n"
        "    multiprocessing.Process(target=print)  # sandbox-env: no env parameter\n"
        "    multiprocessing.Pool(2)  # sandbox-env: no env parameter; inherits the swept one\n"
        "    ProcessPoolExecutor(2)  # sandbox-env: no env parameter; inherits the swept one\n"
        "    joblib.Parallel(n_jobs=2)  # sandbox-env: no env parameter; inherits the swept one\n"
        "    os.fork()  # sandbox-env: fork has no env=; the child inherits the swept one\n",
        encoding="utf-8",
    )
    assert _unsandboxed_spawn_calls(sample) == []


def test_the_scan_follows_a_helper_imported_from_the_repo(tmp_path: Path) -> None:
    """A repo helper that builds the sandbox with it is the contract, called or imported.

    ``spawn_holder`` lives in ``tests/helpers`` and carries ``sandboxed_env()`` inside it, so
    a test that calls it hands its child the sandbox even though the call names no ``env=``.
    The scan reads the imported module for that.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "from tests.helpers.lock_holder import spawn_holder\n"
        "def test_spawns():\n"
        "    spawn_holder('/tmp/lock', 1.0)\n",
        encoding="utf-8",
    )
    assert _unsandboxed_spawn_calls(sample) == []


def test_the_scan_follows_a_helper_reached_through_a_module_import(tmp_path: Path) -> None:
    """``import tests.conftest as tc`` binds a name the scan resolves too.

    The environment then reads ``tc.sandboxed_env()``: the dotted call carries the callee, so
    the contract is followed whether the binding arrived through ``from x import y`` or
    ``import x as y``.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import os\n"
        "import subprocess\n"
        "import tests.conftest as tc\n"
        "def test_spawns():\n"
        "    subprocess.run(['git', 'status'], env=tc.sandboxed_env())\n"
        "def test_hand_rolled():\n"
        "    subprocess.run(['git', 'status'], env={**os.environ, 'X': '1'})\n",
        encoding="utf-8",
    )
    (offender,) = _unsandboxed_spawn_calls(sample)
    assert offender.endswith(":7: subprocess.run")


def test_the_scan_fails_closed_on_a_launcher_it_does_not_know(tmp_path: Path) -> None:
    """An unknown launcher is reported rather than waved through.

    A call whose name says it starts a process - ``fork``, ``spawn`` - and that cannot be
    resolved to the sandboxed environment is an offender: the cost of the entry is a marker
    comment, the cost of the miss is a child inheriting an unswept environment.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "from some_client_library import spawn_worker_process\n"
        "def test_starts_a_worker():\n"
        "    spawn_worker_process()\n"
        "def test_forks_through_a_helper():\n"
        "    fork_and_join()\n",
        encoding="utf-8",
    )
    offenders = _unsandboxed_spawn_calls(sample)
    assert len(offenders) == 2, offenders
    assert all("test_sample.py:" in offender for offender in offenders)


def test_the_scan_follows_a_spawn_call_through_an_alias(tmp_path: Path) -> None:
    """``run = subprocess.run`` aliases a launcher, and the call target is the bare name.

    The alias hides the launcher from the dotted-name match, so the scan resolves the name
    through its binding before deciding: an alias of a launcher is a launcher, and it owes
    the child the sandboxed environment like any other.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import os\n"
        "import subprocess\n"
        "from subprocess import run as r\n"
        "from tests.conftest import sandboxed_env\n"
        "def test_aliased_ok():\n"
        "    run = subprocess.run\n"
        "    run(['git', 'status'], env=sandboxed_env())\n"
        "def test_aliased_hand_rolled():\n"
        "    other = subprocess.run\n"
        "    other(['git', 'status'], env={**os.environ, 'X': '1'})\n"
        "def test_import_alias_ok():\n"
        "    r(['git', 'status'], env=sandboxed_env())\n"
        "def test_import_alias_flagged():\n"
        "    r(['git', 'status'])\n",
        encoding="utf-8",
    )
    offenders = _unsandboxed_spawn_calls(sample)
    assert [line for line in offenders if ":" in line] == [
        f"{_display_path(sample)}:10: other",
        f"{_display_path(sample)}:14: r",
    ]


def test_a_bare_marker_is_not_an_exception(tmp_path: Path) -> None:
    """``# sandbox-env:`` with no reason documents nothing, so it silences nothing.

    The marker is the one way this contract can be talked out of, so it has to carry the
    reason a reviewer reads: an empty one is treated as no marker at all.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import subprocess\n"
        "def test_spawns():\n"
        "    subprocess.run(['git', 'status'], env={'PATH': '/usr/bin'})  # sandbox-env:\n",
        encoding="utf-8",
    )
    (offender,) = _unsandboxed_spawn_calls(sample)
    assert offender.endswith(":3: subprocess.run")


def test_the_scan_rejects_a_hand_rolled_environment(tmp_path: Path) -> None:

    """``dict(os.environ)`` is the shape the contract exists to remove."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import os\n"
        "import subprocess\n"
        "def test_spawns():\n"
        "    subprocess.run(['git', 'status'], env={**os.environ, 'PROBE': '1'})\n",
        encoding="utf-8",
    )
    (offender,) = _unsandboxed_spawn_calls(sample)
    assert offender.endswith(":4: subprocess.run")
