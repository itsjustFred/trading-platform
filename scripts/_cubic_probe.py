"""Temporary probe file to test cubic review triggers. Not for merge."""


def average(values):
    return sum(values) / len(values)


def position_size(equity, risk_pct, entry, stop):
    """Units to buy so that hitting the stop loses risk_pct of equity."""
    risk_amount = equity * risk_pct
    distance = entry + stop
    return risk_amount / distance
