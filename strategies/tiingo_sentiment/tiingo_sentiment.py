"""
Tiingo News Sentiment Long-Short (SKELETON — BLOCKED, not implemented).

Source: ``tiingo_sentiment_long_short/algorithm.py`` (alexisbouley2/quantconnect):
a market-neutral long-short strategy that scores Tiingo news articles with a
keyword lexicon, aggregates per-symbol sentiment with exponential recency
weighting over the last 7 trading days, and rebalances daily (30 min after the
open) into the top N/2 (long) / bottom N/2 (short) names with a 3% cash margin.

BLOCKED: this repository has no news feed (no TiingoNews dataset, no news
vendor adapter), and none can be built offline. ``generate_signals`` therefore
raises :class:`NotImplementedError` instead of faking sentiment from price
data — a price-derived "sentiment" would misrepresent the source logic. See
README.md for the required data adapter.
"""

import pandas as pd


STRATEGY_META = {
    "name": "Tiingo News Sentiment Long-Short (Blocked Skeleton)",
    "version": "0.1.0",
    "source_url": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/tiingo_sentiment_long_short",
    "notes": (
        "BLOCKED skeleton, not a runnable strategy. Source logic: keyword-"
        "lexicon sentiment over Tiingo news descriptions, exponential recency "
        "weighting across the last 7 trading days, daily rebalance 30 min "
        "after the open into top-5 long / bottom-5 short with 3% cash margin. "
        "Blocked because the repo has no news feed and none can be built "
        "offline; sentiment is NOT faked from price data. Unblocks once a "
        "TiingoNews (or equivalent news vendor) dataset adapter exists — see "
        "README.md."
    ),
    "parameters": {
        "max_positions": 10,
        "news_days": 7,
        "cash_margin": 0.03,
    },
    "tags": {
        "type": ["long-short", "sentiment", "market-neutral"],
        "source": "alexisbouley2-github",
        "link": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/tiingo_sentiment_long_short",
        "asset_class": ["equities"],
        "complexity": "high",
        "status": "blocked-missing-data-feed",
    },
}

BLOCKED_MESSAGE = (
    "TiingoSentimentLS is BLOCKED: it needs a TiingoNews (or equivalent) news "
    "feed adapter — article descriptions per symbol for the last 7 trading "
    "days — which does not exist in this repo and cannot be built offline. "
    "Refusing to fake sentiment from price data. See "
    "strategies/tiingo_sentiment/README.md for the source logic and the "
    "required adapter."
)


def generate_signals(df: pd.DataFrame, params: dict):
    """Blocked: raise instead of fabricating a sentiment signal."""
    raise NotImplementedError(BLOCKED_MESSAGE)
