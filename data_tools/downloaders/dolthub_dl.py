"""
Dolthub Downloader — datasets from DoltHub repositories.

DoltHub hosts versioned SQL databases. This downloader uses the DoltHub API
to query and export data to Parquet.

Common trading-relevant repos:
  - dolthub/us-stock-market-daily  (US equities daily OHLCV)
  - dolthub/options               (options chains)
  - post-no-preference/options    (historical options data)

Usage:
    python -m data_tools.downloaders.dolthub_dl --repo dolthub/us-stock-market-daily \\
        --query "SELECT * FROM dolt_history WHERE symbol='AAPL'" --output ~/data

    python -m data_tools.downloaders.dolthub_dl --list-repos
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

DOLTHUB_API = "https://www.dolthub.com/api/v1alpha1"

# Well-known trading data repos
KNOWN_REPOS = [
    {
        "owner": "post-no-preference",
        "repo": "stocks",
        "branch": "master",
        "description": "US stock market daily OHLCV data",
        "asset_class": "equity",
        "tables": ["ohlcv"],
    },
    {
        "owner": "post-no-preference",
        "repo": "options",
        "branch": "master",
        "description": "Historical US equity options data",
        "asset_class": "options",
        "tables": ["option_chain"],
    },
]


class DolthubDownloader(Downloader):
    """Downloads data from DoltHub SQL repositories via REST API."""

    @property
    def source_name(self) -> str:
        return "dolthub"

    def list_available(self, **kwargs) -> list[dict]:
        return [
            {
                "symbol": "*",
                "repo": f"{r['owner']}/{r['repo']}",
                "description": r["description"],
                "asset_class": r["asset_class"],
                "timeframe": "1d",
            }
            for r in KNOWN_REPOS
        ]

    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        repo: str = "post-no-preference/stocks",
        query: str | None = None,
        table: str = "ohlcv",
        branch: str = "master",
        **kwargs,
    ) -> DownloadResult:
        owner, repo_name = repo.split("/", 1)
        symbol = symbol.upper()

        if query is None:
            # Default query for stock data
            conditions = [f"act_symbol = '{symbol}'"]
            if start:
                conditions.append(f"date >= '{start}'")
            if end:
                conditions.append(f"date <= '{end}'")
            where = " AND ".join(conditions)
            query = f"SELECT date, open, high, low, close, volume FROM `{table}` WHERE {where} ORDER BY date"

        logger.info("Querying DoltHub %s/%s: %s", owner, repo_name, query[:100])

        # Execute SQL query via DoltHub API (with retry for timeouts)
        url = f"{DOLTHUB_API}/{owner}/{repo_name}/{branch}"
        params = {"q": query}
        max_retries = 3

        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < max_retries - 1:
                    import time

                    wait = 5 * (attempt + 1)
                    logger.warning(
                        "DoltHub timeout (attempt %d/%d), retrying in %ds...",
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"DoltHub API timeout after {max_retries} attempts. "
                        "The free API is slow for large tables — use narrow date "
                        "ranges and LIMIT clauses."
                    ) from e
            except requests.RequestException as e:
                raise RuntimeError(f"DoltHub API error: {e}") from e

        # Check query execution status
        if data.get("query_execution_status") == "Error":
            raise RuntimeError(
                f"DoltHub query error: {data.get('query_execution_message', 'unknown')}"
            )

        # Parse response into DataFrame
        rows = data.get("rows", [])
        schema = data.get("schema", [])

        if not rows:
            raise RuntimeError(f"No data returned for {symbol} from {repo}")

        col_names = [
            col.get("columnName", col.get("name", f"col_{i}")) for i, col in enumerate(schema)
        ]
        df = pd.DataFrame(rows, columns=col_names)

        # Normalize column names
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ("date", "datetime", "timestamp", "time"):
                col_map[c] = "timestamp"
            elif cl in ("open", "high", "low", "close", "volume"):
                col_map[c] = cl
        df = df.rename(columns=col_map)

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Keep only OHLCV columns
        keep = [
            c for c in ["timestamp", "open", "high", "low", "close", "volume"] if c in df.columns
        ]
        df = df[keep].dropna(subset=["open"]).sort_values("timestamp").reset_index(drop=True)

        # Save
        out_path = self._make_output_path(output_dir, "equity", symbol, timeframe)
        df.to_parquet(out_path, index=False)

        return DownloadResult(
            symbol=symbol,
            timeframe=timeframe,
            source=self.source_name,
            asset_class="equity",
            path=out_path,
            rows=len(df),
            start=str(df["timestamp"].iloc[0]) if "timestamp" in df.columns else "",
            end=str(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else "",
            size_bytes=out_path.stat().st_size,
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://www.dolthub.com",
            license="varies",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download data from DoltHub")
    parser.add_argument("--symbol", help="Symbol to download")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--repo", default="post-no-preference/stocks", help="DoltHub repo")
    parser.add_argument("--query", help="Custom SQL query")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR))
    parser.add_argument("--list-repos", action="store_true", help="List known repos")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    dl = DolthubDownloader()

    if args.list_repos:
        for item in dl.list_available():
            print(f"  {item['repo']:40s}  {item['description']}")
        return

    if not args.symbol and not args.query:
        parser.error("--symbol or --query required")

    result = dl.download(
        args.symbol or "UNKNOWN",
        args.timeframe,
        Path(args.output),
        start=args.start,
        end=args.end,
        repo=args.repo,
        query=args.query,
    )
    print(f"✅ {result.symbol} {result.timeframe}: {result.rows} rows → {result.path}")


if __name__ == "__main__":
    main()
