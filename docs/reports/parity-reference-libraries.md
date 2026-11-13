# Parity of the native engine with open-source backtesting libraries

Branch `test/parity-reference-libraries` (base `integration/backtest-fixes-v2`). Engine under test:
`NativeAdapter` / `TradeEngine`. Reference: the **original** `backtesting.py` 0.6.6 (AGPL-3.0),
running its own strategy classes unchanged (`scripts/reference_parity/reference_strategies.py`,
`FractionalBacktest` so share counts are fractional like the platform's). Backtrader was not
attempted (optional in the brief; its fill model, next-bar-open market orders, is the same as the
one compared here).

Only the reference's *numeric output* is committed (`tests/parity/fixtures/ref_parity_*`, 212 KB);
the library is never vendored and `pytest tests/parity/test_reference_library_parity.py` does not
need it. Regenerate / re-verify with
`python -m scripts.reference_parity.generate_reference_parity_fixtures [--no-write]`
(re-run during this review: all four runs reproduce the committed manifest bit for bit).

## Data and licence

`backtesting.test.GOOG` (Yahoo quotes shipped inside an AGPL package) has no clear redistribution
grant, so it is **not** committed. The fixture is 2,400 seeded synthetic daily bars
(`ref_parity_daily.csv`, seed 20240617: regime drift, overnight gaps, wicks). Overnight gaps are now
fat-tailed on purpose (8 % of bars carry an extra N(0, 2.5 %) jump) so that some stops and targets are
opened through; 13 protective exits across the two bracket runs do (5 long, 8 short; stops and targets, 2 of
them the D5 entries below; listed per run as `gap_through_exits` in `ref_parity_summary.json`). The generator still refuses to write
unless (1) the reference's one-position-at-a-time gate never binds (so the platform's stateless port is
equivalent) and (2) no trade is open at the end of the data (the forced close differs: reference last
open, platform last close). The former invariant "no bar opens through a level" was dropped because the
platform now fills such exits exactly like the library (D3 below). The synthetic series, hence every
number below, changed from the previous revision of this report.

## Ported strategies

| Port | Original | Sizing | Exits |
|---|---|---|---|
| `ref_sma_cross_all_in` | README quickstart `SmaCross` (SMA 10/20) | all-in (`size=1`) | opposite cross (market) |
| `ref_bracket_long` | `BracketLong`: SMA cross, `sl=close-2*ATR14`, `tp=close*1.05`, `size=0.5` | 50 % notional | stop / target only |
| `ref_bracket_short` | mirror, `tp=close*0.96` | 50 % notional | stop / target only |

## Results

Engine run on the committed bars vs the committed reference trades
(`pytest tests/parity/test_reference_library_parity.py`: 18 passed). Engine = platform defaults
(`signal_exit_fill="next_open"`, gap-at-open protective fills, sizing on realized cash).

| Scenario | Engine trades | Reference trades | Entry time+price | Exit time+price | Quantity | Final equity (engine / reference) |
|---|---|---|---|---|---|---|
| BracketLong, 0 commission | 30 | 30 | 30/30 identical | 30/30 identical (15 stop, 15 target; 5 gap-through) | identical (ref quantised to 1e-8) | 11,286.87 / 11,286.87 |
| BracketShort, 0 commission | 28 (+2 D5) | 30 | 28/28 identical | 28/28 identical (stop/target; 6 gap-through) | identical | 8,854.59 / 8,854.59 |
| SmaCross, 0 commission | 64 | 64 | 64/64 identical | 64/64 identical (signal, next open) | 64/64 identical | 13,777.10 / 13,777.10 |
| SmaCross, 0.2 % commission | 64 | 64 | 64/64 identical | 64/64 identical | differ by D2 | see D2 (reference 10,665.43) |

Before this update (base `integration/backtest-fixes`, previous fixture): the SmaCross rows had
exit time and price differing on 64/64 trades (D1) and, through it, later quantities and the
final equity; the bracket rows matched exactly because the fixture was filtered to contain no
gaps. Now every field of every compared trade is identical, with the single documented exception D5.

### Documented semantic differences (each pinned exactly by a test, not tolerated loosely)

* **D1 - signal-exit fill: GONE.** `backtesting.py` fills `position.close()` at the *next bar's
  open*; the platform's default is now the same (`signal_exit_fill="next_open"`, owner decision
  2026-11-13, #208 B). The test asserts exit timestamp and price equal the reference's on all 64
  trades of both SmaCross runs; with no divergence in realized cash the zero-commission run is
  identical in quantity, PnL and final equity (`test_zero_fee_run_is_identical_...`).
