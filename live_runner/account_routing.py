from __future__ import annotations

from collections.abc import Sequence

from shared.models import LiveAccountConfig


def ctrader_account_identifier(account: LiveAccountConfig) -> str:
    """Return the broker account identifier used for cTrader routing."""
    return str(account.ctid or account.broker_login or account.id).strip()


def validate_live_account_routes(accounts: Sequence[LiveAccountConfig]) -> None:
    """Reject local account definitions that cannot be routed unambiguously."""
    account_ids: set[str] = set()
    ctrader_targets: dict[str, str] = {}

    for account in accounts:
        account_id = str(account.id).strip()
        if account_id in account_ids:
            raise ValueError(f"duplicate account id '{account_id}' in live account configuration")
        account_ids.add(account_id)

        if account.broker != "ctrader":
            continue

        identifier = ctrader_account_identifier(account)
        try:
            numeric_identifier = int(identifier)
            routing_key = str(numeric_identifier) if numeric_identifier > 0 else identifier
        except ValueError:
            routing_key = identifier
        existing_account_id = ctrader_targets.get(routing_key)
        if existing_account_id is not None:
            raise ValueError(
                "cTrader broker account identifier "
                f"'{routing_key}' is assigned to multiple local accounts: "
                f"'{existing_account_id}' and '{account_id}'"
            )
        ctrader_targets[routing_key] = account_id
