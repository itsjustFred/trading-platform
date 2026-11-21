import ast
import re
from pathlib import Path

LIVE_RUNNER = Path(__file__).resolve().parents[2] / "live_runner"
VALID_KINDS = {"loop-survival", "money-decision"}
MARKER_RE = re.compile(r"#\s*except-kind:\s*([A-Za-z-]+)")


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id in {
        "Exception",
        "BaseException",
    }


def _broad_handlers(path: Path) -> list[ast.ExceptHandler]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_broad_except(node)
    ]


def test_every_broad_except_in_live_runner_is_classified() -> None:
    missing: list[str] = []
    invalid: list[str] = []
    stray: list[str] = []

    for path in sorted(LIVE_RUNNER.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        handler_lines = {node.lineno for node in _broad_handlers(path)}

        for node in _broad_handlers(path):
            line = lines[node.lineno - 1]
            match = MARKER_RE.search(line)
            rel = f"{path.relative_to(LIVE_RUNNER)}:{node.lineno}"
            if match is None:
                missing.append(rel)
                continue
            if match.group(1) not in VALID_KINDS:
                invalid.append(f"{rel} ({match.group(1)})")

        for lineno, line in enumerate(lines, start=1):
            if "except-kind:" in line and lineno not in handler_lines:
                stray.append(f"{path.relative_to(LIVE_RUNNER)}:{lineno}")

    assert not missing, "broad except handlers missing # except-kind markers:\n" + "\n".join(
        missing
    )
    assert not invalid, "broad except handlers with invalid # except-kind markers:\n" + "\n".join(
        invalid
    )
    assert not stray, "# except-kind markers outside broad except handlers:\n" + "\n".join(
        stray
    )
