# Parity of the platform on well-known published strategies

Branch `test/parity-published-strategies` (base `integration/backtest-fixes-v2`). Engine under
test: `NativeAdapter` / `TradeEngine`. Reference: plain-Python event loops in
`tests/parity/ref_published.py` (signals **and** fills, no import from `runner/`, `strategies/`,
`risk/`, `shared/`; enforced by `test_published_reference_independence.py`). Numbers below are
reproduced by `python scripts/report_parity_published.py`.

## Strategies reviewed

| Idea | Strategy file | Duplicate of an existing strategy? |
|---|---|---|
| Faber (2007) 10-month SMA timing, monthly | `strategies/faber_tactical_asset_allocation` | No. `trend_following` (EMA20/50/200 stack + ATR trailing), `sector_rotation` (63/21-day momentum + SMA200, daily), `cross_asset_momentum` (weighted 21-252-day momentum, daily) and `risk_parity` (a volatility-regime long) trade daily signals. None decides once a month on month-end closes vs a 10-month SMA. |
| Connors/Alvarez RSI(2): close>SMA200, RSI(2)<10 buy, close>SMA5 exit | `strategies/connors_rsi2_mean_reversion` | No. `mean_reversion_rsi_bb` is an overnight-gap fade, not RSI/Bollinger despite its name. |
| Antonacci Global Equity Momentum (SPY/VEU or EFA/AGG, 12-month, monthly) | `strategies/antonacci_global_equity_momentum` | No. `sector_rotation`/`cross_asset_momentum` are single-symbol time-series momentum; GEM ranks three slots against each other and against a cash hurdle. |

Review of the WIP ports against the sources: rules are implemented as published (month-end
SMA incl. the current month; Wilder RSI with SMA seed, `<10`, one position, no stop; relative
`us > non_us`, absolute `> cash` with ties to non-US, bonds otherwise). One real defect found and
fixed in the GEM port: its `peer_close_<SYMBOL>` columns were mixed-case but the engines
lower-case every data column on load, so the strategy raised "needs the peer column" in every
engine run; the columns are now lower-case (test in `test_published_signal_edges.py`).

## Data

Yahoo Finance daily data (yfinance 1.6.0), committed as gzip CSV in `tests/parity/fixtures/published/`
(496 KB total; rebuilt by `scripts/build_parity_published_fixtures.py`, raw downloads go to
`data/ohlcv/yfinance/equity/US/<SYM>/1d.parquet`):

| Fixture | Range | Basis |
|---|---|---|
| `spy`, `efa`, `veu`, `agg`, `bil` | 1993 / 2001 / 2007 / 2003 / 2007 .. 2025-12-30 | **dividend-adjusted** OHLC (total return) |
| `gspc` | 1928-01-03 .. 2025-12-30 | S&P 500 **price** index (pre-1962 close only) |
| `sp500tr` | 1988-01-04 .. 2025-12-30 | S&P 500 **total return** index |
| `irx` | 1960 .. 2025 | 13-week T-bill yield, % (used only for the cash-leg estimate in this report) |

## Execution conventions (identical in engine and reference)

