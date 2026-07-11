from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float = 0.0


class AICodeValidator:
    _FORBIDDEN_CALLS = {"eval", "exec", "compile"}
    _FORBIDDEN_ATTR_CALLS = {
        ("os", "system"),
        ("os", "popen"),
        ("subprocess", "Popen"),
        ("subprocess", "run"),
        ("subprocess", "call"),
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

        # Only run the subprocess import check on code that already passed
        # security validation. Executing untrusted code that tripped a
        # forbidden-API rule risks running the very call we just flagged.
        if not errors:
            self._validate_subprocess_import(code, strategy_name, errors, warnings)

        score = self._score(errors, warnings)
        return ValidationResult(
            passed=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            score=score,
        )

    @staticmethod
    def _alias_map(tree: ast.AST) -> dict[str, str | tuple[str, str]]:
        """Map local import names to their real module (and attribute).

        `import os as o`             -> {"o": "os"}
        `from os import system as s` -> {"s": ("os", "system")}
        so both `o.system()` and `s()` resolve to the forbidden `("os", "system")`.
        """
        aliases: dict[str, str | tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    aliases[local] = (node.module or "", alias.name)
        return aliases

    def _validate_security(self, tree: ast.AST, errors: list[str]) -> None:
        aliases = self._alias_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Direct calls: eval(), exec(), compile()
            if isinstance(func, ast.Name) and func.id in self._FORBIDDEN_CALLS:
                errors.append(f"Forbidden call detected: {func.id}()")
                continue
            # Attribute calls resolved through import aliases:
            #   import os as o; o.system(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                resolved = aliases.get(func.value.id, func.value.id)
                mod = resolved[0] if isinstance(resolved, tuple) else resolved
                if (mod, func.attr) in self._FORBIDDEN_ATTR_CALLS:
                    errors.append(f"Forbidden call detected: {mod}.{func.attr}()")
                continue
            # Imported-function calls resolved through `from` aliases:
            #   from os import system as s; s()  ->  os.system()
            if isinstance(func, ast.Name):
                resolved = aliases.get(func.id)
                if (
                    isinstance(resolved, tuple)
                    and (resolved[0], resolved[1]) in self._FORBIDDEN_ATTR_CALLS
                ):
                    errors.append(f"Forbidden call detected: {resolved[0]}.{resolved[1]}()")
                    continue
                if func.id in {"getattr", "__getattribute__"}:
                    if node.args:
                        subj = node.args[0]
                        if isinstance(subj, ast.Name):
                            subj_resolved = aliases.get(subj.id, subj.id)
                            subj_name = (
                                subj_resolved[0]
                                if isinstance(subj_resolved, tuple)
                                else subj_resolved
                            )
                        elif isinstance(subj, ast.Constant) and isinstance(subj.value, str):
                            subj_name = subj.value
                        else:
                            subj_name = None
                        if subj_name in {"os", "subprocess"}:
                            errors.append(
                                f"Forbidden indirect access detected: {subj_name}.<attr>()"
                            )

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

    def _validate_subprocess_import(
        self,
        code: str,
        strategy_name: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        strategy_slug = "".join(
            ch if ch.isalnum() or ch == "_" else "_" for ch in strategy_name
        ).strip("_")
        module_name = strategy_slug or "validated_strategy"

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            module_path = tmp_path / f"{module_name}.py"
            module_path.write_text(code, encoding="utf-8")

            check_script = (
                "import importlib.util, pathlib, sys; "
                "p = pathlib.Path(sys.argv[1]); "
                "spec = importlib.util.spec_from_file_location('validated_module', p); "
                "m = importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(m); "
                "required = ('generate_signals', 'get_name', 'get_parameters'); "
                "missing = [name for name in required if not callable(getattr(m, name, None)) and "
                "not any(callable(getattr(cls, name, None)) "
                "for cls in m.__dict__.values() if isinstance(cls, type))]; "
                "sys.exit(2 if missing else 0)"
            )

            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", check_script, str(module_path)],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                errors.append("Subprocess import timed out.")
                return

            if completed.returncode == 2:
                errors.append("Required callable interface not importable in subprocess.")
            elif completed.returncode != 0:
                stderr_text = (completed.stderr or "").strip()
                errors.append(f"Subprocess import failed: {stderr_text or 'unknown error'}")
            elif completed.stderr.strip():
                warnings.append(f"Subprocess import emitted stderr: {completed.stderr.strip()}")

    def _score(self, errors: list[str], warnings: list[str]) -> float:
        raw_score = 1.0 - (0.25 * len(errors)) - (0.05 * len(warnings))
        return max(0.0, min(1.0, round(raw_score, 3)))
