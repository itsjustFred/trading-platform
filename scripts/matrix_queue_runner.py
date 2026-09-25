#!/usr/bin/env python3
"""Run the backtest matrix strategy queue conservatively.

Two cron sessions can call this script. A lock file guarantees that only one
session runs at a time. If a session dies, the next cron tick retakes the lock.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

REPO = Path(
    os.environ.get("TRADING_PLATFORM_REPO", str(Path(__file__).resolve().parents[1]))
)
VENV_PYTHON = Path(os.environ.get("PYTHON_BIN", sys.executable))
MATRIX_DIR = Path(
    os.environ.get("MATRIX_OUTPUT_DIR", str(REPO / "data" / "backtests" / "matrix_runner_v2"))
)
STATE_PATH = MATRIX_DIR / "queue_state.json"
LOCK_PATH = MATRIX_DIR / "queue.lock"
MAX_WORKERS = os.environ.get("MATRIX_MAX_WORKERS", "2")
TIMEOUT_SECONDS = os.environ.get("MATRIX_TIMEOUT_SECONDS", "3600")
ENGINE = os.environ.get("MATRIX_ENGINE", "vectorbt")

_LOCK_HANDLE = None


def _ensure_repo_on_path() -> None:
    repo = str(REPO)
    if repo not in sys.path:
        sys.path.insert(0, repo)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strategy_slugs() -> list[str]:
    _ensure_repo_on_path()
    from shared.strategy_registry import iter_strategy_files, strategy_slug_for_path

    slugs: list[str] = []
    for path in iter_strategy_files(REPO / "strategies"):
        if path.name.endswith("_context.py"):
            continue
        slugs.append(strategy_slug_for_path(path, REPO / "strategies"))
    return sorted(dict.fromkeys(slugs))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "strategies": [],
        "current_strategy": None,
    }


def save_state(state: dict) -> None:
    MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def any_matrix_process_running() -> bool:
    """Liveness probe (diagnostics only — never used for lock decisions)."""
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return False
    try:
        proc = subprocess.run(
            [pgrep, "-f", r"python.*runner\.cli matrix"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _lock_metadata_path() -> Path:
    return LOCK_PATH.with_name(f"{LOCK_PATH.name}.metadata")


def acquire_lock() -> bool:
    """Take the queue lock for this process."""
    MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(LOCK_PATH))
    try:
        lock.acquire(timeout=0)
        _lock_metadata_path().write_text(
            json.dumps({"pid": os.getpid(), "acquired_at": now_iso()}) + "\n",
            encoding="utf-8",
        )
    except Timeout:
        return False
    except OSError:
        with contextlib.suppress(Exception):
            lock.release()
        return False
    global _LOCK_HANDLE
    _LOCK_HANDLE = lock
    return True


def release_lock() -> None:
    """Release the queue lock held by this process, if any."""
    global _LOCK_HANDLE
    lock = _LOCK_HANDLE
    if lock is None:
        return
    try:
        lock.release()
    finally:
        _LOCK_HANDLE = None


def grid_summary(strategy: str) -> dict | None:
    path = MATRIX_DIR / strategy / "grid_summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"load_error": str(exc)}


def refresh_strategy_states(state: dict) -> None:
    rows = []
    for slug in strategy_slugs():
        summary = grid_summary(slug)
        existing = next(
            (row for row in state.get("strategies", []) if row.get("strategy") == slug),
            {},
        )
        trades = (
            len(list((MATRIX_DIR / slug).glob("*.trades.json")))
            if (MATRIX_DIR / slug).exists()
            else 0
        )
        if summary and not summary.get("load_error"):
            status = "done" if summary.get("failed", 0) == 0 else "failed"
            row = {
                "strategy": slug,
                "status": status,
                "ok": summary.get("ok", 0),
                "failed": summary.get("failed", 0),
                "skipped_existing": summary.get("skipped_existing", 0),
                "requested": summary.get("requested", 0),
                "trades": trades,
                "completed_at": summary.get("completed_at"),
                "updated_at": now_iso(),
            }
        elif trades:
            row = {
                "strategy": slug,
                "status": "partial",
                "ok": existing.get("ok", 0),
                "failed": existing.get("failed", 0),
                "skipped_existing": existing.get("skipped_existing", 0),
                "requested": existing.get("requested", 0),
                "trades": trades,
                "updated_at": now_iso(),
            }
        else:
            row = {
                "strategy": slug,
                "status": "pending",
                "ok": 0,
                "failed": 0,
                "skipped_existing": 0,
                "requested": 0,
                "trades": 0,
                "updated_at": now_iso(),
            }
        rows.append(row)
    state["strategies"] = rows


def next_pending_strategy(state: dict) -> str | None:
    refresh_strategy_states(state)
    priority = {"partial": 0, "failed": 1, "pending": 2}
    candidates = [r for r in state.get("strategies", []) if r.get("status") in priority]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda r: (priority[r.get("status")], r.get("strategy", "")),
    )["strategy"]


def run_strategy(slug: str) -> dict:
    """Run one strategy matrix cell in a subprocess and return its result payload."""
    _ensure_repo_on_path()
    from runner.agent_process import build_backtest_child_env

    cmd = [
        str(VENV_PYTHON),
        "-m",
        "runner.cli",
        "matrix",
        "--strategy",
        slug,
        "--engine",
        ENGINE,
        "--max-workers",
        MAX_WORKERS,
        "--timeout-seconds",
        TIMEOUT_SECONDS,
        "--skip-sqlite",
        "--resume",
        "--output-dir",
        str(MATRIX_DIR),
    ]
    started = now_iso()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=build_backtest_child_env(),
            timeout=int(TIMEOUT_SECONDS) + 120,
        )
        output_tail = proc.stdout[-8000:] if proc.stdout else ""
        summary = grid_summary(slug)
        completed = (
            proc.returncode == 0
            and bool(summary)
            and not summary.get("load_error")
            and summary.get("failed", 0) == 0
        )
        return {
            "strategy": slug,
            "status": "done" if completed else "failed",
            "returncode": proc.returncode,
            "started_at": started,
            "finished_at": now_iso(),
            "summary": summary,
            "output_tail": output_tail,
        }
    except Exception as exc:
        return {
            "strategy": slug,
            "status": "failed",
            "returncode": None,
            "started_at": started,
            "finished_at": now_iso(),
            "error": str(exc),
        }


def main() -> int:
    if not acquire_lock():
        print("Matrix queue: another session is already running; skipping this tick.")
        return 0

    ran_any = False
    try:
        while True:
            state = load_state()
            refresh_strategy_states(state)
            slug = next_pending_strategy(state)
            if not slug:
                if ran_any:
                    print("Matrix queue: all strategies are done or failed.")
                else:
                    print("Matrix queue: no pending strategies found.")
                save_state(state)
                print(render_state(state))
                return 0 if ran_any else 1

            state["current_strategy"] = slug
            save_state(state)
            print(f"Matrix queue: starting {slug} with max_workers={MAX_WORKERS}")
            result = run_strategy(slug)
            ran_any = True
            state["last_run"] = result
            state["current_strategy"] = None
            refresh_strategy_states(state)
            save_state(state)
            print(render_state(state))

            # Keep the queue alive between cron wakeups so there is no idle gap
            # while pending strategies remain.
            continue
    finally:
        release_lock()


def render_state(state: dict) -> str:
    rows = state.get("strategies", [])
    done = sum(1 for r in rows if r.get("status") == "done")
    failed = sum(1 for r in rows if r.get("status") == "failed")
    partial = sum(1 for r in rows if r.get("status") == "partial")
    pending = sum(1 for r in rows if r.get("status") == "pending")
    trades = sum(int(r.get("trades", 0)) for r in rows)
    cells_ok = sum(int(r.get("ok", 0)) for r in rows)
    cells_total = sum(int(r.get("requested", 0)) for r in rows)
    current = (
        state.get("current_strategy")
        or (state.get("last_run") or {}).get("strategy")
        or "none"
    )
    lines = [
        "🧭 Backtest Matrix Queue",
        f"Output: `{MATRIX_DIR}`",
        (
            f"Strategy progress: `{done}/{len(rows)}` done · failed `{failed}` · "
            f"partial `{partial}` · pending `{pending}`"
        ),
        f"Cells: `{cells_ok}/{cells_total}` ok · Trade JSONs: `{trades}`",
        f"Current/last: `{current}`",
        "",
        "Strategies:",
    ]
    for r in rows:
        status = r.get("status")
        if status == "done":
            emoji = "✅"
        elif status == "failed":
            emoji = "❌"
        elif status == "partial":
            emoji = "⚠️"
        else:
            emoji = "⏳"
        suffix = f" · {r.get('trades', 0)} trades"
        if r.get("status") == "done":
            suffix += f" · {r.get('ok', 0)}/{r.get('requested', 0)} ok"
        lines.append(f"- {emoji} `{r.get('strategy')}` — {r.get('status')}{suffix}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
