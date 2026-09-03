import argparse
import logging
import os
import subprocess
from pathlib import Path

import pandas as pd

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("dolthub_manager")

from shared.constants import DATA_DIR
from shared.sql import quote_identifier, quote_literal

DOLT_DIR = DATA_DIR / "dolthub"
EXPORT_DIR = DATA_DIR / "ohlcv" / "dolthub"

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


def build_symbol_select(table_name: str, symbol: str = "") -> str:
    """Build the distinct-symbol query without raw interpolation (DV-B27).

    ``symbol`` is part of the signature for callers that want a literal
    filter; when given it must pass :func:`shared.sql.validate_symbol`.
    """
    if symbol:
        return (
            "SELECT DISTINCT act_symbol FROM "
            f"{quote_identifier(table_name)} WHERE act_symbol = {quote_literal(symbol)}"
        )
    return f"SELECT DISTINCT act_symbol FROM {quote_identifier(table_name)}"


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
            if (repo_path / ".dolt").exists():
                try:
                    run_dolt(["dolt", "pull"], cwd=repo_path)
                except subprocess.CalledProcessError:
                    logger.error(
                        f"Failed to pull {name}. Manual intervention required."
                    )
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
            except subprocess.CalledProcessError:
                logger.error(f"Failed to clone {remote}.")


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
    with open(output_csv, "w") as f:
        run_dolt(["dolt", "table", "export", "-f", table], cwd=repo_path, stdout=f)


def _stock_export_path(target_dir: Path, symbol: str) -> Path:
    """Return the canonical OHLCV output path and migrate legacy flat exports."""
    legacy_path = target_dir / f"{symbol}.parquet"
    canonical_path = target_dir / symbol / "1d.parquet"

    if legacy_path.exists() and not canonical_path.exists():
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(canonical_path)
        logger.info("Migrated legacy Dolthub export %s -> %s", legacy_path, canonical_path)

    return canonical_path


def export_data():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Export Stocks (Daily OHLCV) — per-symbol parquet files
    stocks_repo = DOLT_DIR / "stocks"
    if stocks_repo.exists() and (stocks_repo / ".dolt").exists():
        logger.info("Exporting stocks OHLCV...")
        target_dir = EXPORT_DIR / "equity" / "US"
        target_dir.mkdir(parents=True, exist_ok=True)

        tables = _list_tables(stocks_repo)
        table_name = "ohlcv" if "ohlcv" in tables else (tables[0] if tables else None)
        if not table_name:
            logger.error("No tables found in stocks repo")
        else:
            logger.info(f"Using table: {table_name}")

            # Get symbols
            result = run_dolt(
                ["dolt", "sql", "-q", build_symbol_select(table_name), "-r", "csv"],
                cwd=stocks_repo,
                capture=True,
            )
            lines = result.stdout.strip().split("\n")[1:]  # skip header
            symbols = [l.strip() for l in lines if l.strip()]
            logger.info(f"Found {len(symbols)} symbols to export.")

            for i, symbol in enumerate(symbols):
                out_parquet = _stock_export_path(target_dir, symbol)
                if out_parquet.exists():
                    continue  # skip already exported

                if i % 500 == 0:
                    logger.info(f"  Progress: {i}/{len(symbols)} symbols...")

                try:
                    out_parquet.parent.mkdir(parents=True, exist_ok=True)
                    sym_csv = target_dir / f"{symbol}.csv"
                    with open(sym_csv, "w") as f:
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
                            stdout=f,
                        )
                    df = pd.read_csv(sym_csv)
                    if not df.empty:
                        df.rename(columns={"date": "timestamp"}, inplace=True)
                        df["timestamp"] = pd.to_datetime(df["timestamp"])
                        df.to_parquet(out_parquet, index=False)
                except Exception as e:
                    logger.warning(f"Failed to export {symbol}: {e}")
                finally:
                    csv_path = target_dir / f"{symbol}.csv"
                    if csv_path.exists():
                        csv_path.unlink()

    # Export Earnings — whole tables as parquet
    earnings_repo = DOLT_DIR / "earnings"
    if earnings_repo.exists() and (earnings_repo / ".dolt").exists():
        logger.info("Exporting earnings tables...")
        earnings_dir = DATA_DIR / "fundamentals" / "dolthub" / "earnings"
        earnings_dir.mkdir(parents=True, exist_ok=True)

        for table in _list_tables(earnings_repo):
            out_parquet = earnings_dir / f"{table}.parquet"
            if out_parquet.exists():
                continue
            try:
                csv_path = earnings_dir / f"{table}.csv"
                _export_table_csv(earnings_repo, table, csv_path)
                df = pd.read_csv(csv_path)
                if not df.empty:
                    df.to_parquet(out_parquet, index=False)
                    logger.info(f"  Exported {table}: {len(df)} rows")
                csv_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to export earnings/{table}: {e}")

    # Export Rates
    rates_repo = DOLT_DIR / "rates"
    if rates_repo.exists() and (rates_repo / ".dolt").exists():
        logger.info("Exporting rates...")
        rates_dir = DATA_DIR / "fundamentals" / "dolthub" / "rates"
        rates_dir.mkdir(parents=True, exist_ok=True)

        for table in _list_tables(rates_repo):
            out_parquet = rates_dir / f"{table}.parquet"
            if out_parquet.exists():
                continue
            try:
                csv_path = rates_dir / f"{table}.csv"
                _export_table_csv(rates_repo, table, csv_path)
                df = pd.read_csv(csv_path)
                if not df.empty:
                    df.to_parquet(out_parquet, index=False)
                    logger.info(f"  Exported {table}: {len(df)} rows")
                csv_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to export rates/{table}: {e}")

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
