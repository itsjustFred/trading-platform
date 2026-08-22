# QuantConnect reference fixture

This fixture is a deterministic projection of the real QuantConnect/LEAN
Sleepy Tan Coyote export held outside the repository. It keeps the source
orders, closed trades, portfolio and trade statistics, and all Strategy Equity
OHLC points unchanged. Unused charts, rolling-window data, logs, and analysis
payloads are omitted to keep the test fixture small.

The Orders and Trades CSV files are byte-for-byte copies of the real exports.
The importer tests continue to verify the real counts, identifiers, fees,
equity endpoints, and normalized CSV multisets.

Source artifact SHA-256 values:

- JSON: b88add7cb3f40794dac314e4133222d63fc4ea973d53d399f1340eca3da56e57
- Orders CSV: a567bea292417f9d086c0066474a6e8895e3600673881583dba7875cc776ff24
- Trades CSV: a0156b64e068bffcb927dac865184e417db2f6f3cf8a4b463e461ec8731a2817

Set QUANTCONNECT_REFERENCE_DIR to run the same tests against the complete
external export instead of this repository-local fallback.