Decision on the close of bar *i*; entry fills at the **open** of bar *i+1*; a signal exit also fills at the
**open** of bar *i+1* (`signal_exit_fill="next_open"`, the platform default since the 2026-11-13 owner
decision; an exit and the following entry of a rotation fill at the same open, the entry sized on the
cash the exit just realized, #190); one fully invested position (`risk_per_trade_pct=1.0`,
`max_position_exposure_pct=1.0`, `max_leverage=1.0`, so the 100 % cap is the binding rule);
quantity = cash / **fill-bar open**; a proportional fee is charged on top and is not reserved by the
sizing; a position still open on the last bar (or an exit signalled on it, flagged
`signal_exit_deferred_to_end_of_data`) is closed at that bar's close (`end_of_data`). The equity curve is
mark-to-market per bar (#196: capital + closed net PnL + unrealized gross PnL; fees are recognised when the
trade fully closes), and the reference computes exactly that, so the compared curve is the reference's
`marked` series only. These are platform conventions and differ from the papers' "fill at the signal
close" (Faber, Antonacci). Daily bars have no intrabar stop/target here, so the gap-at-open stop rule
(#208 A) is not exercised by these strategies.

## Parity: platform vs independent reference (all must be exact)

Compared per trade: symbol, entry date and price, quantity, exit date and price, net PnL; plus every
published equity-curve point against the reference's marked equity. Tolerances: price 1e-6,
quantity 1e-9 relative, PnL 0.005, curve 0.02. Multi-symbol GEM runs go through the adapter's shared-equity
replay, which rounds quantities to 4 decimals and sizes later trades on the rounded-PnL equity; there the
tolerance is 5.1e-5 absolute + 1e-6 relative (observed relative drift about 2e-7), and the final equity
differs by 0.02-0.03 on ~400,000 (1e-7). Every other row is identical to the cent.

| Scenario | Engine trades | Reference trades | Differences | Final equity engine = reference |
|---|---|---|---|---|
| Faber SMA10, S&P 500 price index 1928-2012 | 62 | 62 | 0 | 12,425,600.25 |
| Faber SMA10, S&P 500 TR index 1988-2012 | 15 | 15 | 0 | 885,286.00 |
| Faber SMA10, SPY adj. 1993-2025 | 23 | 23 | 0 | 1,726,724.91 |
| Faber SMA10, SPY, 0.1 % fee per side | match | match | 0 | |
| Connors RSI(2), SPY adj. 1993-2025 | 255 | 255 | 0 | 458,854.00 |
| Connors RSI(2), SPY 2015-2025, 0.05 % fee | match | match | 0 | |
| GEM SPY/VEU/AGG (BIL hurdle) 2007-2025 | 32 | 32 | 0 | 404,327.06 (ref 404,327.08) |
| GEM SPY/EFA/AGG (BIL hurdle) 2007-2025 | 33 | 33 | 0 | 396,413.99 (ref 396,413.96) |
| GEM VEU variant with 0.05 % fee | match | match | 0 | |

Non-vacuity: with a deliberately wrong rule on the engine side (Faber 9-month SMA, Connors RSI<5) the
comparison reports differences (`test_*_comparison_detects_a_wrong_rule`). The reference RSI(2) is
SMA-seeded Wilder; a test shows it equals the `ewm(alpha=1/2)` form to 1e-9 after 60 bars, so the seed
convention cannot move a signal after warm-up. GEM trades by slot: SPY 14, VEU 10, AGG 8 (VEU run);
SPY 15, EFA 11, AGG 7 (EFA run), i.e. all three slots rotate.

## Platform vs reference vs paper

Reference metrics are computed on daily mark-to-market equity from the (identical) trades, Sharpe with
rf = 0. "Platform" is `runner.stats.compute_stats_for_result` on the engine result, whose equity curve is
now mark-to-market too (#196), so its CAGR and max drawdown equal the reference's (before #196 the platform
maxDD was -35.9 % / -12.4 % / -14.6 % / -10.6 % / -13.2 % on the first five rows below vs -52.5 % / -23.6 % / -25.8 % /
-14.5 % / -33.7 % marked). The numbers moved from the previous report revision only through the new
next-open exit fill (rule B); the paper comparison is unchanged in substance.

### Faber (2007), S&P 500 10-month SMA timing

Paper (SSRN 962461, 2013 update, 1901-2012, S&P 500 **total return** from Shiller data, cash earns
90-day T-bills, no costs): buy-and-hold ~9.3 % CAGR, timing ~10.2 % CAGR, max drawdown ~-83.7 % vs ~-50.3 %.
These figures were taken from secondary summaries (the paper itself could not be read here); treat them as
approximate.

| Series / window | Strategy CAGR | Buy&hold CAGR | Strategy maxDD (daily MTM) | B&H maxDD | Platform CAGR / maxDD (MTM curve) |
|---|---|---|---|---|---|
| Paper, 1901-2012, TR, T-bill cash | ~10.2 % | ~9.3 % | ~-50.3 % | ~-83.7 % | n/a |
| S&P price index 1928-2012 (no dividends, no cash) | 5.84 % | 5.30 % | -53.8 % | -86.2 % | 5.84 % / -53.8 % |
| same + T-bill cash leg (reference only) | 6.98 % | 5.30 % | -53.8 % | -86.2 % | |
| S&P TR index 1988-2012 (no cash) | 9.12 % | 9.55 % | -23.3 % | -55.3 % | 9.12 % / -23.3 % |
| same + T-bill cash leg (reference only) | 10.02 % | 9.55 % | -21.9 % | -55.3 % | |
| SPY adj. 1993-2025 (no cash) | 9.04 % | 10.69 % | -25.6 % | -55.2 % | 9.04 % / -25.6 % |
| same + T-bill cash leg (reference only) | 9.61 % | 10.69 % | -24.2 % | -55.2 % | |

Explanation of the deviation from the paper:

* **Total return vs price.** The paper's series includes dividends; the 1928-2012 price index does not.
  Buy-and-hold drops from ~9.3 % to 5.3 % (about 4 points, the long-run S&P dividend yield), so absolute
  CAGRs cannot match on a price index. On total-return data (`sp500tr`, SPY adj.) the B&H level (9.55 %,
  10.69 %) is back in the paper's neighbourhood.
* **Cash leg.** The port is long/flat in one symbol, flat months earn 0; the paper earns T-bill interest.
  Adding it (reference only) raises timing by about 1.1 points (price, 1928-2012; 65 % of the time invested)
  and 0.9 / 0.6 points (TR windows).
* **What does reproduce:** the drawdown protection (-86.2 % -> -53.8 % vs the paper's -83.7 % -> -50.3 %),
  the volatility reduction (19.2 % -> 13.0 %), Sharpe improvement (0.37 -> 0.50 / 0.59 with cash) and time in
  market (64-75 %). The paper's ~0.9 point CAGR edge over buy-and-hold shows up on the 1928-2012 window
  with the cash leg (+1.7 points) but not on 1988-2012 (-0.4 / +0.5 points) or 1993-2025 (-1.7 / -1.1
  points): the edge is regime dependent, it is the drawdown/vol benefit that is robust.
* **Execution and data.** Entries and exits fill at the next open (paper: signal close); daily month-end bars instead
  of Shiller monthly averages/closes; 1901-1927 is not available from the data source; no costs in either.

### Connors/Alvarez RSI(2), SPY daily

No paper figures are quoted: the source book's published SPY tables use the *cumulative* RSI(2) variant
(sum of two readings < 35) and period 1993-2007, not this single-day `RSI(2) < 10` rule, and no
reliable published figures for the exact rule could be found. So this is parity with the reference only.

| SPY adj. 1993-2025 | Result |
|---|---|
| Trades (engine = reference) | 255 |
| Total return / CAGR / vol / Sharpe / maxDD (daily MTM; platform curve identical) | 358.9 % / 4.74 % / 6.49 % / 0.75 / -14.9 % |
| Buy and hold | 2734.0 % / 10.69 % / 18.63 % / 0.64 / -55.2 % |
| Time in market | 10.5 % (7.4 % when exits filled at the signal close) |
| Platform win rate | 76.5 % |

The strategy is in the market 10 % of the time; its Sharpe (0.75) exceeds buy-and-hold's (0.64) with a third of the volatility
and a quarter of the drawdown, which is the qualitative claim of the source (high win rate, short holding, small
exposure). Absolute return is low because capital is idle 90 % of the time (no cash yield here).

### Antonacci Global Equity Momentum

Paper (Antonacci 2013/2014, 1974-2013, MSCI series, no costs): ~17.4 % annual return, ~12.6 % volatility,
Sharpe ~0.87, max drawdown ~-22.7 % (secondary summaries). It cannot be reproduced on this data: the ETFs only
start in 2007 (VEU, BIL; EFA 2001, AGG 2003), so the paper's 1974-2007 history, the MSCI/Barclays total-return
indices and the T-bill series are not available; 2007-2025 includes 2008, 2022 and a decade of US dominance.
Parity is therefore against the reference only; the platform run on the ETFs:

| 2007-05 .. 2025-12 | Total return | CAGR | Vol | Sharpe | maxDD (daily MTM) | Platform CAGR / maxDD (MTM curve) |
|---|---|---|---|---|---|---|
| GEM SPY/VEU/AGG, BIL hurdle | 304.3 % | 7.81 % | 15.71 % | 0.56 | -33.7 % | 7.80 % / -33.7 % |
| GEM SPY/EFA/AGG, BIL hurdle | 296.4 % | 7.69 % | 15.59 % | 0.55 | -33.7 % | 7.69 % / -33.7 % |
| SPY buy and hold, same window | 534.1 % | 10.45 % | 19.91 % | 0.60 | -55.2 % | |

Deviation drivers: window (the paper's edge came largely from 1974-2008 non-US and bond regimes); ETF
total-return proxies instead of MSCI indices; entries and exits fill at the next open; no costs (a 0.05 % fee run is
also exact against the reference). The 2008-2009 and 2020 drawdown cuts relative to buy-and-hold (-33.7 % vs
-55.2 %) are consistent with the paper's drawdown story.

## Engine findings

1. **Equity curve and stats were realized-only (FIXED by #196).** Drawdowns of an open position were invisible:
   a long hold through a 40 % intramonth crash reported `max_drawdown_pct == 0.0`; on real runs the platform
   maxDD was -35.9 % vs -52.5 % marked (Faber S&P 1928-2012), -14.6 % vs -25.8 % (SPY Faber) and -13.2 % vs
   -33.7 % (GEM). With the MTM curve the platform equals the reference (-53.8 % / -25.6 % / -33.7 %). The
   former strict `xfail` is now a plain regression test (`tests/parity/test_published_engine_findings.py`).
2. **Sizing price vs fill price (documented here, not a bug):** quantity is `equity / fill-bar open`, not the
   signal-bar close; the fee is not reserved, so a 100 %-invested position with a fee exceeds 100 % of cash by
   the fee. Both are encoded in the reference.
3. **No mechanism to feed peer-symbol data to a strategy.** The engine runs each symbol with only its own frame,
   so any cross-asset strategy (GEM, pairs, rotation) has to carry peers inside its own data columns. The GEM
   test does this with the port's mechanical `attach_peer_closes`; with real catalog data a loader that joins
   peer closes would be needed. Only the pre-existing multi-symbol shared-equity replay is exercised.
4. **Multi-symbol replay rounds quantities to 4 decimals** (relative drift ~2e-7 on GEM over 32 trades).
   Harmless here; the comparison tolerates it explicitly.

## Not verified

The paper numbers above come from secondary summaries; the Connors single-day rule has no published
figures located; the GEM paper window cannot be reproduced with ETF history.
