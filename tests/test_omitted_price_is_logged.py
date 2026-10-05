"""Item 10: an omitted execution price resolved to 0.0 in silence.

One cause of a sentinel value was loud and the other was not, which is the same shape as item 2's
finding two commits earlier.

`_resolve_execution_price` returns 0.0 as its "could not read" sentinel, and a stored
row then carries `actual_price = 0.0`, from which any PnL is meaningless. There are
exactly two causes, and protobuf's presence makes them distinguishable:

| what the broker sent | before | now |
|---|---|---|
| a price of exactly `0` | logged loudly, deliberately | unchanged |
| **no price at all** (`executionPrice` absent on deal *and* order) | **silent** | logged |

The sent-zero warning explains itself as "the one case a reader cannot otherwise notice, because the
broker spoke and said zero". That is true of it, and it is exactly why the *other* case needed a
warning too: an omitted price leaves **nothing** in the payload, so the row simply carries 0.0 and a
reader cannot tell a missing price from a price of zero without re-deriving it from the wire.

The stored value is unchanged, and whether such a row should be refused, flagged or corrected
remains a trading decision. What changes is that both causes are countable, so that decision can
be made on evidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from live_runner.ctrader_adapter import CTraderLiveAdapter as _Adapter


class _Field:
    """A protobuf field with presence: `HasField` is what makes the two causes separable."""

    def __init__(self, **values: Any) -> None:
        self._values = values

    def HasField(self, name: str) -> bool:  # noqa: N802 - protobuf spelling
        return name in self._values

    def __getattr__(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        raise AttributeError(name)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_an_omitted_price_still_resolves_to_zero_and_now_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The value is unchanged; the silence is not."""
    deal = _Field(fillVolume=1.0)
    order = _Field(fillVolume=1.0)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=order, deal=deal) == 0.0

    warnings = _warnings(caplog)
    assert warnings, (
        "a price that was never sent resolves to 0.0, the row carries actual_price=0, and nothing "
        "said so - a reader cannot tell a missing price from a price of zero"
    )
    assert "no execution price at all" in warnings[-1], warnings[-1]


def test_an_explicit_zero_still_gets_its_own_distinct_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two causes must not collapse into one message.

    They are different facts with different fixes - a broker quoting zero is a broker problem, an
    absent price is a payload problem - so a reader needs to tell them apart.
    """
    deal = _Field(fillVolume=1.0, executionPrice=0.0)
    order = _Field(fillVolume=1.0)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=order, deal=deal) == 0.0

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "exactly 0" in warnings[-1], warnings[-1]
    assert "no execution price at all" not in warnings[-1], (
        "the explicit-zero case reported itself as an omitted price"
    )


def test_a_real_price_is_not_logged_at_all(caplog: pytest.LogCaptureFixture) -> None:
    """The cry-wolf direction: a warning on every ordinary fill is one nobody reads."""
    deal = _Field(fillVolume=1.0, executionPrice=261.7)

    with caplog.at_level("WARNING", logger="live-runner"):
        assert _Adapter._resolve_execution_price(order=None, deal=deal) == 261.7

    assert not _warnings(caplog), _warnings(caplog)
