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
    _FORBIDDEN_CALLS = {"eval", "exec"}
    _FORBIDDEN_ATTR_CALLS = {("os", "system")}
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

        self._validate_subprocess_import(code, strategy_name, errors, warnings)

        score = self._score(errors, warnings)
        return ValidationResult(
            passed=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            score=score,
        )

    def _validate_security(self, tree: ast.AST, errors: list[str]) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in self._FORBIDDEN_CALLS:
                    errors.append(f"Forbidden call detected: {node.func.id}()")
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in self._FORBIDDEN_ATTR_CALLS
                ):
                    errors.append(
                        f"Forbidden call detected: {node.func.value.id}.{node.func.attr}()"
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
