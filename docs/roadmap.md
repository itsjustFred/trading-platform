# Roadmap and research ideas

This file holds ideas that are not yet concrete enough to be an issue. Bugs and
ready work belong in the issue tracker, not here.

## How this file works

- Each entry says what the idea is, why it matters, where it comes from, and
  what is missing before it can become an issue.
- Status is one of:
  - `idea`: noted, not yet examined.
  - `next`: worth scoping soon.
  - `parked`: deliberately postponed; the entry says until when or why.
  - `issue`: promoted; the entry is reduced to a link.
- Promote an entry to an issue once it has a clear scope and an acceptance
  criterion. Replace the entry text with the issue link instead of keeping both.
- For research topics, do not open a feature issue first. Open a small,
  time-boxed investigation issue ("is X worth building?") whose result is a
  yes/no recommendation, then update the entry here.

## Product and engineering

| Idea | Status | Notes |
|---|---|---|
| Three-way optimization split (train/validation/test) with overfitting detection | `done` | #164 (implemented in `feat/optimizer-three-way-split`) |
| Strategy governance: promotion gate experimental to production | `issue` | #165 |
| Volatility targeting overlay | `issue` | #166 |
| Multi-broker portfolio | `idea` | Combined equity and risk across Alpaca and cTrader. Large; start after the promotion gate (#165). Missing: definition of "combined" (one risk budget or per-broker budgets), currency handling, behaviour when one broker is unreachable. |
| Regime detection as a portfolio overlay | `parked` | Exists only inside individual strategies, not for combinations. Mainly a visual/viewer feature; later. |
| N-way comparison endpoint | `parked` | Compare more than two strategies in one view. Visual/viewer feature; later. |

## Research

| Idea | Status | Notes |
|---|---|---|
| Tranching / rebalance timing luck | `next` | Rebalance date choice alone can move results by up to 350 bp. Candidate for the risk-parity rebalance; natural follow-up to #166. Missing: measure the effect on our own risk-parity backtest first. |
| Strategy ideas from the paper survey | `idea` | SPY intraday momentum, VWAP+ADX for FX, hierarchical risk parity (HRP), deep reinforcement learning. Source: `docs/todo/architektur/research-papers-trading-strategies.md`. Missing: pick one candidate and define the reference result to reproduce. |
| Meta-learning strategy ensemble | `next` | arXiv:2505.03659 (NYU Stern, 2025). The platform has many strategies but no layer that learns how to weight them. Missing: time-boxed investigation; depends on trustworthy out-of-sample numbers (#164 — now done). |
| Order flow / footprint for cTrader | `idea` | VPIN, order book imbalance, footprint imbalance, absorption, high-volume price levels. Reference: GitHub `dhruuvsharma/Trading-Strategies` (portable C#/Python). Missing: check which tick and depth data cTrader actually provides, live and historically. |
| Alternative data, HFT, limit order book, DeFi | `idea` | Broad themes with no concrete proposal retained. Keep only as a reminder; needs a specific question before any work. |
