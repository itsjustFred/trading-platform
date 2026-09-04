"""Static AST-based validation for AI-generated strategy code.

This validator deliberately never executes the submitted code: earlier
versions wrote the code to a temp file and ran ``exec_module`` in a
subprocess to check the interface, which ran module-level statements
with full user privileges (``python -I`` isolates sys.path/env, not the
OS). Every check below works on the parse tree only.

This module is a POLICY GATE, not a sandbox. The denylists below inspect
the *submitted* code; they say nothing about what this file itself may do.
Executing generated strategy code is the engine adapter's job, inside its
own worker isolation — if you are about to add an ``importlib`` /
``exec_module`` call here to "just read the interface", that is the exact
bug this module was rewritten to remove. The sentinel test
``test_validator_never_executes_submitted_code`` guards this contract.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float = 0.0


class AICodeValidator:
    _FORBIDDEN_CALLS = {"eval", "exec", "getattr", "__import__"}
    _FORBIDDEN_ATTR_CALLS = {("os", "system"), ("os", "popen")}
    # Calling through one of these hands back the live namespace and lets a
    # payload reach any builtin by computed name (vars()['eval'](...)).
    _REFLECTIVE_NAMESPACES = {"vars", "globals", "locals"}
    # Imports that have no business inside a strategy: any of these is a
    # hard error regardless of how it is called afterwards.
    _FORBIDDEN_IMPORTS = {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "multiprocessing",
        "pathlib",
        "importlib",
        "sys",
        "builtins",
        "tempfile",
        "pickle",
        "requests",
        "urllib",
        "http",
    }
    _FORBIDDEN_DUNDER_ATTRS = {
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__import__",
        "__dict__",
        "__class__",
        "__bases__",
        "__code__",
        "__closure__",
    }
    _REQUIRED_METHODS = {"generate_signals", "get_name", "get_parameters"}

    def validate(self, code: str, strategy_name: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        tree: ast.AST | None = None

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            errors.append(f"Syntax error: {exc.msg} (line {exc.lineno})")

        if tree is not None:
            self._validate_security(tree, errors)
            self._validate_interface(tree, errors, warnings)

        score = self._score(errors, warnings)
        return ValidationResult(
            passed=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            score=score,
        )

    def _validate_security(self, tree: ast.AST, errors: list[str]) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._validate_import(node, errors)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in self._FORBIDDEN_CALLS:
                    errors.append(f"Forbidden call detected: {node.func.id}()")
                if self._is_reflective_call(node.func):
                    errors.append(
                        "Forbidden reflective call: strategy code must not call "
                        "through vars()/globals()/locals() namespaces"
                    )
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in self._FORBIDDEN_ATTR_CALLS
                ):
                    errors.append(
                        f"Forbidden call detected: {node.func.value.id}.{node.func.attr}()"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in self._FORBIDDEN_DUNDER_ATTRS:
                    errors.append(f"Forbidden attribute access: ...{node.attr}")
            elif isinstance(node, ast.Subscript):
                # __builtins__['__import__'] style indirection: the dunder
                # name surfaces as the subscript key constant.
                key = node.slice
                key_value = getattr(key, "value", None)
                if isinstance(key_value, str) and key_value in self._FORBIDDEN_DUNDER_ATTRS:
                    errors.append(f"Forbidden dunder reference: {key_value!r}")
                base = node.value
                if isinstance(base, ast.Name) and base.id in self._FORBIDDEN_DUNDER_ATTRS:
                    errors.append(f"Forbidden dunder subscript: {base.id}[...]")
                # Subscripting a reflective namespace (vars()['eval'],
                # globals()[name]) resolves callables by computed name and
                # bypasses every name-based check — rejected as a policy
                # regardless of whether the key is a literal or an expression.
                if self._is_reflective_call(base):
                    errors.append(
                        "Forbidden reflective namespace lookup: "
                        "vars()/globals()/locals()[...] must not be used"
                    )

    def _is_reflective_call(self, node: ast.AST) -> bool:
        """True when the expression calls vars()/globals()/locals().

        These hand back the live module/frame namespace, so a call or lookup
        through them (``vars()['eval'](...)``) is a denylist bypass by
        construction. Rejected as a policy, the same way the dunder
        indirections above are.
        """
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in self._REFLECTIVE_NAMESPACES
        )

    def _validate_import(self, node: ast.Import | ast.ImportFrom, errors: list[str]) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in self._FORBIDDEN_IMPORTS:
                    errors.append(f"Forbidden import detected: {alias.name}")
        else:
            root = (node.module or "").split(".")[0]
            if root in self._FORBIDDEN_IMPORTS:
                errors.append(f"Forbidden import detected: from {node.module} import ...")

    def _validate_interface(self, tree: ast.AST, errors: list[str], warnings: list[str]) -> None:
        module_functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        class_methods: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_methods.update(
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )

        implemented = module_functions | class_methods
        missing = sorted(self._REQUIRED_METHODS - implemented)
        if missing:
            errors.append(f"Missing required methods: {', '.join(missing)}")

        if class_methods and module_functions:
            warnings.append("Mixed interface style detected (module functions + class methods).")

    def _score(self, errors: list[str], warnings: list[str]) -> float:
        raw_score = 1.0 - (0.25 * len(errors)) - (0.05 * len(warnings))
        return max(0.0, min(1.0, round(raw_score, 3)))
