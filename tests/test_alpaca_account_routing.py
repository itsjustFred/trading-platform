from live_runner.alpaca_adapter import resolve_alpaca_credentials


def test_resolve_alpaca_credentials_matches_account_number_hint() -> None:
    credentials = resolve_alpaca_credentials(
        broker_login="PA3EFBL4P7BU",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
            "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC2": "PA3EFBL4P7BU",
            "ALPACA_ENDPOINT_ACC2": "https://paper-api.alpaca.markets/v2",
            "ALPACA_API_KEY_ACC3": "ramjet-key",
            "ALPACA_SECRET_KEY_ACC3": "ramjet-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC3": "PA3T0MQAGQ51",
            "ALPACA_ENDPOINT_ACC3": "https://paper-api.alpaca.markets/v2",
        },
    )

    assert credentials == ("rocket-key", "rocket-secret", True)


def test_resolve_alpaca_credentials_does_not_fallback_when_hints_exist() -> None:
    credentials = resolve_alpaca_credentials(
        broker_login="PA3UNKNOWN",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
            "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
        },
    )

    assert credentials is None


def test_resolve_alpaca_credentials_hint_block_beats_default_pair() -> None:
    """Kills mutant: default key/secret pair short-circuits before ACC{N} hint
    matching, routing every account onto the same default credentials."""
    credentials = resolve_alpaca_credentials(
        broker_login="PA3EFBL4P7BU",
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
            "ALPACA_ENDPOINT_ACC1": "https://paper-api.alpaca.markets/v2",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC2": "PA3EFBL4P7BU",
            "ALPACA_ENDPOINT_ACC2": "https://paper-api.alpaca.markets/v2",
            "ALPACA_API_KEY_ACC3": "ramjet-key",
            "ALPACA_SECRET_KEY_ACC3": "ramjet-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC3": "PA3T0MQAGQ51",
            "ALPACA_ENDPOINT_ACC3": "https://paper-api.alpaca.markets/v2",
        },
    )

    assert credentials == ("rocket-key", "rocket-secret", True)


def test_resolve_alpaca_credentials_account_id_hint_beats_default_pair() -> None:
    credentials = resolve_alpaca_credentials(
        account_id="ACC-D9K2LL",
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_ID_ACC1": "acc-b7x4mm",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
            "ALPACA_ACCOUNT_ID_ACC2": "ACC-D9K2LL",
            "ALPACA_PAPER_ACC2": "true",
        },
    )

    assert credentials == ("rocket-key", "rocket-secret", True)


def test_resolve_alpaca_credentials_default_pair_used_only_when_no_hints() -> None:
    # Hints absent: default pair must still win over configured ACC{N} blocks.
    without_hints = resolve_alpaca_credentials(
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_PAPER": "true",
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
        },
    )
    assert without_hints == ("default-key", "default-secret", True)

    # No ACC{N} blocks at all: default pair remains the fallback.
    without_blocks = resolve_alpaca_credentials(
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_PAPER": "true",
        },
    )
    assert without_blocks == ("default-key", "default-secret", True)


def test_resolve_alpaca_credentials_ambiguous_hints_refuse_even_with_default_pair() -> None:
    """A hint that matches nothing must not silently fall back to either the
    default pair or blocks[0]."""
    credentials = resolve_alpaca_credentials(
        broker_login="PA3UNKNOWN",
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
        },
    )

    assert credentials is None