* **D2 - commission sizing.** `backtesting.py` floors size onto `price*(1+commission)`; the platform
  sizes on the raw notional and charges fees separately (`runner/fees.py`). First-trade quantity
  ratio is exactly `1+0.002`; platform fees equal `0.002*qty*(entry+exit)` per trade and the
  reference's recorded commission matches the same formula. Unchanged.
* **D3 - gap-through protective fills: NOW AGREED and compared.** `backtesting.py`
  (`Backtest._process_orders`, 0.6.6) fills the contingent stop of a long as `min(open, stop)` and the
  contingent target as `max(open, target)` (mirrored for shorts): at the OPEN when the bar opens
  beyond the level (worse for a stop, better for a target), at the level on an intrabar touch. The
  platform rule #208 A is the same, for stops and targets and for both sides. The fixture contains
  13 such exits (stops and targets, long and short; 11 comparable + the 2 D5 entries) and every comparable one matches to 1e-9
  (`test_gap_through_exits_fill_at_the_open_and_touches_at_the_level`, which also pins that touches
  fill at the level and that the manifest lists exactly the gapped trades).
* **D4 - end-of-data close.** Reference closes a still-open trade at the last open, platform at the
  last close (flagged `end_of_data`; a signal exit on the last bar becomes
  `signal_exit_deferred_to_end_of_data`); the fixture is trimmed to a flat bar so neither occurs.
  Still different, still excluded by construction (seen on GOOG below).
* **D5 - entry gapped through its own stop (new, found by D3).** On two bracket-short signals the
  next bar opens above the short's stop (published from the signal bar close). `backtesting.py`
  enters at that open and stops out at the same open: a zero-PnL one-bar round trip. The platform
  refuses the entry ("Skipping market order with invalid sizing stop at fill",
  `runner/engine_pkg/trading_engine.py`), so it has 28 trades vs 30. Equity is identical because the
  round trips are free; `test_entries_gapped_through_their_own_stop_are_refused_by_the_platform` pins
  that exactly the zero-PnL same-bar stop-outs are absent and no other trade is. Not changed here:
  rejecting an entry whose protective stop is already breached at the fill is the risk-sizing
  policy (a stop on the wrong side of the entry cannot size a risk-based position), a maintainer
  may decide otherwise.

### Cross-check on `backtesting.py`'s bundled GOOG (local only, nothing committed)

SmaCross, 0.2 % commission, 2004-08-19..2013-03-01 (2,148 bars), fractional shares: reference 47
trades (46 signal + 1 forced end-of-data), platform 47 trades, **47/47 entry timestamps, 47/47 exit
timestamps and 46/46 signal-exit prices identical** (before: entries only, every exit one bar apart).
The remaining difference is the end-of-data trade (D4: reference last open, platform last close,
exit timestamp equal, price differs). Final equity 63,366.59 (reference) vs 64,234.83 (platform),
+1.37 %, attributable to D2 (the platform sizes ~0.2 % larger on every trade and compounds it) and D4;
not decomposed trade by trade (the synthetic fixture is the exact check). Before: 63,193.79, -0.27 %.

## Engine findings

No engine bug found. Every remaining difference above (D2, D4, D5) is a documented convention, and the
protective-exit (including gap-at-open fills), signal-exit, entry-fill, short-side, same-bar-stop-first
and notional-cap paths match the original library exactly. Whether D5 (refusing an entry whose stop is
already breached at the fill, instead of the library's open-and-stop-out at zero PnL) is the desired
policy is a maintainer decision; it costs nothing in equity.

Not a bug but worth knowing: the engine labels exit reasons `stop_loss` and `tp` (asymmetric);
the test asserts those spellings.

## Review of the crashed WIP

* The three ports and the generator were sound; the tests promised by their docstrings
  (`test_reference_library_parity.py`) did not exist and are added here.
* Fixed during review: nothing needed correction in the strategies; the generator's `--no-write`
  reproduces the committed fixtures exactly.
* Rebased onto `integration/backtest-fixes-v2` (2026-11-13: #190 sizing on realized cash, #196
  mark-to-market curves, #208 gap-at-open fills and next-open signal exits). The reference
  fixtures were regenerated with the fat-tailed-gap data (generator `--no-write` reproduces them) and
  the D1/D3 tests were rewritten from "difference pinned" to "exact equality".
