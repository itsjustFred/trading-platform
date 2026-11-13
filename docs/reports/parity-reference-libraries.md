# Parity of the native engine with open-source backtesting libraries

Branch `test/parity-reference-libraries` (base `integration/backtest-fixes`). Engine under test:
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
(`ref_parity_daily.csv`, seed 20240617: regime drift, overnight gaps, wicks). The generator refuses
to write unless (1) no stop/target bar opens *through* its level, (2) the reference's
one-position-at-a-time gate never binds (so the platform's stateless port is equivalent) and (3) no
trade is open at the end of data (the forced close differs: reference last open, platform last
close). Those three invariants separate documented conventions from engine bugs.

## Ported strategies

| Port | Original | Sizing | Exits |
|---|---|---|---|
| `ref_sma_cross_all_in` | README quickstart `SmaCross` (SMA 10/20) | all-in (`size=1`) | opposite cross (market) |
| `ref_bracket_long` | `BracketLong`: SMA cross, `sl=close-2*ATR14`, `tp=close*1.05`, `size=0.5` | 50 % notional | stop / target only |
| `ref_bracket_short` | mirror, `tp=close*0.96` | 50 % notional | stop / target only |

## Results

Engine run on the committed bars vs the committed reference trades
(`pytest tests/parity/test_reference_library_parity.py`: 14 passed).

| Scenario | Engine trades | Reference trades | Entry time+price | Exit time+price | Quantity | Final equity (engine / reference) |
|---|---|---|---|---|---|---|
| BracketLong, 0 commission | 30 | 30 | 30/30 identical | 30/30 identical (16 stop, 14 target) | identical (ref quantised to 1e-8) | 11,395.00 / 11,395.00 |
| BracketShort, 0 commission | 30 | 30 | 30/30 identical | 30/30 identical (23 stop, 7 target) | identical | 8,481.55 / 8,481.55 |
| SmaCross, 0 commission | 64 | 64 | 64/64 identical | differ by D1 (below), 64/64 | trade 1 identical, later trades differ via D1 | see D1 |
| SmaCross, 0.2 % commission | 64 | 64 | 64/64 identical | differ by D1 | differ by D2 | see D1, D2 |

Reference final equity: SmaCross 0 % = 13,033.54, 0.2 % = 10,089.81.

### Documented semantic differences (each pinned exactly by a test, not tolerated loosely)

* **D1 - signal-exit fill.** `backtesting.py` fills `position.close()` at the *next bar's open*;
  the platform fills a signal exit at the *trigger bar's close*
  (`docs/cross-engine-backtest-contract.md` section 1, decision D4). The test asserts, for every one
  of the 64 trades, platform exit = bar `k` close and reference exit = bar `k+1` open, exit
  timestamps one bar apart. Because realized cash feeds the next sizing, later quantities and final
  equity diverge only through D1; the test therefore checks each side's quantity against its own
  running cash (`quantity == realized cash / entry price`, both frameworks). Protective exits are
  unaffected (bracket runs match exactly), which is why they are the exact-equality check.
* **D2 - commission sizing.** `backtesting.py` floors size onto `price*(1+commission)`; the platform
  sizes on the raw notional and charges fees separately (`runner/fees.py`). First-trade quantity
  ratio is exactly `1+0.002`; platform fees equal `0.002*qty*(entry+exit)` per trade and the
  reference's recorded commission matches the same formula.
* **D3 - gap-through protective fills (not exercised by the fixture, by construction).**
  `backtesting.py` fills a stop that the bar opens beyond at the open (worse) and a target at the
  open (better); the platform fills both at the level (contract section 1). The generator rejects
  data where this happens, so it cannot hide a defect behind the convention.
* **D4 - end-of-data close.** Reference closes a still-open trade at the last open, platform at the
  last close; the fixture is trimmed to a flat bar so neither occurs.

### Cross-check on `backtesting.py`'s bundled GOOG (local only, nothing committed)

SmaCross, 0.2 % commission, 2004-08-19..2013-03-01 (2,148 bars), fractional shares: reference 47
trades (46 signal + 1 forced end-of-data), platform 47 trades, **47/47 entry timestamps identical**;
final equity 63,366.59 (reference) vs 63,193.79 (platform), -0.27 %, fully attributable to D1/D2/D4
(not decomposed trade by trade; the synthetic fixture is the exact check).

## Engine findings

No engine bug found. Reproduction-free: every difference above is a documented convention, and the
protective-exit, entry-fill, short-side, same-bar-stop-first and notional-cap paths match the
original library exactly.

Not a bug but worth knowing: the engine labels exit reasons `stop_loss` and `tp` (asymmetric);
the test asserts those spellings.

## Review of the crashed WIP

* The three ports and the generator were sound; the tests promised by their docstrings
  (`test_reference_library_parity.py`) did not exist and are added here.
* Fixed during review: nothing needed correction in the strategies; the generator's `--no-write`
  reproduces the committed fixtures exactly.
* Base-branch note: sizing tests in `tests/test_engine_behavior.py` fail on this base (PR #190);
  these parity runs use single positions sized on realized cash and are unaffected.
