"""Read-only preflight check of the deployments the daemon actually trades.

``live_runner.daemon.main`` syncs from the ``live_deployments`` table, not from the YAML that
``live_runner.preflight`` validates, so a clean YAML preflight says nothing about what runs. This
module reads the ACTIVE rows straight from SQLite in read-only mode (``mode=ro``: no ``init_db``,
no migration, no lock-taking write) and diffs them against the YAML.

A row is matched to a deployment by ``(strategy_name, account_id)`` and compared on the fields the
daemon actually trades: symbols, timeframe, and the signal source that gates which deployments
receive a Discord signal and which credential they are dispatched with. Where they differ the DB
value is what runs, so the difference is reported rather than silently reconciled.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from shared.constants import normalize_symbols, normalize_timeframe
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
    signal_source_id: str
    #: Stored symbol items the daemon cannot match: non-strings, blanks, or padded spellings.
    unclean_symbols: tuple[str, ...] = ()

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


def _sqlite_url_file(url: str) -> str:
    """The file part of a SQLite URL, with any query string and fragment stripped.

    ``shared.database`` hands ``DATABASE_URL`` straight to SQLAlchemy, which reads everything
    after the first ``?`` as driver options rather than as part of the filename, so
    ``sqlite:////abs/backtests.db?timeout=30`` opens ``/abs/backtests.db``. Splitting the raw URL
    kept ``?timeout=30`` in the path, and a ``Path`` carrying it is not a file: a working database
    was reported as "Database file not found".
    """
    without_params = url.partition("?")[0].partition("#")[0]
    return unquote(without_params.split("///", 1)[1]) if "///" in without_params else ""


def resolve_db_path(explicit: str | Path | None = None) -> Path:
    """The SQLite file the daemon would use: explicit, ``DATABASE_URL`` or ``DATA_DIR``.

    An explicit empty path is still explicit: it names no file and fails the existence check,
    rather than quietly checking the default database instead.
    """
    if explicit is not None:
        return Path(explicit)
    import os

    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        parsed = urlparse(url)
        if not parsed.scheme.lower().startswith("sqlite"):
            raise ValueError(f"DB preflight supports SQLite only, got {parsed.scheme!r}.")
        raw = _sqlite_url_file(url)
        if raw == ":memory:":
            # Not a missing file: the daemon would trade ACTIVE rows no on-disk path can show.
            raise ValueError(
                "DATABASE_URL points at an in-memory SQLite database, which this read-only check "
                "cannot inspect; the ACTIVE deployments the daemon trades cannot be verified "
                "here."
            )
        if not raw:
            raise ValueError("DATABASE_URL has no SQLite file path.")
        return Path(raw)
    from shared import constants

    return Path(constants.DATA_DIR) / "backtests.db"


def _load_symbols(raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The stored symbols, and the items the daemon cannot match (as their JSON text).

    The daemon matches a stored symbol with ``item.upper()`` and no strip, so a padded, blank or
    non-string item never equals a signal's symbol. Those items are not normalized into the list
    here: that would hide a row the daemon will not trade behind a clean-looking match.
    """
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return (), ()
    if not isinstance(parsed, list):
        return (), ()
    symbols: list[str] = []
    unclean: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip() and item == item.strip():
            symbols.append(item)
        else:
            unclean.append(json.dumps(item))
    return tuple(symbols), tuple(unclean)


