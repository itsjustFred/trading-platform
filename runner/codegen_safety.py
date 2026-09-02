"""Static screening for LLM-generated strategy code (F25).

Generated code is never trusted. Before a generated strategy module is written
to disk (and therefore before anything can import or execute it), it is parsed
and screened for constructs that the runner must never execute on behalf of a
model response: process spawning, dynamic execution, filesystem/database
access, network egress, deserialization of untrusted data, and reflection
escape hatches. Screening is a deny-list over the AST — it is a tripwire, not a
sandbox. The real containment is the human review gate
(``runner.approval_gate``) plus the fact that generated code only ever runs in
a short-lived subprocess with timeouts.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class ScreeningFinding:
    line: int
    message: str


@dataclass(frozen=True)
class ScreeningResult:
    allowed: bool
    findings: tuple[ScreeningFinding, ...]

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(finding.message for finding in self.findings)


_BANNED_MODULES = frozenset(
    {
        "subprocess",
        "multiprocessing",
        "ctypes",
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "paramiko",
        "webbrowser",
        "pickle",
        "cPickle",
        "shelve",
        "dill",
        "marshal",
        "glob",
        "tempfile",
        "web3",
        "ccxt",
        "alpaca",
        "boto3",
        "firebase_admin",
    }
)

_BANNED_ROOT_ATTRIBUTES = frozenset(
    {
        "system",
        "popen",
        "spawn",
        "fork",
        "exec",
        "execv",
        "execve",
        "eval",
        "evalf",
        "remove",
        "unlink",
        "rmtree",
        "makedirs",
        "read_pickle",
        "eval_expr",
    }
)

# Calls that must never appear in generated code. Shared with the helper-code
# screen in runner/engines/pynecore_adapter.py so the two deny lists cannot
# drift apart.
BANNED_CALL_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "open",
        "__import__",
        "input",
    }
)

_BANNED_MODULES_DYNAMIC_LOADERS = frozenset({"importlib", "runpy"})
_BANNED_DYNAMIC_LOAD_ATTRIBUTES = frozenset({"import_module", "run_module", "run_path"})

_BANNED_STDLIB_ROOTS = frozenset(
    {"builtins", "os", "sys", "pathlib", "shutil", "importlib", "runpy", "io", "codecs", "base64"}
)

# Attributes that are dangerous regardless of the receiving object
# (e.g. pandas.read_pickle, base64.b64decode on any aliased import).
_BANNED_ANY_ROOT_ATTRIBUTES = frozenset(
    {
        "read_pickle",
        "b64decode",
        "b32decode",
        "b16decode",
        "b85decode",
        "decodebytes",
        "a2b_base64",
        "a2b_qp",
        "a2b_uu",
    }
)


def screen_strategy_source(source: str) -> ScreeningResult:
    """Parse ``source`` and flag constructs generated code must not use."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ScreeningResult(
            allowed=False,
            findings=(ScreeningFinding(line=exc.lineno or 0, message=f"Syntax error: {exc.msg}"),),
        )

    findings: list[ScreeningFinding] = []

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        message = _screen_node(node)
        if message:
            findings.append(ScreeningFinding(line=line, message=message))

    unique: dict[tuple[int, str], ScreeningFinding] = {}
    for finding in findings:
        unique[(finding.line, finding.message)] = finding
    return ScreeningResult(
        allowed=not unique,
        findings=tuple(sorted(unique.values(), key=lambda item: (item.line, item.message))),
    )


def _screen_node(node: ast.AST) -> str | None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _screen_import(node)

    if isinstance(node, ast.Call):
        return _screen_call(node)

    if isinstance(node, ast.Attribute):
        return _screen_attribute(node)

    if isinstance(node, ast.Name) and node.id == "__builtins__":
        return "refuses references to '__builtins__' (reflection escape hatch)"

    return None


def import_root(node: ast.Import | ast.ImportFrom) -> str:
    """Top-level module name of an import statement (shared screening primitive)."""
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0] if node.names else ""
    return (node.module or "").split(".")[0]


def _screen_import(node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in _BANNED_MODULES_DYNAMIC_LOADERS:
                return f"refuses import of '{alias.name}' (dynamic code loading)"
            if root in _BANNED_MODULES:
                return f"refuses import of '{alias.name}' (banned module)"
        return None

    module_root = (node.module or "").split(".")[0]
    if module_root in _BANNED_MODULES_DYNAMIC_LOADERS:
        return f"refuses import from '{node.module}' (dynamic code loading)"
    if module_root in _BANNED_MODULES:
        return f"refuses import from '{node.module}' (banned module)"
    return None


def _screen_call(node: ast.Call) -> str | None:
    target = node.func

    if isinstance(target, ast.Name) and target.id in BANNED_CALL_NAMES:
        return f"refuses call to '{target.id}()'"

    if isinstance(target, ast.Attribute) and target.attr in _BANNED_DYNAMIC_LOAD_ATTRIBUTES:
        if isinstance(target.value, ast.Name) and target.value.id in _BANNED_MODULES_DYNAMIC_LOADERS:
            return f"refuses call to '{target.value.id}.{target.attr}()' (dynamic code loading)"

    return None


def _screen_attribute(node: ast.Attribute) -> str | None:
    if node.attr in _BANNED_DYNAMIC_LOAD_ATTRIBUTES:
        if isinstance(node.value, ast.Name) and node.value.id in _BANNED_MODULES_DYNAMIC_LOADERS:
            return f"refuses reference to '{node.value.id}.{node.attr}' (dynamic code loading)"

    if node.attr in _BANNED_ANY_ROOT_ATTRIBUTES:
        return f"refuses access to '.{node.attr}' (untrusted deserialization / decoding)"

    if node.attr not in _BANNED_ROOT_ATTRIBUTES:
        return None

    if isinstance(node.value, ast.Name) and node.value.id in _BANNED_STDLIB_ROOTS:
        return f"refuses access to '{node.value.id}.{node.attr}'"

    if isinstance(node.value, ast.Name) and node.value.id == "builtins":
        return f"refuses access to 'builtins.{node.attr}'"

    return None
