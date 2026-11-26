"""Periodic protective-stop maintenance driver.

The Alpaca adapter's ``maintain_protective_stops`` re-protects positions
whose stop order the broker expired or will age out (GTC orders ~90 days),
and closes at market when the recorded stop is already through the market.
This loop gives that pass a daemon cadence under the broker mutation gate:
the pass can place, cancel and close orders, so the shutdown drain must wait
for it and no pass may start once the stop flag is set - the same contract
as a trailing-stop amendment.

The adapter throttles its own paged ``status=all`` history scan; the tick
only decides when to ask, so a quiet account costs one in-memory check per
interval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from live_runner.base_adapter import BaseLiveAdapter
from live_runner.shutdown import BrokerMutationGate
from shared.logging import get_logger

logger = get_logger("live-runner")

#: Tick of the maintenance loop. The adapter's own scan throttle
#: (``_EXPIRED_STOP_HEAL_INTERVAL_SECONDS``) is longer, so most ticks return
#: before any broker or database I/O.
PROTECTIVE_STOP_MAINTENANCE_TICK_SECONDS = 60.0


async def run_protective_stop_maintenance_loop(
    eligible_adapters: Callable[[], Mapping[str, BaseLiveAdapter]],
    *,
    mutation_gate: BrokerMutationGate,
    interval_seconds: float = PROTECTIVE_STOP_MAINTENANCE_TICK_SECONDS,
) -> None:
    """Drive ``maintain_protective_stops`` on every adapter that has it.

    ``eligible_adapters`` and ``mutation_gate`` are the daemon's in-memory
    reads, so a tick with nothing due costs no broker or database I/O. One
    failed pass never ends the loop; the adapter reports its own alerts.
    """
    while True:
        try:
            for _account_id, adapter in list(eligible_adapters().items()):
                maintain = getattr(adapter, "maintain_protective_stops", None)
                if not callable(maintain):
                    continue
                try:
                    async with mutation_gate() as may_mutate:
                        if not may_mutate:
                            return
                        await maintain()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # except-kind: loop-survival
                    logger.warning(
                        "Protective-stop maintenance pass failed for an adapter",
                        extra={
                            "data": {
                                "account_id": _account_id,
                                "error": repr(exc),
                            }
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "Protective-stop maintenance tick failed",
                extra={"data": {"error": repr(exc)}},
            )
        await asyncio.sleep(interval_seconds)
