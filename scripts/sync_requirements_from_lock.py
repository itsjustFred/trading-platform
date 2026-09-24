"""Sync requirements.txt pins from uv.lock (B3: lock-generated, no drift).

Keeps the curated subset in requirements.txt, updating each pin to the
locked version. Packages absent from uv.lock keep their current pin and
are reported. Run from the repo root.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_lock_versions(lock_path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    pattern = re.compile(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"')
    for match in pattern.finditer(lock_path.read_text(encoding="utf-8")):
        versions[match.group(1).lower()] = match.group(2)
    return versions


def main() -> int:
    lock_versions = load_lock_versions(ROOT / "uv.lock")
    req_path = ROOT / "requirements.txt"
    output: list[str] = []
    missing: list[str] = []
    for line in req_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==(.*)", stripped)
        if not match:
            output.append(line)
            continue
        name, extra, _ = match.group(1), match.group(2) or "", match.group(3)
        locked = lock_versions.get(name.lower())
        if locked is None:
            missing.append(name)
            output.append(line)
        else:
            output.append(f"{name}{extra}=={locked}")
    req_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    if missing:
        print(f"kept stale pins (absent from uv.lock): {', '.join(missing)}")
    print(f"synced {len(output)} lines from uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
