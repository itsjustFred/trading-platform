# MT5 CI tester tools

Helpers for `.github/workflows/mt5-backtest.yml`, which runs the MetaTrader 5
Strategy Tester on a GitHub-hosted `windows-latest` runner. No real broker
account and no secrets are involved: the price history is our own CSV export,
loaded into a custom symbol, so the tester never needs a connection. The
terminal still has to see *some* account configured; the workflow writes
placeholder `[Common]` values for that, as described below.

| File | Purpose |
|---|---|
| `ImportCustomSymbol.mq5` | MQL5 script that creates the custom symbol, sets its contract specs and replaces its M1 rates from a tab separated MT5 "export bars" CSV. Writes `MQL5\Files\mt5_ci_import_status.txt` (`OK …` / `FAIL …`). |
| `convert_setfile.py` | Turns a Strategy Tester setfile (UTF-16LE + BOM, `name=value\|\|start\|\|step\|\|stop\|\|flag`) into the plain `.set` the tester reads from `MQL5\Profiles\Tester\` — ASCII, no BOM. |
| `inspect_report.py` | Asserts the produced HTML report names the expected EA and symbol and has trades; optionally dumps the deals to JSON. Reads German and English labels, no third-party deps. |

The tester refuses to start with a bare `[Tester]` section
(`tester not started because the account is not specified`). It only checks that
an account is *configured*: the workflow writes placeholder `[Common]`
credentials, the terminal logs `authorization ... failed (Invalid account)` and
then runs the test anyway, because the emulated account comes from
`[Tester]` Deposit/Currency/Leverage and the symbol history is local.

The workflow passes the custom symbol's parameters through
`MQL5\Files\mt5_ci_import.txt` (key=value) rather than `.ini` script
parameters, so no argument ever needs quoting through a config line.

## Silent failures this flow guards against

Each of these still produces a normal-looking report:

- missing `/portable` — the `[Tester]` section is ignored and the terminal just opens
- a BOM (or UTF-16) in `tester.ini` / `.set` — the whole file is ignored
- forward slashes in `Expert=` — the tester falls back to the stock Moving
  Average sample, so the report contains none of our inputs
- `ExpertParameters=` in `MQL5\Presets` instead of `MQL5\Profiles\Tester`
- a non-numeric `Period` (use `16385` for H1, not `H1`)
- a zero-trade run

The workflow therefore parses the compile log for `0 errors`, requires the
import status to start with `OK`, and runs `inspect_report.py` with
`--expect-expert`, `--expect-symbol` and `--min-trades` before uploading.

## Local use

The reference setfile is a release asset, not part of the checkout:

```bash
gh release download mt5-ci-data-v0 --pattern EURUSD_H1_setfile.ini
python tools/mt5_ci/convert_setfile.py --input EURUSD_H1_setfile.ini --output out.set
python tools/mt5_ci/inspect_report.py --report report.html \
    --expect-expert IntradayVolatilityBreakout --expect-symbol EURUSD_CI --min-trades 1
```

Both tools confine their paths to the current directory; pass `--root DIR`
(repeatable) when a path legitimately lives elsewhere.

Tests: `python -m pytest tests/tools`.
