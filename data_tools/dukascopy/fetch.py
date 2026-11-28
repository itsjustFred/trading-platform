"""Fetch Dukascopy daily m1 candle .bi5 files (BID and ASK) into a raw cache.

Ported from the out-of-repo ``fetch_bi5.py``. Layout::

    <raw_dir>/<SYM>/<SIDE>/<YYYY>/<MM>/<DD>.bi5   (MM 1-based)

A 0-byte file means Dukascopy returned an empty body / 404 (no data that
day). The cache is resumable: existing files are skipped, so a rerun after an
interruption only fetches what is missing.
"""

from __future__ import annotations

import asyncio
import lzma
import os
from datetime import date, timedelta
from pathlib import Path

URL = "http://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m0:02d}/{d:02d}/{side}_candles_min_1.bi5"
SIDES = ("BID", "ASK")


def _day_path(raw: Path, sym: str, side: str, day: date) -> Path:
    return raw / sym / side / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}.bi5"


def jobs(raw: Path, start: date, end: date, syms: list[str]):
    """Yield ``(url, out_path)`` for every missing ``sym/side/day`` file."""
    for sym in syms:
        for side in SIDES:
            day = start
            while day < end:
                out = _day_path(raw, sym, side, day)
                if not out.exists():
                    yield (
                        URL.format(sym=sym, y=day.year, m0=day.month - 1, d=day.day, side=side),
                        out,
                    )
                day += timedelta(days=1)


class FetchStats:
    def __init__(self) -> None:
        self.ok = 0
        self.empty = 0
        self.retries = 0
        self.failed: list[str] = []

    @property
    def done(self) -> int:
        return self.ok + self.empty


async def _fetch_one(session, url: str, out: Path, st: FetchStats) -> None:
    delay = 2.0
    for attempt in range(12):
        try:
            async with session.get(url, timeout=_client_timeout()) as r:
                body = await r.read()
                if r.status == 404:
                    body = b""
                elif r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                elif body:
                    if body.lstrip()[:1] == b"<":
                        raise RuntimeError("HTML body")
                    lzma.decompress(body)  # validate
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, out)
            if body:
                st.ok += 1
            else:
                st.empty += 1
            return
        except Exception:  # noqa: BLE001 - retry everything, give up loudly
            st.retries += 1
            if attempt >= 11:
                st.failed.append(url)
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


def _client_timeout():
    import aiohttp

    return aiohttp.ClientTimeout(total=60)


async def _fetch_all(
    todo: list[tuple[str, Path]], concurrency: int, st: FetchStats
) -> None:
    import aiohttp

    q: asyncio.Queue = asyncio.Queue()
    for j in todo:
        q.put_nowait(j)

    async def worker(session):
        while True:
            try:
                url, out = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            await _fetch_one(session, url, out, st)

    conn = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(
        connector=conn, headers={"User-Agent": "Mozilla/5.0"}
    ) as session:
        await asyncio.gather(*(worker(session) for _ in range(concurrency)))


def fetch_bi5_range(
    raw_dir: Path,
    symbols: list[str],
    start: date,
    end: date,
    *,
    concurrency: int = 8,
) -> FetchStats:
    """Fetch every missing daily ``{BID,ASK}_candles_min_1.bi5`` for the range.

    ``end`` is exclusive. Returns stats; ``st.failed`` lists URLs that never
    succeeded — callers must treat a non-empty list as an incomplete fetch.
    """
    todo = list(jobs(raw_dir, start, end, symbols))
    st = FetchStats()
    if todo:
        asyncio.run(_fetch_all(todo, max(1, concurrency), st))
    return st
