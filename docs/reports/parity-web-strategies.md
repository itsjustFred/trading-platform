# Parity of the backtest engine on publicly documented strategies

Branch `test/parity-web-strategies` (base `integration/backtest-fixes`). Engine under test:
`NativeAdapter` / `TradeEngine`. Reference: plain-Python event loops in `tests/parity/`
(`reference_turtle.py`, `reference_orb.py`, `reference_vola.py` for the signals,
`reference_execution.py` for fills, intrabar stops, sizing and realized equity). The
reference imports nothing from `runner/`, `risk/`, `shared/` or `strategies/`.

Compared per trade: entry time/price/direction, quantity, exit time/price/reason,
net PnL, and the published equity curve at every point the engine publishes.
Tolerances: price 1e-6, quantity 1e-6 relative, PnL 0.005, curve 0.02.

## Strategies

| Idea | Strategy verified | New? |
|---|---|---|
| Turtle System 1 (20d breakout, 10d exit, N, 2N stop, 0.5N pyramid x4, skip rule + 55d failsafe) | `strategies/turtle_system1` | new (nothing in the repo implements it) |
| Opening Range Breakout (Zarattini/Barbon/Aziz, 5 min, stop 10 % ATR14, flat at close) | `strategies/zarattini_orb` | new (`pynecore_orb` is a different, QuantConnect ORB with a trailing stop) |
| Larry Williams volatility breakout (session open +/- k x prior volatility) | existing `strategies/volatility_breakout` | no: it already implements the idea (intraday, ATR(5) x 0.3, stop at the session open) |

`channel_breakout`, `trend_following` and `intraday_volatility_breakout` were reviewed
and are not Turtle/ORB/Williams ideas. Data: SPY and GLD daily unadjusted OHLC
2005-01-03..2023-12-29 (yfinance, 4781 bars each); Dukascopy USA500 CFD 1-minute data
resampled to 5-minute regular-session bars, 2026-03-20..2026-05-15 (38 full sessions,
2 partial sessions dropped). Fixtures are gzip CSVs in `tests/parity/fixtures/parity/` (210 KB total).

## Parity results

Result on the engine WITH the realized-cash sizing fix of PR #190 (`fix/engine-differential-bugs`
merged into this branch in a scratch checkout, `pytest tests/parity --runxfail`): **all 42 tests
pass, zero differences**.

| Scenario | Engine trades | Reference trades | Differences | Final realized equity (engine = reference) |
|---|---|---|---|---|
| Turtle S1, SPY daily, 2 % risk, pyramiding | 355 | 355 | 0 | 52,508.22 |
| Turtle S1, GLD daily | 318 | 318 | 0 | 103,326.02 |
| ORB, USA500 5 min | 24 | 24 | 0 | 98,078.65 |
| volatility_breakout, USA500 5 min | 141 | 141 | 0 | 102,067.12 |
| Turtle single-unit synthetic, 12 seeds x 420 bars | match | match | 0 | |
| Turtle pyramiding synthetic, 6 seeds | match | match | 0 | |
| ORB / volatility_breakout synthetic sessions, 8 seeds each | match | match | 0 | |

Event classes exercised and compared (counts from the reference):

| Event | SPY | GLD | ORB | VolaBO | Synthetic (asserted minimum) |
|---|---|---|---|---|---|
| Stop-loss exits (intrabar) | 150 | 132 | 18 | 4 | >= 20 (Turtle) |
| Signal exits (10-day channel / session close) | 202 | 185 | 6 | 137 | >= 20 |
| Stop fill when the bar OPENED beyond the stop (gap through) | 30 | 40 | 2 | 0 | >= 3 Turtle, >= 1 ORB |
| Entry bar stopped on its own fill bar (same-bar entry+stop) | 4 | 4 | 0 | 0 | >= 3 |
| Entry skipped, stop on the wrong side of the fill | 0 | 1 | 0 | 0 | >= 1 |
| Unit sized to zero by the 5 % aggregate open-risk ceiling | 45 | 49 | 0 | 0 | >= 1 |
| Four concurrent units | yes | yes | n/a | n/a | yes |
| Shorts | 137 | 136 | 13 | 42 | >= 10 |
| Leverage ceiling (4x) binding on entries (ORB) | | | >= 10 | | |

## Engine findings

1. **Mark-to-market sizing on this base (known, owned by PR #190).** With a profitable open
   unit, the next unit is sized on equity including its unrealized PnL, contradicting the
   realized-cash contract in `docs/sizing-semantics.md`. Reproduction: SPY 2005-02-16 second
   Turtle unit, engine quantity 832.976 vs 826.925 (equity 100,732 vs 100,000); over the full
   SPY run the engine produces 386 trades and 49,304.17 vs the reference 355 and 52,508.22,
   because the different quantities change which later units fit under the 5 % ceiling.
   Single-position strategies (ORB, volatility_breakout, Turtle with `max_units=1`) are unaffected.
   The pyramiding tests are `xfail(strict=True)` on this base; when #190 lands the strict
   xfail fails with a message to remove the marker.
2. **No other engine defect found**, including stop fills, same-bar entry+stop, wrong-side
   skips, the leverage ceiling, short side, and the realized-equity curve.

## Observations (not defects, documented contract behavior)

* A stop that the bar gaps through fills **at the stop level**, not at the open
  (`docs/cross-engine-backtest-contract.md` section 1). Real execution would fill at the
  worse open. Over the real runs the optimism is: SPY 30 gap-through stops = 6,382 of PnL
  (6.4 % of initial capital), GLD 40 = 15,625, ORB 2 = 200. This matters for the Turtle
  results below.
* The published equity curve is realized cash at trade-exit events (206 points for SPY,
  not one per bar); the parity check therefore compares realized equity at those timestamps.
* The strategies assume every signalled unit fills. If the 5 % ceiling sizes a Turtle unit
  to zero (45 times on SPY) the strategy still counts the unit toward the 4-unit limit
  (documented in the strategy docstring).

## Comparison with public statistics

* **Turtle System 1.** The original Turtles traded futures (1983-88) and are reported as
  highly profitable; no public per-trade ledger exists for ETFs. On SPY/GLD 2005-2023 with
  close-based breakouts, per-unit stops and no stop ratchet this implementation shows
  win rate 30 %/29 %, profit factor 0.77/1.01. That is a different market, a modified
  exit/stop model and optimistic gap fills, so it is not a replication of the published
  results; parity here means engine = independent reference, not engine = publication.
* **ORB.** Published results (Zarattini, Barbon, Aziz 2024): top-20 relative-volume
  stocks, 2016-2023, annualized 41.6 % (secondary source: total 1,637 %, Sharpe 2.81). This
  run trades one index CFD for 24 trades in ~2 months with no stock selection and 10 % ATR
  stops that were hit in 18 of 24 trades (win rate 25 %, PF 0.78); the sample is far too
  small and differently constituted to compare, and an independent blog replication on index
  CFDs reports the edge disappearing after spreads. The QQQ paper (Zarattini & Aziz 2023) is a
  different rule set (first-candle stop, 10R target).
* **volatility_breakout.** Source is an NT8 script without published statistics; the existing
  `tests/strategies/test_volatility_breakout*.py` pin its signal logic, the parity test pins
  the execution.

## Reproduce

```bash
.venv/bin/python -m pytest tests/parity -n 2
```
