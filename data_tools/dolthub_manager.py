import argparse
import logging
import os
import re
import subprocess
from pathlib import Path

import pandas as pd

from data_tools.path_safety import build_dataset_path
from shared.atomic_io import atomic_replace, write_parquet_atomically
from shared.constants import DATA_DIR
from shared.sql import quote_identifier, quote_literal

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("dolthub_manager")

DOLT_DIR = DATA_DIR / "dolthub"
EXPORT_DIR = DATA_DIR / "ohlcv" / "dolthub"
DOLT_METADATA_DIR = ".dolt"

REPOS = {
    "stocks": "post-no-preference/stocks",
    "earnings": "post-no-preference/earnings",
    "options": "post-no-preference/options",
    "rates": "post-no-preference/rates",
}

# DV-B28: every dolt subprocess runs under a timeout.  Large `dolt clone`
# and `dolt pull` operations override the timeout per call.
DEFAULT_DOLT_TIMEOUT_SECONDS = 600.0


class DoltTimeoutError(RuntimeError):
    """A dolt subprocess exceeded its allowed runtime."""


def _dolt_timeout(override: float | None = None) -> float:
    """Resolve the subprocess timeout: explicit arg > env > default."""
    if override is not None:
        return override
    raw = os.environ.get("DOLT_TIMEOUT_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_DOLT_TIMEOUT_SECONDS


def run_dolt(
    cmd: list[str],
    cwd: str | Path | None = None,
    *,
    timeout: float | None = None,
    capture: bool = False,
    stdout=None,
) -> subprocess.CompletedProcess:
    """Run a dolt subprocess under a mandatory timeout (DV-B28).

    Raises
    ------
    DoltTimeoutError
        When the subprocess exceeds the timeout.
    subprocess.CalledProcessError
        When the subprocess exits non-zero.
    """
    logger.info(f"Running: {' '.join(cmd)}")
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=capture,
            text=capture,
            stdout=stdout,
            timeout=_dolt_timeout(timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise DoltTimeoutError(
            f"dolt command timed out after {_dolt_timeout(timeout)}s: {' '.join(cmd)}"
        ) from exc


def build_distinct_symbol_select(table_name: str) -> str:
    """Build the every-symbol listing query without raw interpolation (DV-B27).

    The table name goes through :func:`shared.sql.quote_identifier`, which
    escapes backticks and raises ``ValueError`` on an empty or otherwise
    unusable identifier. The listing query must not filter on ``act_symbol``
    — every symbol is requested, so routing a wildcard through
    :func:`shared.sql.quote_literal` would raise before the ``dolt`` call.
    """
    return f"SELECT DISTINCT act_symbol FROM {quote_identifier(table_name)}"


def build_symbol_select(table_name: str, symbol: str | None = None) -> str:
    """Build the distinct-symbol query without raw interpolation (DV-B27).

    The table name goes through :func:`shared.sql.quote_identifier`, and a
    given ``symbol`` through :func:`shared.sql.quote_literal`; both validate
    their input and raise ``ValueError`` on anything unsafe. Omitting
    ``symbol`` returns the every-symbol listing query, i.e. exactly what
    :func:`build_distinct_symbol_select` returns; pass a symbol only when the
    caller really wants that one symbol.
    """
    if symbol is None:
        return build_distinct_symbol_select(table_name)
    return (
        "SELECT DISTINCT act_symbol FROM "
        f"{quote_identifier(table_name)} WHERE act_symbol = {quote_literal(symbol)}"
    )


def build_ohlcv_select(table_name: str, symbol: str) -> str:
    """Build the per-symbol OHLCV query without raw interpolation (DV-B27)."""
    return (
        "SELECT date, open, high, low, close, volume FROM "
        f"{quote_identifier(table_name)} WHERE act_symbol = {quote_literal(symbol)} "
        "ORDER BY date"
    )


def update_repos():
    DOLT_DIR.mkdir(parents=True, exist_ok=True)

    for name, remote in REPOS.items():
        repo_path = DOLT_DIR / name
        if repo_path.exists():
            logger.info(f"Updating {name} in {repo_path}...")
            # Check if it is a dolt repo
            if (repo_path / DOLT_METADATA_DIR).exists():
                try:
                    run_dolt(["dolt", "pull"], cwd=repo_path)
                except (subprocess.CalledProcessError, DoltTimeoutError) as exc:
                    # Per-repo update failures are logged and skipped so one
                    # stuck repo does not block the others (DV-B28: the
                    # timeout itself stays mandatory).
                    logger.exception("Failed to pull %s: %s", name, exc)
            else:
                logger.warning(f"{repo_path} exists but is not a dolt repo. Skipping.")
        else:
            logger.info(f"Cloning {remote} to {repo_path}...")
            try:
                # Clones of large repos can legitimately take longer than the
                # default; give them 3x before giving up.
                run_dolt(
                    ["dolt", "clone", remote, str(repo_path)],
                    timeout=3 * DEFAULT_DOLT_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, DoltTimeoutError) as exc:
                logger.exception("Failed to clone %s: %s", remote, exc)


def _list_tables(repo_path: Path) -> list[str]:
    """List tables in a dolt repo using 'dolt sql -r csv'."""
    try:
        result = run_dolt(
            ["dolt", "sql", "-q", "SHOW TABLES", "-r", "csv"],
            cwd=repo_path,
            capture=True,
        )
    except (subprocess.CalledProcessError, DoltTimeoutError) as exc:
        logger.exception("Failed to list tables in %s: %s", repo_path, exc)
        return []
    lines = result.stdout.strip().split("\n")
    # First line is header (Tables_in_{dbname}), rest are table names
    return [line.strip() for line in lines[1:] if line.strip()]


def _export_table_csv(repo_path: Path, table: str, output_csv: Path):
    """Export a dolt table to CSV using 'dolt table export'."""

    def _write(target: Path) -> None:
        with target.open("w", encoding="utf-8", newline="") as handle:
            run_dolt(
                ["dolt", "table", "export", "-f", table],
                cwd=repo_path,
                stdout=handle,
            )

    atomic_replace(output_csv, _write)


def _stock_export_path(target_dir: Path, symbol: str) -> Path:
    """Return the canonical OHLCV output path and migrate legacy flat exports."""
    legacy_path = build_dataset_path(target_dir, f"{symbol}.parquet")
    canonical_path = build_dataset_path(target_dir, symbol, "1d.parquet")

    if legacy_path.exists() and not canonical_path.exists():
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(canonical_path)
        logger.info("Migrated legacy Dolthub export %s -> %s", legacy_path, canonical_path)

    return canonical_path


def _stocks_table_name(stocks_repo: Path) -> str | None:
    tables = _list_tables(stocks_repo)
    if "ohlcv" in tables:
        return "ohlcv"
    return tables[0] if tables else None


def _export_stocks() -> None:
    """Export Stocks (Daily OHLCV) as per-symbol parquet files."""
    stocks_repo = DOLT_DIR / "stocks"
    if not stocks_repo.exists() or not (stocks_repo / DOLT_METADATA_DIR).exists():
        return

    logger.info("Exporting stocks OHLCV...")
    target_dir = EXPORT_DIR / "equity" / "US"
    target_dir.mkdir(parents=True, exist_ok=True)

    table_name = _stocks_table_name(stocks_repo)
    if not table_name:
        logger.error("No tables found in stocks repo")
        return

    logger.info(f"Using table: {table_name}")
    symbols = _list_symbols(stocks_repo, table_name)
    logger.info(f"Found {len(symbols)} symbols to export.")

    for i, symbol in enumerate(symbols):
        if not _is_safe_symbol(symbol):
            logger.warning("Skipping unexpected symbol value: %r", symbol)
            continue

        out_parquet = _stock_export_path(target_dir, symbol)
        if out_parquet.exists():
            continue  # skip already exported

        if i % 500 == 0:
            logger.info(f"  Progress: {i}/{len(symbols)} symbols...")

        _export_symbol_csv(stocks_repo, table_name, symbol, target_dir, out_parquet)


def _list_symbols(stocks_repo: Path, table_name: str) -> list[str]:
    """List every symbol in ``table_name`` (no symbol filter — see the builder).

    Runs under the mandatory dolt timeout (DV-B28). A timeout, a dolt failure
    or an unusable table name returns no symbols instead of aborting the whole
    export, so one bad repo cannot block the others.
    """
    try:
        result = run_dolt(
            ["dolt", "sql", "-q", build_distinct_symbol_select(table_name), "-r", "csv"],
            cwd=stocks_repo,
            capture=True,
        )
    except (subprocess.CalledProcessError, DoltTimeoutError, ValueError) as exc:
        logger.exception("Failed to list symbols in %s: %s", stocks_repo, exc)
        return []
    lines = result.stdout.strip().split("\n")[1:]  # skip header
    return [line.strip() for line in lines if line.strip()]


_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")


def _is_safe_symbol(value: str) -> bool:
    return value not in {".", ".."} and _SYMBOL_RE.fullmatch(value) is not None


def _export_symbol_csv(
    stocks_repo: Path, table_name: str, symbol: str, target_dir: Path, out_parquet: Path
) -> None:
    if not _is_safe_symbol(symbol):
        logger.warning("Skipping unexpected symbol value: %r", symbol)
        return
    try:
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        sym_csv = build_dataset_path(target_dir, f"{symbol}.csv")

        def _write_csv(target: Path) -> None:
            with target.open("w", encoding="utf-8", newline="") as handle:
                run_dolt(
                    [
                        "dolt",
                        "sql",
                        "-q",
                        build_ohlcv_select(table_name, symbol),
                        "-r",
                        "csv",
                    ],
                    cwd=stocks_repo,
                    stdout=handle,
                )

        atomic_replace(sym_csv, _write_csv)
        df = pd.read_csv(sym_csv)
        if not df.empty:
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            write_parquet_atomically(out_parquet, df)
    except Exception as e:
        logger.warning(f"Failed to export {symbol}: {e}")
    finally:
        csv_path = build_dataset_path(target_dir, f"{symbol}.csv")
        if csv_path.exists():
            csv_path.unlink()


def _export_repo_tables(repo_name: str, output_dir: Path) -> None:
    """Export every table of a dolt repo as one parquet file per table."""
    repo_path = DOLT_DIR / repo_name
    if not repo_path.exists() or not (repo_path / DOLT_METADATA_DIR).exists():
        return

    logger.info(f"Exporting {repo_name} tables...")
    output_dir.mkdir(parents=True, exist_ok=True)

    for table in _list_tables(repo_path):
        if not _is_safe_symbol(table):
            logger.warning("Skipping unexpected table value: %r", table)
            continue
        csv_path: Path | None = None
        try:
            out_parquet = build_dataset_path(output_dir, f"{table}.parquet")
            if out_parquet.exists():
                continue
            csv_path = build_dataset_path(output_dir, f"{table}.csv")
            _export_table_csv(repo_path, table, csv_path)
            df = pd.read_csv(csv_path)
            if not df.empty:
                write_parquet_atomically(out_parquet, df)
                logger.info(f"  Exported {table}: {len(df)} rows")
        except Exception as e:
            logger.warning(f"Failed to export {repo_name}/{table}: {e}")
        finally:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)


def export_data():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Export Stocks (Daily OHLCV) — per-symbol parquet files
    stocks_repo = DOLT_DIR / "stocks"
    if stocks_repo.exists() and (stocks_repo / DOLT_METADATA_DIR).exists():
        logger.info("Exporting stocks OHLCV...")
        target_dir = EXPORT_DIR / "equity" / "US"
        target_dir.mkdir(parents=True, exist_ok=True)

        tables = _list_tables(stocks_repo)
        if "ohlcv" in tables:
            table_name: str | None = "ohlcv"
        else:
            table_name = tables[0] if tables else None
        if not table_name:
            logger.error("No tables found in stocks repo")
        else:
            logger.info(f"Using table: {table_name}")

            # Get symbols, through the helper that already carries the timeout and
            # dolt-failure guard. This used to be inlined, and the inline copy had
            # no `except`: a `DoltTimeoutError` from the symbol listing escaped
            # `export_data` and took the earnings and rates exports with it, so one
            # slow or broken `stocks` repo meant *no* fundamentals at all, with a
            # traceback and no partial-success report. The guard was added to
            # `_list_symbols` - which nothing in production called, since this
            # re-implementation is what actually runs.
            symbols = _list_symbols(stocks_repo, table_name)
            logger.info(f"Found {len(symbols)} symbols to export.")

            for i, symbol in enumerate(symbols):
                if not _is_safe_symbol(symbol):
                    logger.warning("Skipping unexpected symbol value: %r", symbol)
                    continue
                if i % 500 == 0:
                    logger.info(f"  Progress: {i}/{len(symbols)} symbols...")

                csv_path: Path | None = None
                try:
                    out_parquet = _stock_export_path(target_dir, symbol)
                    if out_parquet.exists():
                        continue  # skip already exported
                    out_parquet.parent.mkdir(parents=True, exist_ok=True)
                    sym_csv = build_dataset_path(target_dir, f"{symbol}.csv")
                    csv_path = sym_csv

                    def _write_csv(target: Path) -> None:
                        with target.open("w", encoding="utf-8", newline="") as handle:
                            run_dolt(
                                [
                                    "dolt",
                                    "sql",
                                    "-q",
                                    build_ohlcv_select(table_name, symbol),  # noqa: B023 - called synchronously by atomic_replace within this iteration
                                    "-r",
                                    "csv",
                                ],
                                cwd=stocks_repo,
                                stdout=handle,
                            )

                    atomic_replace(sym_csv, _write_csv)
                    df = pd.read_csv(sym_csv)
                    if not df.empty:
                        df.rename(columns={"date": "timestamp"}, inplace=True)
                        df["timestamp"] = pd.to_datetime(df["timestamp"])
                        write_parquet_atomically(out_parquet, df)
                except Exception as e:
                    logger.warning(f"Failed to export {symbol}: {e}")
                finally:
                    if csv_path is not None:
                        csv_path.unlink(missing_ok=True)

    # Export Earnings — whole tables as parquet
    _export_repo_tables(
        "earnings", DATA_DIR / "fundamentals" / "dolthub" / "earnings"
    )

    # Export Rates
    _export_repo_tables("rates", DATA_DIR / "fundamentals" / "dolthub" / "rates")

    logger.info("Export completed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["update", "export"], help="Action to perform")
    args = parser.parse_args()

    if args.action == "update":
        update_repos()
        export_data()
    elif args.action == "export":
        export_data()


if __name__ == "__main__":
    main()
