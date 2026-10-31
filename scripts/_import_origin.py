"""The import-origin guard, extracted so ten scripts stop carrying a verbatim copy of it.

Running a script puts `scripts/` on `sys.path[0]`, **not the repo root**, so `import shared` does
not find the checkout the file lives in: it falls through to the installed package, which points at
a *different* tree. Measured on 2026-10-04: the two were genuinely different revisions, and the
installed one carried a *shorter migration set*, so a script run this way applied an older schema
than the code believes and then reported success.

Refused rather than silently corrected: quietly importing a different tree than the one being edited
*is* the hazard, not the fix. The correct invocation is ``python -m scripts.<name>`` from the repo
root. Pinned by ``tests/test_script_import_origin.py``.

Why a module and not a pasted block
-----------------------------------
Ten scripts carried the same ~30 lines verbatim. That is ten copies of one behaviour, ten places to
fix it, and - from the SonarCloud analysis of PR #116 - 7.8% new-code duplication against a 3%
threshold. The behaviour is unchanged; only its address moved.

How each script reaches this module
-----------------------------------
The script's bootstrap cannot assume the repo root is importable, because that is exactly what is in
doubt. Each script puts ``scripts/`` on the path itself (``sys.path.insert(0, str(Path(__file__)
.parent))``) and imports ``_import_origin`` from there. If an *installed* copy of that name answered
first, the script notices (``is_own_checkout_file``) and falls back to loading the sibling
``scripts/_import_origin.py`` by path, so the check is run by the file the reader is looking at
rather than by whatever answered first.

The guard does **not** put the repo root on the path. Doing so would repair the divergence and then
report success, which is the very behaviour it exists to refuse - the original inline block did not
do it either, and this rewrite must not start.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def repo_root() -> Path:
    """The checkout this file lives in, from its own location - never a literal.

    ``<root>/scripts/_import_origin.py`` -> ``<root>/scripts`` -> ``<root>``. A literal would keep
    passing after the repo moved, which is the failure the guard exists to prevent one level up:
    ``tests/test_script_import_origin.py::test_the_guard_compares_against_the_scripts_own_checkout``
    holds that derivation as a substring of this module, so it stays spelled out here.
    """
    return Path(__file__).resolve().parent.parent


def resolve_shared() -> tuple[Path, Path | None]:
    """Import `shared`, and return (tree that should answer, tree that did).

    The two differ exactly when something answered `import shared` before this module put the repo
    root on `sys.path` - the installed package - which is the case worth refusing.

    ``actual`` is ``None`` when *nothing* answers ``import shared`` at all - a venv where the
    package was never installed, or installed somewhere the direct invocation cannot see. That is
    the same hazard with the same fix (run from the repo root), so it is reported as a refusal
    rather than
    left to surface as a bare ``ModuleNotFoundError`` traceback the reader has to interpret: the
    message that names the trees is the thing this guard exists to produce.
    """
    try:
        import shared
    except ImportError:
        return Path(__file__).resolve().parent.parent / "shared", None

    expected = Path(__file__).resolve().parent.parent / "shared"
    actual = Path(next(iter(shared.__path__))).resolve()
    return expected, actual


def refuse(script: str, expected: Path, actual: Path | None) -> None:
    """Report the divergence and exit non-zero. Never returns."""
    imported = str(actual) if actual is not None else "<nothing - `shared` is not importable>"
    print(
        "refusing to run: `shared` was imported from the wrong tree.\n"
        f"  this script lives in : {repo_root()}\n"
        f"  expected shared/     : {expected}\n"
        f"  actually imported    : {imported}\n"
        "\n"
        "Running a script puts scripts/ on sys.path[0], not the repo root, so `import shared`\n"
        "falls through to the installed package - a different revision, with a shorter\n"
        "migration set. To run against the checkout being edited, from the repo root:\n"
        f"    python -m scripts.{script}\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


def ensure_own_checkout(script: str) -> None:
    """The guard: refuse when `shared` did not come from this checkout.

    Deliberately does **not** touch `sys.path`. Repairing the path here would make the check pass by
    construction and let a directly-invoked script mutate the installed tree's schema while
    reporting success - the exact incident this exists to prevent. The caller runs from the repo
    root (``python -m scripts.<name>``) or is refused.
    """
    expected, actual = resolve_shared()
    if actual != expected:
        refuse(script, expected, actual)


def is_own_checkout_file(module: ModuleType) -> bool:
    """True when an imported `_import_origin` is this file, not an installed copy of it."""
    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    try:
        return Path(origin).resolve() == Path(__file__).resolve()
    except OSError:
        return False


def load_sibling(script_file: str) -> ModuleType:
    """Load ``scripts/_import_origin.py`` next to `script_file`, ignoring `sys.modules`.

    The fallback for a script whose `scripts/` directory is answered by an installed package first.
    """
    path = Path(script_file).resolve().parent / "_import_origin.py"
    spec = importlib.util.spec_from_file_location("_import_origin_sibling", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"refusing to run: cannot load the import-origin guard at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    ensure_own_checkout("_import_origin")
    _expected, _actual = resolve_shared()
    print(f"expected shared/ : {_expected}")
    print(f"actually imported: {_actual}")
