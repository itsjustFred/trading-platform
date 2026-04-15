//@version=5
strategy("Sample EMA Cross", overlay=true)

fastLength = input.int(9, "Fast Length")
slowLength = input.int(21, "Slow Length")

fast = ta.ema(close, fastLength)
slow = ta.ema(close, slowLength)

longCondition = ta.crossover(fast, slow)
exitCondition = ta.crossunder(fast, slow)

if longCondition
    strategy.entry("Long", strategy.long)

if exitCondition
    strategy.close("Long")
