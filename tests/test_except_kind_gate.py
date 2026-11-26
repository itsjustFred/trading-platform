"""Every broad except handler in the gated packages must be classified.

A bare ``except``, ``except Exception`` or ``except BaseException`` must
carry a trailing ``# except-kind: <kind> - <justification>`` marker so a
reviewer can see why swallowing (or re-raising) is allowed there:

- ``loop-survival``: the failure is recorded and the enclosing loop/batch
  continues (one bad cell must not abort the grid).
- ``money-decision``: live_runner money path; unknown state must not
  produce a success-shaped continuation.
- ``best-effort``: an optional enrichment/persistence whose failure
  degrades to a documented default.
- ``propagate``: the handler classifies or enriches the error and always
  re-raises; nothing is swallowed.

The gate covers ``live_runner/``, ``runner/`` (including ``runner/engines/``)
and ``risk/``. See docs/live-exception-classification.md.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATED_ROOTS = ("live_runner", "runner", "risk")
VALID_KINDS = {"loop-survival", "money-decision", "best-effort", "propagate"}
MARKER_RE = re.compile(r"#\s*except-kind:\s*([A-Za-z-]+)")


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    # `except (Exception, X)` and `except builtins.Exception` are broad too.
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in candidates:
        if isinstance(node, ast.Name) and node.id in {"Exception", "BaseException"}:
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"Exception", "BaseException"}:
            return True
    return False


def _broad_handlers(path: Path) -> list[ast.ExceptHandler]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_broad_except(node)
    ]


def _violations(root: Path) -> tuple[list[str], list[str], list[str]]:
    """(missing markers, invalid kinds, stray markers) under ``root``."""
    missing: list[str] = []
    invalid: list[str] = []
    stray: list[str] = []

    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        handlers = _broad_handlers(path)
        handler_lines = {node.lineno for node in handlers}

        for node in handlers:
            line = lines[node.lineno - 1]
            match = MARKER_RE.search(line)
            rel = f"{path.relative_to(root)}:{node.lineno}"
            if match is None:
                missing.append(rel)
                continue
            if match.group(1) not in VALID_KINDS:
                invalid.append(f"{rel} ({match.group(1)})")

        for lineno, line in enumerate(lines, start=1):
            if "except-kind:" in line and lineno not in handler_lines:
                stray.append(f"{path.relative_to(root)}:{lineno}")

    return missing, invalid, stray


def test_every_broad_except_is_classified() -> None:
    missing: list[str] = []
    invalid: list[str] = []
    stray: list[str] = []

    for name in GATED_ROOTS:
        root = REPO_ROOT / name
        assert root.is_dir(), f"gated root {name} is missing"
        m, i, s = _violations(root)
        missing.extend(f"{name}/{rel}" for rel in m)
        invalid.extend(f"{name}/{rel}" for rel in i)
        stray.extend(f"{name}/{rel}" for rel in s)

    assert not missing, "broad except handlers missing # except-kind markers:\n" + "\n".join(
        missing
    )
    assert not invalid, "broad except handlers with invalid # except-kind markers:\n" + "\n".join(
        invalid
    )
    assert not stray, "# except-kind markers outside broad except handlers:\n" + "\n".join(
        stray
    )


def test_gate_flags_a_swallowed_exception_fixture(tmp_path: Path) -> None:
    """A fixture file with an unmarked broad except must fail the gate."""
    pkg = tmp_path / "runner"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    missing, _invalid, _stray = _violations(tmp_path)
    assert missing == ["runner/bad.py:4"]


def test_gate_accepts_a_classified_fixture(tmp_path: Path) -> None:
    pkg = tmp_path / "risk"
    pkg.mkdir()
    (pkg / "ok.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:  # except-kind: propagate - classify, then re-raise\n"
        "        raise\n"
        "    try:\n"
        "        other()\n"
        "    except ValueError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    missing, invalid, stray = _violations(tmp_path)
    assert (missing, invalid, stray) == ([], [], [])


def test_gate_flags_invalid_kind_and_stray_marker(tmp_path: Path) -> None:
    pkg = tmp_path / "runner"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:  # except-kind: vibes\n"
        "        pass\n"
        "    # except-kind: loop-survival\n"
        "    x = 1\n",
        encoding="utf-8",
    )
    missing, invalid, stray = _violations(tmp_path)
    assert missing == []
    assert invalid == ["runner/bad.py:4 (vibes)"]
    assert stray == ["runner/bad.py:6"]
