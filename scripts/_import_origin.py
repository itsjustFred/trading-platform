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
doubt. Each script puts ``scripts/`` at the *front* of ``sys.path`` and imports ``_import_origin``
from there, so the copy next to the script is the one that runs - an installed module of the same
name cannot answer first.

The guard does **not** put the repo root on the path. Doing so would repair the divergence and then
report success, which is the very behaviour it exists to refuse - the original inline block did not
do it either, and this rewrite must not start.
"""

from __future__ import annotations

import sys
from pathlib import Path


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


if __name__ == "__main__":
    ensure_own_checkout("_import_origin")
    _expected, _actual = resolve_shared()
    print(f"expected shared/ : {_expected}")
    print(f"actually imported: {_actual}")
