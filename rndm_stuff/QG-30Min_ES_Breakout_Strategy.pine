//@version=6

// xauusd 30min-4h gut; sp500 30min
// https://discord.com/channels/1230977283888648315/1387506825603059762/1387506825603059762


strategy("QG-30Min ES Breakout Strategy", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=100)

////////////// Inputs //////////////
FRACT = input.float(3.3, title="Fractal Factor")
ADX_PERIOD = 45
smooth = 5

// Market open/close times (New York time assumed)
market_open  = timestamp("America/New_York", year, month, dayofmonth, 08, 00)
market_close = timestamp("America/New_York", year, month, dayofmonth, 15, 00)
exit_time    = timestamp("America/New_York", year, month, dayofmonth, 14, 55) // 5 minutes before close

//////Custom function for ADX////////

f_adx(_len, _smooth) =>
    // True Range
    tr = ta.tr(true)
    // Directional Movement
    up = high - high[1]
    down = low[1] - low
    plusDM = na(up) ? 0 : up > down and up > 0 ? up : 0
    minusDM = na(down) ? 0 : down > up and down > 0 ? down : 0
    // Smooth TR, +DM, -DM using Wilder's method
    tr_smooth = ta.rma(tr, _len)
    plusDM_smooth = ta.rma(plusDM, _len)
    minusDM_smooth = ta.rma(minusDM, _len)
    // Calculate +DI and -DI
    plusDI = 100 * plusDM_smooth / tr_smooth
    minusDI = 100 * minusDM_smooth / tr_smooth
    // Calculate DX
    dx = (plusDI + minusDI) != 0 ? 100 * math.abs(plusDI - minusDI) / (plusDI + minusDI) : na
    // Smooth DX to get ADX
    adx = ta.rma(dx, _smooth)
    // Return ADX, +DI, -DI
    [adx, plusDI, minusDI]

[adx, plusDI, minusDI] = f_adx(ADX_PERIOD, smooth)

////////////// Calculations //////////////
in_trade = strategy.position_size != 0
new_day = ta.change(time("D"))

// Only trade once per day
var int trade_count = 0
if not na(new_day) and new_day != 0
    trade_count := 0

in_time = time >= market_open and time < market_close
can_trade = not in_trade and trade_count == 0 and in_time

// Breakout levels
breakout_long  = low + FRACT * math.abs(open - low)
breakout_short = high - FRACT * math.abs(open - low)

// Conditions
filter_long  = adx < 30
filter_short = adx > 30

enter_long  = can_trade and filter_long and close < breakout_long
enter_short = can_trade and filter_short and close < breakout_short

////////////// Entries //////////////
if enter_long
    strategy.entry("Long", strategy.long, stop=breakout_long)
    trade_count := trade_count + 1
    label.new(bar_index, high, "Buy", style=label.style_label_up, color=color.green, textcolor=color.white)

if enter_short
    strategy.entry("Short", strategy.short, stop=breakout_short)
    trade_count := trade_count + 1
    label.new(bar_index, low, "Sell", style=label.style_label_down, color=color.red, textcolor=color.white)

////////////// Exit: 5 min before close //////////////
exit_now = time >= exit_time and in_trade

if exit_now
    strategy.close("Long")
    strategy.close("Short")
    label.new(bar_index, close, "Exit", style=label.style_label_left, color=color.orange, textcolor=color.white)

////////////// Plot breakout levels for visual reference //////////////
plotshape(enter_long, location=location.belowbar, color=color.green, style=shape.triangleup, size=size.small, title="Long Entry Signal")
plotshape(enter_short, location=location.abovebar, color=color.red, style=shape.triangledown, size=size.small, title="Short Entry Signal")
