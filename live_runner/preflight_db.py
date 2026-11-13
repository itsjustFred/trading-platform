"""Read-only preflight check of the deployments the daemon actually trades.

``live_runner.daemon.main`` syncs from the ``live_deployments`` table, not from the YAML that
``live_runner.preflight`` validates, so a clean YAML preflight says nothing about what runs. This
module reads the ACTIVE rows straight from SQLite in read-only mode (``mode=ro``: no ``init_db``,
no migration, no lock-taking write) and diffs them against the YAML.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from shared.models import LiveDeploymentConfig

#: (strategy_name, account_id): how a YAML deployment and a DB row are matched.
DeploymentKey = tuple[str, str]


@dataclass(frozen=True)
class DbDeploymentRow:
    id: str
    strategy_name: str
    account_id: str
    symbols: tuple[str, ...]
    timeframe: str
    signal_source: str

    @property
    def key(self) -> DeploymentKey:
        return (self.strategy_name, self.account_id)

    @property
    def label(self) -> str:
        return f"{self.strategy_name} ({self.account_id}, id={self.id})"


@dataclass(frozen=True)
class DbPreflightReport:
    db_path: str
    active_count: int = 0
    matched: list[str] = field(default_factory=list)
    only_in_db: list[str] = field(default_factory=list)
    only_in_yaml: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_db_path(explicit: str | Path | None = None) -> Path:
    """The SQLite file the daemon would use: explicit, ``DATABASE_URL`` or ``DATA_DIR``."""
    if explicit:
        return Path(explicit)
    import os

    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        parsed = urlparse(url)
        if not parsed.scheme.lower().startswith("sqlite"):
            raise ValueError(f"DB preflight supports SQLite only, got {parsed.scheme!r}.")
        raw = unquote(url.split("///", 1)[1]) if "///" in url else ""
        if not raw:
            raise ValueError("DATABASE_URL has no SQLite file path.")
        return Path(raw)
    from shared import constants

    return Path(constants.DATA_DIR) / "backtests.db"


def _load_symbols(raw: Any) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip().upper() for item in parsed if str(item).strip())


def read_active_db_deployments(db_path: Path) -> tuple[list[DbDeploymentRow], set[str]]:
    """The ACTIVE ``live_deployments`` rows and all ``live_accounts`` ids, read-only."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT id, strategy_name, account_id, symbols, timeframe, signal_source "
            "FROM live_deployments WHERE upper(status) = 'ACTIVE' ORDER BY strategy_name, id"
        ).fetchall()
        account_ids = {str(row[0]) for row in connection.execute("SELECT id FROM live_accounts")}
    finally:
        connection.close()
    return (
        [
            DbDeploymentRow(
                id=str(row[0]),
                strategy_name=str(row[1]),
                account_id=str(row[2]),
                symbols=_load_symbols(row[3]),
                timeframe=str(row[4] or ""),
                signal_source=str(row[5] or ""),
            )
            for row in rows
        ],
        account_ids,
    )


def diff_db_and_yaml(
    db_rows: Sequence[DbDeploymentRow],
    db_account_ids: set[str],
    yaml_deployments: Sequence[LiveDeploymentConfig],
    *,
    db_path: str,
) -> DbPreflightReport:
    """Validate the ACTIVE DB rows and report how they differ from the YAML deployments."""
    errors: list[str] = []
    warnings: list[str] = []
    yaml_active = {
        (d.strategy_name, d.account_id): d for d in yaml_deployments if d.status == "ACTIVE"
    }
    db_by_key: dict[DeploymentKey, DbDeploymentRow] = {}
    matched: list[str] = []
    only_in_db: list[str] = []

    for row in db_rows:
        if row.account_id not in db_account_ids:
            errors.append(f"DB deployment {row.label}: account_id is not in live_accounts.")
        if not row.symbols:
            errors.append(f"DB deployment {row.label}: has no symbols configured.")
        if not row.timeframe.strip():
            errors.append(f"DB deployment {row.label}: has no timeframe configured.")
        if row.key in db_by_key:
            warnings.append(
                f"DB deployment {row.label}: duplicate of id={db_by_key[row.key].id}; "
                "both are ACTIVE for the same strategy and account."
            )
        db_by_key.setdefault(row.key, row)
        yaml_dep = yaml_active.get(row.key)
        if yaml_dep is None:
            only_in_db.append(row.label)
            errors.append(
                f"DB deployment {row.label}: ACTIVE in the DB but absent from the YAML, so "
                "this preflight never validated it; the daemon will trade it."
            )
            continue
        matched.append(row.label)
        yaml_symbols = tuple(sorted(str(s).upper() for s in yaml_dep.symbols))
        if tuple(sorted(row.symbols)) != yaml_symbols:
            warnings.append(
                f"DB deployment {row.label}: symbols differ (DB {list(row.symbols)} vs YAML "
                f"{list(yaml_symbols)}); the daemon trades the DB value."
            )
        if row.timeframe != str(yaml_dep.timeframe):
            warnings.append(
                f"DB deployment {row.label}: timeframe differs (DB {row.timeframe!r} vs YAML "
                f"{str(yaml_dep.timeframe)!r}); the daemon trades the DB value."
            )

    only_in_yaml = [
        f"{name} ({account})"
        for (name, account) in sorted(yaml_active)
        if (name, account) not in db_by_key
    ]
    for label in only_in_yaml:
        warnings.append(
            f"YAML deployment {label}: not an ACTIVE row in the DB; the daemon will not trade it."
        )
    return DbPreflightReport(
        db_path=db_path,
        active_count=len(db_rows),
        matched=matched,
        only_in_db=only_in_db,
        only_in_yaml=only_in_yaml,
        errors=errors,
        warnings=warnings,
    )


def run_db_preflight(
    yaml_deployments: Sequence[LiveDeploymentConfig],
    *,
    db_path: str | Path | None = None,
) -> DbPreflightReport:
    """Read the daemon's DB (read-only) and diff it against ``yaml_deployments``."""
    try:
        path = resolve_db_path(db_path)
    except ValueError as exc:
        return DbPreflightReport(db_path=str(db_path or ""), errors=[str(exc)])
    if not path.is_file():
        return DbPreflightReport(
            db_path=str(path),
            errors=[f"Database file not found: {path}; the daemon would start on an empty DB."],
        )
    try:
        rows, account_ids = read_active_db_deployments(path)
    except sqlite3.Error as exc:
        return DbPreflightReport(
            db_path=str(path), errors=[f"Cannot read deployments from {path}: {exc}"]
        )
    return diff_db_and_yaml(rows, account_ids, yaml_deployments, db_path=str(path))
