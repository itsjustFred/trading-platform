# Turtle System 1

Published Turtle rules on daily bars: 20-day breakout entry (skipped after a winning
previous breakout unless the 55-day failsafe fires), 10-day opposite-channel exit,
`N` = 20-day smoothed true range, 2N stop per unit, pyramiding one unit per 0.5N up
to 4 units, long and short. Run with `risk_per_trade_pct=0.02` (a 2N stop at 2 % risk
equals the Turtle unit of 1 % of equity per N) and `max_leverage=4`.

Platform deviations (close-based breakouts, next-open fills, per-unit stops without
the Turtle stop ratchet, signal-exit at the close) are listed in the module docstring.
Parity against an independent reference: `tests/parity/test_turtle_parity.py`,
`docs/reports/parity-web-strategies.md`.
