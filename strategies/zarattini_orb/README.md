# Zarattini ORB (5 minute, ATR stop)

First 5-minute candle of the session decides direction (bullish long, bearish short,
doji none); entry at the open of the second candle; stop 10 % of the previous-day
daily ATR(14); flat at the session close; one trade per day. Run on 5-minute
regular-session bars with `risk_per_trade_pct=0.01`, `max_leverage=4`, no position cap.

The paper's stock selection ("stocks in play") is not implemented. See the module
docstring for the execution deviations (stop anchored on the signal bar close) and
`docs/reports/parity-web-strategies.md` for the parity results.
