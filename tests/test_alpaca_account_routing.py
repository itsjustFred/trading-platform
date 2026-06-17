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
