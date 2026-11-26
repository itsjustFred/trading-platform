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
    }
)

#: ``asyncio.create_subprocess_exec`` / ``..._shell``: reached as attributes, so
#: the dotted tail is matched instead of a fixed pair.
_ASYNCIO_SPAWN_TAILS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})

#: The marker that documents a deliberate exception on the ``env=`` line.
_ENV_EXCEPTION_MARKER = "sandbox-env:"


#: Deliberately unparseable inputs and Pine Script: not Python, and excluded from
#: the repo's own lint for the same reason (see ``[tool.ruff] extend-exclude``).
_NON_PYTHON_DIRS = frozenset({"fixtures"})


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in TESTS_ROOT.rglob("*.py")
        if not _NON_PYTHON_DIRS.intersection(path.relative_to(TESTS_ROOT).parts)
    )


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


def _is_spawn_call(node: ast.Call) -> bool:
    dotted = _dotted_call_name(node)
    parts = dotted.split(".")
    if len(parts) >= 2 and (parts[-2], parts[-1]) in _SPAWN_CALLS:
        return True
    return parts[-1] in _ASYNCIO_SPAWN_TAILS


def _names_defined_in_file(tree: ast.AST) -> dict[str, ast.AST]:
    """Every name the module binds at top level or inside a function body.

    A local ``_child_env`` helper is how most call sites build the child's
    environment, so the scan has to see a name's definition to decide whether an
    ``env=_child_env(...)`` is the sandboxed one or a hand-rolled copy.
    """
    definitions: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions.setdefault(target.id, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions.setdefault(node.target.id, node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(node.name, node)
    return definitions


def _expr_uses_sandboxed_env(expression: ast.AST, definitions: dict[str, ast.AST]) -> bool:
    """Whether *expression* is ``sandboxed_env(...)`` or a name that resolves to it.

    Follows same-file variables and helper functions transitively: a ``_child_env``
    that calls ``sandboxed_env`` (directly or through another local helper) is the
    contract, while ``dict(os.environ)`` is not.
    """
    seen: set[str] = set()
    pending: list[ast.AST] = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Call):
            if _dotted_call_name(node).endswith("sandboxed_env"):
                return True
            pending.append(node.func)
            continue
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.add(node.id)
            definition = definitions.get(node.id)
            if definition is None:
                continue
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The helper's whole body: it may reach the sandboxed environment
                # through another local name, which this loop then follows.
                pending.extend(ast.walk(definition))
                continue
            pending.append(definition)
    return False


def _env_keyword(node: ast.Call) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == "env":
            return keyword.value
    return None


def _spawn_call_is_sandboxed(
    node: ast.Call, definitions: dict[str, ast.AST], source_lines: list[str]
) -> bool:
    """Whether the ``env=`` of one spawn call is the sandboxed environment.

    Three shapes are the contract: ``env=sandboxed_env(...)``, an ``env=`` bound to
    a same-file name that resolves to it, and an ``env=`` carrying the
    ``# sandbox-env:`` marker that documents why this call is the exception.
    """
    env_expression = _env_keyword(node)
    if env_expression is None:
        return False
    if _expr_uses_sandboxed_env(env_expression, definitions):
        return True
    # The call may span several lines; the marker is looked for on the whole call.
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    for line in source_lines[start:end]:
        if _ENV_EXCEPTION_MARKER in line:
            return True
    return False


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
    except SyntaxError as exc:  # pragma: no cover - a broken test file fails on its own
        return [f"{path}: {exc}"]
    definitions = _names_defined_in_file(tree)
    source_lines = source.splitlines()
    return [
        f"{_display_path(path)}:{node.lineno}: {_dotted_call_name(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_spawn_call(node)
        and not _spawn_call_is_sandboxed(node, definitions, source_lines)
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
    definitions = _names_defined_in_file(tree)
    source_lines = sample.read_text(encoding="utf-8").splitlines()
    assert _spawn_call_is_sandboxed(call, definitions, source_lines) is False
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
