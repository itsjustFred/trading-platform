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
ALL_KINDS = {"loop-survival", "money-decision", "best-effort", "propagate"}
# The money path only tolerates the two strict kinds; the backtest/risk
# packages may additionally degrade best-effort or enrich-then-reraise.
KINDS_BY_ROOT = {
    "live_runner": {"loop-survival", "money-decision", "propagate"},
    "runner": set(ALL_KINDS),
    "risk": set(ALL_KINDS),
}
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


def _name_is_loaded(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(inner, ast.Name) and inner.id == name and isinstance(inner.ctx, ast.Load)
        for inner in ast.walk(node)
    )


def _straight_line_raise(stmts: list[ast.stmt]) -> bool:
    """A raise that runs unconditionally on this control-flow path.

    ``try``/``with`` bodies are transparent (they do not make the raise
    conditional); ``if``/``for``/``while``/``match``/nested ``def`` do, so
    they are not descended into.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, ast.Try):
            if _straight_line_raise(stmt.body + stmt.orelse + stmt.finalbody):
                return True
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            if _straight_line_raise(stmt.body):
                return True
    return False


def _handler_propagates(handler: ast.ExceptHandler) -> bool:
    """A ``propagate`` marker must actually propagate.

    The body must either raise unconditionally, or hand the caught
    exception object to the caller in a way that surfaces it later:
    appended/added to a collection (``errors.append(exc)`` — the vectorbt
    aggregate), stored into a subscript or attribute
    (``error["value"] = exc`` — the copilot bridge), or returned. Merely
    *referencing* the exception — e.g. passing it to a logging call —
    does not count: that is observation, not propagation. A raise nested
    inside ``if``/``for``/``while``/``match``/``def`` is conditional and
    does not count either; such handlers must be reclassified.
    """
    if _straight_line_raise(handler.body):
        return True
    name = handler.name
    if not name:
        return False
    for node in ast.walk(handler):
        if isinstance(node, ast.Return) and node.value is not None and _name_is_loaded(
            node.value, name
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "add"}
            and any(_name_is_loaded(arg, name) for arg in node.args)
        ):
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(target, (ast.Subscript, ast.Attribute))
            and _name_is_loaded(node.value, name)
            for target in node.targets
        ):
            return True
    return False


def _violations(root: Path, valid_kinds: set[str] | None = None) -> tuple[
    list[str], list[str], list[str]
]:
    """(missing markers, invalid kinds, stray markers) under ``root``.

    ``valid_kinds`` defaults to every kind so fixture trees under an
    arbitrary tmp dir are checked against the full vocabulary; the real
    run passes the per-root set from ``KINDS_BY_ROOT``.
    """
    if valid_kinds is None:
        valid_kinds = ALL_KINDS
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
            kind = match.group(1)
            if kind not in valid_kinds:
                invalid.append(f"{rel} ({kind})")
            elif kind == "propagate" and not _handler_propagates(node):
                invalid.append(f"{rel} (propagate but body never raises or uses the exception)")

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
        m, i, s = _violations(root, KINDS_BY_ROOT[name])
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


def test_gate_scope_covers_the_money_and_engine_packages() -> None:
    """Pin the gated roots: dropping `runner` must fail a test, not pass silently."""
    assert {"live_runner", "runner", "risk"} <= set(GATED_ROOTS)
    runner_handlers = sum(
        len(_broad_handlers(path)) for path in (REPO_ROOT / "runner").rglob("*.py")
    )
    assert runner_handlers > 0


def test_live_runner_kind_set_is_pinned() -> None:
    """The money path must never accept `best-effort`; pin the exact set."""
    assert KINDS_BY_ROOT["live_runner"] == {
        "loop-survival",
        "money-decision",
        "propagate",
    }


def test_gate_flags_a_propagate_marker_that_swallows(tmp_path: Path) -> None:
    """A `propagate` marker on a handler that drops the exception is a lie."""
    pkg = tmp_path / "runner"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception as exc:  # except-kind: propagate - re-raised\n"
        "        pass\n"
        "    try:\n"
        "        other()\n"
        "    except Exception as exc:  # except-kind: propagate - stored for the caller\n"
        "        errors.append(exc)\n"
        "    try:\n"
        "        third()\n"
        "    except Exception as exc:  # except-kind: propagate - logged only\n"
        "        logger.warning('failed: %s', exc)\n"
        "    try:\n"
        "        fourth()\n"
        "    except Exception as exc:  # except-kind: propagate - conditional raise\n"
        "        if exc:\n"
        "            raise\n",
        encoding="utf-8",
    )
    _missing, invalid, _stray = _violations(tmp_path)
    assert invalid == [
        "runner/bad.py:4 (propagate but body never raises or uses the exception)",
        "runner/bad.py:12 (propagate but body never raises or uses the exception)",
        "runner/bad.py:16 (propagate but body never raises or uses the exception)",
    ]


def test_gate_rejects_a_kind_not_allowed_in_that_root(tmp_path: Path) -> None:
    """`best-effort` is valid in runner/ but not on the live money path."""
    pkg = tmp_path / "live_runner"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:  # except-kind: best-effort - degrade to default\n"
        "        pass\n",
        encoding="utf-8",
    )
    missing, invalid, stray = _violations(tmp_path, KINDS_BY_ROOT["live_runner"])
    assert missing == []
    assert invalid == ["live_runner/bad.py:4 (best-effort)"]
    assert stray == []


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
