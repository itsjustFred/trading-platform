from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORT_ARGS = (
    "export",
    "--format",
    "requirements.txt",
    "--all-extras",
    "--no-editable",
    "--no-emit-project",
    "--no-header",
    "--no-annotate",
    "--no-hashes",
    "--frozen",
)


def _normalize_name(name: str) -> str:
    """PEP 503 normalization for lock/requirements key matching."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_lock_versions(lock_path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    pattern = re.compile(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"')
    for match in pattern.finditer(lock_path.read_text(encoding="utf-8")):
        versions[_normalize_name(match.group(1))] = match.group(2)
    return versions


def _run_uv(args: tuple[str, ...], root: Path) -> str:
    result = subprocess.run(
        ("uv", *args),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"uv {' '.join(args)} failed")
    return result.stdout


def render_requirements(root: Path = ROOT) -> str:
    _run_uv(("lock", "--check"), root)
    output = _run_uv(EXPORT_ARGS, root)
    return output.replace("\r\n", "\n").rstrip() + "\n"


def requirements_drift(root: Path = ROOT) -> str | None:
    expected = render_requirements(root)
    actual_path = root / "requirements.txt"
    actual = actual_path.read_text(encoding="utf-8") if actual_path.exists() else ""
    if actual == expected:
        return None
    return "requirements.txt does not match the locked uv export"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or validate locked requirements.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = render_requirements()
    except (OSError, RuntimeError) as exc:
        print(f"requirements generation failed: {exc}", file=sys.stderr)
        return 1
    path = ROOT / "requirements.txt"
    if args.check:
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            print("requirements.txt does not match the locked uv export", file=sys.stderr)
            return 1
        print("requirements.txt matches uv.lock")
        return 0
    path.write_text(expected, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
