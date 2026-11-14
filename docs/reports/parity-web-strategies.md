# Parity of the backtest engine on publicly documented strategies

Branch `test/parity-web-strategies` (base `integration/backtest-fixes-v3`; the v2 base was
`integration/backtest-fixes-v2`). Engine under test:
`NativeAdapter` / `TradeEngine`. Reference: plain-Python event loops in `tests/parity/`
(`reference_turtle.py`, `reference_orb.py`, `reference_vola.py` for the signals,
`reference_execution.py` for fills, intrabar stops, sizing and the mark-to-market equity curve). The
reference imports nothing from `runner/`, `risk/`, `shared/` or `strategies/`.

Compared per trade: entry time/price/direction, quantity, exit time/price/reason,
net PnL, and the published mark-to-market equity curve at every point the engine publishes.
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

Engine contract under test (base `integration/backtest-fixes-v2` = main + #186/#203 + #188 + #208
chain, including #190 realized-cash sizing, #196 mark-to-market equity, #208 owner decisions
2026-11-13): (A) a bar that opens beyond a stop/target fills at the OPEN, intrabar touches at the
level; (B) signal exits fill at the NEXT bar's open (`signal_exit_fill="next_open"` default; a
last-bar signal closes at the end-of-data close and is flagged
`signal_exit_deferred_to_end_of_data`). The independent reference was updated to these rules
and to the per-bar mark-to-market curve; it still imports nothing from the platform.

