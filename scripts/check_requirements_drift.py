from __future__ import annotations

try:
    from sync_requirements_from_lock import main
except ModuleNotFoundError:
    from scripts.sync_requirements_from_lock import main


if __name__ == "__main__":
    raise SystemExit(main(["--check"]))