def read_active_db_deployments(db_path: Path) -> tuple[list[DbDeploymentRow], set[str]]:
    """The ACTIVE ``live_deployments`` rows and all ``live_accounts`` ids, read-only."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        columns = {
            str(info[1]) for info in connection.execute("PRAGMA table_info(live_deployments)")
        }
        # The daemon rehydrates the source as ``signal_source or signal_source_type``
        # (``LiveDeploymentModel.to_live_deployment``); an older DB may lack the type column.
        type_column = "signal_source_type"
        if type_column not in columns:
            type_column = "NULL AS signal_source_type"
        rows = connection.execute(
            "SELECT id, strategy_name, account_id, symbols, timeframe, signal_source, "
            f"signal_source_id, {type_column} "
            "FROM live_deployments WHERE upper(status) = 'ACTIVE' ORDER BY strategy_name, id"
        ).fetchall()
        account_ids = {str(row["id"]) for row in connection.execute("SELECT id FROM live_accounts")}
    finally:
        connection.close()
    deployments: list[DbDeploymentRow] = []
    for row in rows:
        symbols, unclean = _load_symbols(row["symbols"])
        deployments.append(
            DbDeploymentRow(
                id=str(row["id"]),
                strategy_name=str(row["strategy_name"]),
                account_id=str(row["account_id"]),
                symbols=symbols,
                timeframe=str(row["timeframe"] or ""),
                # Mirror the daemon's fallback: an empty stored source takes the type.
                signal_source=str(row["signal_source"] or row["signal_source_type"] or ""),
                signal_source_id=str(row["signal_source_id"] or ""),
                unclean_symbols=unclean,
            )
        )
    return (
        deployments,
        account_ids,
    )


def _signal_source(raw: object) -> str:
    """The form the daemon compares: ``str(raw or "internal").lower()``, with no strip.

    The daemon does not strip the stored value, so `` discord`` is not Discord to it; stripping
    here would report such a row as the clean match it is not.
    """
    return str(raw or "internal").lower()


#: The signal sources the daemon routes on; anything else is a stored value it does not recognise.
_KNOWN_SIGNAL_SOURCES = frozenset({"internal", "discord"})


def _db_row_errors(row: DbDeploymentRow, db_account_ids: set[str]) -> list[str]:
    """What makes this ACTIVE row untradeable as configured, independent of the YAML."""
    errors: list[str] = []
    if row.account_id not in db_account_ids:
        errors.append(f"DB deployment {row.label}: account_id is not in live_accounts.")
    if not row.symbols and not row.unclean_symbols:
        errors.append(f"DB deployment {row.label}: has no symbols configured.")
    if row.unclean_symbols:
        errors.append(
            f"DB deployment {row.label}: stored symbols {', '.join(row.unclean_symbols)} are not "
            "clean symbol strings; the daemon's Discord signal match compares a stored symbol "
            "verbatim, so these will never match a signal. Rewrite the row's symbols."
        )
    if not row.timeframe.strip():
        errors.append(f"DB deployment {row.label}: has no timeframe configured.")
    if _signal_source(row.signal_source) not in _KNOWN_SIGNAL_SOURCES:
        errors.append(
            f"DB deployment {row.label}: signal_source {row.signal_source!r} is not 'internal' or "
            "'discord'; the daemon compares the stored text verbatim, so this row does not route "
            "as configured. Rewrite the row's signal_source."
        )
    return errors


def _db_row_drift(row: DbDeploymentRow, yaml_dep: LiveDeploymentConfig) -> list[str]:
    """Fields where this DB row and its YAML twin disagree. The DB value is what runs.

    The comparison normalizes both sides: symbols through
    ``shared.constants.normalize_symbols`` and timeframes through
    ``shared.constants.normalize_timeframe``. Raw comparison would report a difference for every
    cosmetic spelling (``aapl``, ``1H`` vs ``1h``) and the warning would stop carrying
    information. A padded stored symbol is not hidden by this normalization: ``_db_row_errors``
    reports it as an error.

    ``signal_source`` is on this list because the daemon gates Discord dispatch on the DB value
    (``live_runner.daemon._active_discord_deployments_for_account`` and
    ``_discord_active_account_ids``): a row that says ``discord`` on a deployment the YAML
    configures as ``internal`` receives Discord signals, and the YAML-side check in
    ``live_runner.preflight._validate_deployments`` never sees the DB row.
    ``signal_source_id`` is on this list because it names the credential the daemon resolves that
    deployment to.
    """
    differences: list[str] = []
    db_symbols = tuple(sorted(normalize_symbols(row.symbols)))
    yaml_symbols = tuple(sorted(normalize_symbols(yaml_dep.symbols)))
    if db_symbols != yaml_symbols:
        differences.append(f"symbols differ (DB {list(db_symbols)} vs YAML {list(yaml_symbols)})")
    db_timeframe = normalize_timeframe(row.timeframe)
    yaml_timeframe = normalize_timeframe(str(yaml_dep.timeframe))
    if db_timeframe != yaml_timeframe:
        differences.append(
            f"timeframe differs (DB {row.timeframe!r} vs YAML {str(yaml_dep.timeframe)!r})"
        )
    if _signal_source(row.signal_source) != _signal_source(yaml_dep.signal_source):
        differences.append(
            f"signal_source differs (DB {_signal_source(row.signal_source)} vs YAML "
            f"{_signal_source(yaml_dep.signal_source)})"
        )
    if row.signal_source_id.strip() != str(yaml_dep.signal_source_id or "").strip():
        differences.append(
            f"signal_source_id differs (DB {row.signal_source_id!r} vs YAML "
            f"{str(yaml_dep.signal_source_id or '')!r})"
        )
    return differences


def _yaml_active_by_key(
    yaml_deployments: Sequence[LiveDeploymentConfig],
) -> tuple[dict[DeploymentKey, LiveDeploymentConfig], list[str]]:
    """The YAML's ACTIVE deployments by key, plus a warning per shadowed duplicate.

    A dict comprehension keeps the last entry, so a YAML listing the same
    ``(strategy_name, account_id)`` twice would be compared against only one of its two
    definitions and the other would never be checked at all.
    """
    by_key: dict[DeploymentKey, LiveDeploymentConfig] = {}
    warnings: list[str] = []
    for deployment in yaml_deployments:
        if deployment.status != "ACTIVE":
            continue
        key = (deployment.strategy_name, deployment.account_id)
        if key in by_key:
            warnings.append(
                f"YAML deployment {key[0]} ({key[1]}): listed twice in the YAML; only the first "
                "entry is compared against the DB."
            )
            continue
        by_key[key] = deployment
    return by_key, warnings


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
    yaml_active, yaml_warnings = _yaml_active_by_key(yaml_deployments)
    warnings.extend(yaml_warnings)
    yaml_inactive = {
        (deployment.strategy_name, deployment.account_id): deployment.status
        for deployment in yaml_deployments
        if deployment.status != "ACTIVE"
    }
    db_by_key: dict[DeploymentKey, DbDeploymentRow] = {}
    matched: list[str] = []
    only_in_db: list[str] = []

    for row in db_rows:
        errors.extend(_db_row_errors(row, db_account_ids))
        if row.key in db_by_key:
            # Not cosmetic drift: the daemon dispatches every ACTIVE match, so this row submits
            # its own order per signal while the YAML describes a single deployment.
            errors.append(
                f"DB deployment {row.label}: duplicate of id={db_by_key[row.key].id}; the daemon "
                "dispatches every ACTIVE match, so this row trades the same strategy and account "
                "a second time."
            )
        db_by_key.setdefault(row.key, row)
        yaml_dep = yaml_active.get(row.key)
        if yaml_dep is None:
            only_in_db.append(row.label)
            if row.key in yaml_inactive:
                errors.append(
                    f"DB deployment {row.label}: ACTIVE in the DB but the YAML lists it as "
                    f"{yaml_inactive[row.key]}, so this preflight never validated it; the daemon "
                    "will trade it."
                )
            else:
                errors.append(
                    f"DB deployment {row.label}: ACTIVE in the DB but absent from the YAML, so "
                    "this preflight never validated it; the daemon will trade it."
                )
            continue
        matched.append(row.label)
        for difference in _db_row_drift(row, yaml_dep):
            warnings.append(
                f"DB deployment {row.label}: {difference}; the daemon trades the DB value."
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


def db_check_skipped_report(
    reason: str, db_path: str | Path | None = None
) -> DbPreflightReport:
    """A report for a ``--check-db`` run that never inspected the database.

    ``db_deployments is None`` has to keep meaning exactly "not requested", so a run that was
    asked for and did not happen reports the reason instead of leaving the field empty: a caller
    deciding from it whether the DB was inspected must not read "never ran" as "no problems".
    """
    return DbPreflightReport(db_path=str(db_path or ""), errors=[reason])


#: Appended to a read-only open failure that SQLite reports as a write error.
_WAL_READ_ONLY_HINT = (
    "A WAL-mode database can only be read read-only when SQLite may write its -shm index, and a "
    "read-only connection cannot run WAL recovery; open it once with a writer (any daemon start, "
    "or sqlite3) to checkpoint it."
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
    except sqlite3.OperationalError as exc:
        # A mode=ro connection cannot run WAL recovery: SQLite has to create or write the -shm
        # index, so a WAL database that needs recovery (after an unclean stop) - or that merely
        # lives in a directory this process cannot write - reports a write error even though the
        # file itself is intact. Without the cause named that reads as corruption, and the
        # operator goes hunting a damaged database instead of checkpointing one.
        message = f"Cannot read deployments from {path}: {exc}"
        if "readonly" in str(exc).lower():
            # Only the write-refusal form, so a genuine lock timeout is not blamed on the WAL.
            message = f"{message} {_WAL_READ_ONLY_HINT}"
        return DbPreflightReport(db_path=str(path), errors=[message])
    except sqlite3.Error as exc:
        return DbPreflightReport(
            db_path=str(path), errors=[f"Cannot read deployments from {path}: {exc}"]
        )
    return diff_db_and_yaml(rows, account_ids, yaml_deployments, db_path=str(path))