`volatility_breakout` is at v1.2.0 (PR #211, base `integration/backtest-fixes-v3`): its force
close is raised on the first bar whose END reaches 14:00 NY (one bar early, so the next-open fill
lands at 14:00), its session-close flatten on the second-to-last in-session bar (fill at the last
in-session bar's open), no entries from that bar on, and market exits carry no `exit_price`. The
reference (`reference_vola.py`) re-implements that rule from the strategy's documented spec with its
own bar-spacing code (most frequent positive bar gap); it does not import `shared/bar_timing.py`.
With the previous (v1.1.0-timing) reference, 9 volatility_breakout parity tests (synthetic seeds 1-8
and USA500) failed on the v3 strategy; after the update all 10 volatility_breakout tests pass with
exact parity.

Result: `pytest tests/parity` **all 47 tests pass, zero differences** (before the update on the
v2 base with the old reference: 30 failed / 5 passed / 8 xfailed; before that on the old base:
34 passed / 8 strict xfail, 42 with #190 in a scratch checkout). The 8 strict xfails are gone
(pyramiding now matches exactly on realized-cash sizing).

| Scenario | Engine trades | Reference trades | Differences | Final realized equity (engine = reference) | Old contract (close exits, gap at level) |
|---|---|---|---|---|---|
| Turtle S1, SPY daily, 2 % risk, pyramiding | 355 | 355 | 0 | 49,215.24 | 52,508.22 |
| Turtle S1, GLD daily | 318 | 318 | 0 | 78,117.72 | 103,326.02 |
| ORB, USA500 5 min | 24 | 24 | 0 | 91,484.50 | 98,078.65 |
| volatility_breakout v1.2.0, USA500 5 min | 125 | 125 | 0 | 104,329.46 | 102,067.12 (v1.1.0 timing, see below) |
| Turtle single-unit synthetic, 12 seeds x 420 bars | match | match | 0 | | |
| Turtle pyramiding synthetic, 6 seeds | match | match | 0 | | |
| ORB / volatility_breakout synthetic sessions, 8 seeds each | match | match | 0 | | |
| ORB legacy `signal_exit_fill="close"` opt-in, 2 seeds | match | match | 0 | | |
| ORB last-bar signal exit -> flagged end-of-data close | match | match | 0 | | |

Event classes exercised and compared (counts from the reference):

| Event | SPY | GLD | ORB | VolaBO | Synthetic (asserted minimum) |
|---|---|---|---|---|---|
| Stop-loss exits (intrabar or gap) | 152 | 142 | 19 | 4 | >= 20 (Turtle) |
| Signal exits at the next open (10-day channel / session close) | 200 | 175 | 5 | 121 | >= 20 |
| Stop fill when the bar OPENED beyond the stop (filled at the open, rule A) | 32 | 50 | 3 | 0 | >= 3 Turtle, >= 1 ORB |
| Entry bar stopped on its own fill bar (same-bar entry+stop) | 4 | 4 | 0 | 0 | >= 3 |
| Entry skipped, stop on the wrong side of the fill | 0 | 1 | 0 | 0 | >= 1 |
| Unit sized to zero by the 5 % aggregate open-risk ceiling | 45 | 49 | 0 | 0 | >= 1 |
| Four concurrent units | yes | yes | n/a | n/a | yes |
| Shorts | 137 | 136 | 13 | 38 | >= 10 |
| Leverage ceiling (4x) binding on entries (ORB) | | | >= 10 | | |

## Engine findings

1. **No engine defect found** on the v2 base: stop/target fills incl. gaps at the open, same-bar
   entry+stop, signal exits at the next open, last-bar deferral, wrong-side skips, realized-cash
   sizing of concurrent units, the leverage ceiling, the short side, and the mark-to-market curve
   all match the independent reference exactly.
2. The earlier finding "mark-to-market sizing of the 2nd+ concurrent unit" (SPY 2005-02-16,
   832.976 vs 826.925; 386 vs 355 trades) is fixed by #190; the strict xfails were removed.

## Effect of the new fill rules (not defects, quantified)

The earlier version of this report noted that gap-through stops filled at the stop level, which
flattered results (SPY 6,382, GLD 15,625, ORB 200 of PnL). Rule A removes that flattering: gap
exits now fill at the open. Rule B moves signal exits to the next open. Final realized equity
from the same reference with each rule switched on separately (initial capital 100,000):

| Run | Old contract | + A only | + B only | A + B (current) |
|---|---|---|---|---|
| Turtle SPY | 52,508.22 | 47,089.57 (-5,418.65) | 54,875.49 (+2,367.27) | 49,215.24 (-3,292.98) |
| Turtle GLD | 103,326.02 | 89,995.97 (-13,330.05) | 89,684.37 (-13,641.65) | 78,117.72 (-25,208.30) |
| ORB USA500 | 98,078.65 | 97,878.35 (-200.30) | 91,671.72 (-6,406.93) | 91,484.50 (-6,594.15) |
| volatility_breakout (v1.1.0 timing, historical) | 102,067.12 | 102,067.12 (0, no gap exits) | 102,244.73 (+177.61) | 102,244.73 (+177.61) |

volatility_breakout v1.2.0 (current; clock exits one bar earlier, no session-close entries): final
realized 104,329.46 (+4,329.46 on 100,000), 125 trades, 121 signal exits at the next open, 4 stop
losses, 0 gap-through stops. Its historical columns above are v1.1.0 numbers and were not re-measured.

(Compounding makes the effects non-additive; the PnL of the gap-stop trades themselves under the
current rules is -38,358 over 32 SPY, -99,083 over 50 GLD and -2,397 over 3 ORB trades.) The ORB
"flat at the session close" signal now fills at the NEXT session's open, so the position is held
overnight: its 5 signal exits and 3 gap stops carry overnight gap risk, which is where the
-6.4k comes from. `signal_exit_fill="close"` restores the intended intraday behaviour and is
parity-tested too (reference `RefConfig.signal_exit_fill="close"`).

## Observations

* The published equity curve is mark-to-market per bar (#196); the parity check compares the
  reference's bar-close MTM equity at every timestamp the engine publishes. On the final bar
  the position is already closed, so it reports realized cash.
* The strategies assume every signalled unit fills. If the 5 % ceiling sizes a Turtle unit
  to zero (45 times on SPY) the strategy still counts the unit toward the 4-unit limit
  (documented in the strategy docstring).

## Comparison with public statistics

* **Turtle System 1.** The original Turtles traded futures (1983-88) and are reported as
  highly profitable; no public per-trade ledger exists for ETFs. On SPY/GLD 2005-2023 with
  close-based breakouts, per-unit stops and no stop ratchet this implementation shows
  win rate 32 %/30 %, profit factor 0.76/0.93. That is a different market, a modified
  exit/stop model and a different fill model, so it is not a replication of the published
  results; parity here means engine = independent reference, not engine = publication.
* **ORB.** Published results (Zarattini, Barbon, Aziz 2024): top-20 relative-volume
  stocks, 2016-2023, annualized 41.6 % (secondary source: total 1,637 %, Sharpe 2.81). This
  run trades one index CFD for 24 trades in ~2 months with no stock selection and 10 % ATR
  stops that were hit in 19 of 24 trades (win rate 12.5 %, PF 0.18 with next-open session exits, i.e. overnight holds); the sample is far too
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
