import pytest

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


def test_resolve_alpaca_credentials_multiple_hintless_blocks_refuse_blocks0() -> None:
    """With two or more credential blocks and no hints anywhere there is no unique
    mapping from an account to a block. The account must resolve to None instead of
    silently routing through ACC1's keys."""
    credentials = resolve_alpaca_credentials(
        account_id="ACC-UNMAPPED",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_PAPER_ACC1": "true",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
            "ALPACA_PAPER_ACC2": "true",
        },
    )

    assert credentials is None

    # Same refusal when only the (hint-less) broker login is supplied.
    credentials = resolve_alpaca_credentials(
        broker_login="PA3EFBL4P7BU",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
        },
    )

    assert credentials is None


def test_resolve_alpaca_credentials_hintless_default_pair_and_block_still_unique() -> None:
    """Legacy single-block setup without any hints: the default pair remains the
    unique mapping (blocks[0] and the default pair are two candidate mappings, so
    the documented precedence — default pair first, block only when no default pair
    exists — still applies rather than refusing)."""
    with_default = resolve_alpaca_credentials(
        env={
            "ALPACA_API_KEY": "default-key",
            "ALPACA_SECRET_KEY": "default-secret",
            "ALPACA_PAPER": "true",
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
        },
    )
    assert with_default == ("default-key", "default-secret", True)

    # No default pair configured: the single block is the only candidate mapping.
    without_default = resolve_alpaca_credentials(
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
        },
    )
    assert without_default == ("nitro-key", "nitro-secret", True)

    # Same legacy mapping for an account that carries no identity at all: still
    # resolves, but must log so the unmapped-credential path stays visible.
    unresolved = resolve_alpaca_credentials(
        account_id="ACC-UNMAPPED",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
        },
    )
    assert unresolved == ("nitro-key", "nitro-secret", True)


def test_resolve_alpaca_credentials_unidentified_single_block_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account that identifies against none of the blocks but resolves through
    the legacy single-block fallback must leave a warning naming no secret."""
    import live_runner.alpaca_adapter as adapter_module

    messages: list[str] = []

    def _capture_warning(message: str, extra: dict | None = None) -> None:
        messages.append(message)

    monkeypatch.setattr(adapter_module.logger, "warning", _capture_warning)
    credentials = resolve_alpaca_credentials(
        account_id="ACC-UNMAPPED",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
        },
    )

    assert credentials == ("nitro-key", "nitro-secret", True)
    assert any(
        "Unidentified account using the only configured Alpaca credential block" in message
        for message in messages
    )


def test_resolve_alpaca_credentials_hintless_logs_warning_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing an ambiguous hint-less config must log a warning naming no secret."""
    import live_runner.alpaca_adapter as adapter_module

    messages: list[str] = []

    def _capture_warning(message: str, extra: dict | None = None) -> None:
        messages.append(message)

    monkeypatch.setattr(adapter_module.logger, "warning", _capture_warning)
    credentials = resolve_alpaca_credentials(
        account_id="ACC-UNMAPPED",
        env={
            "ALPACA_API_KEY_ACC1": "nitro-key",
            "ALPACA_SECRET_KEY_ACC1": "nitro-secret",
            "ALPACA_API_KEY_ACC2": "rocket-key",
            "ALPACA_SECRET_KEY_ACC2": "rocket-secret",
        },
    )

    assert credentials is None
    assert any("ambiguous" in message.lower() for message in messages)
    flat = " ".join(messages)
    assert "nitro-key" not in flat
    assert "rocket-secret" not in flat


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


def test_alpaca_credential_blocks_strict_parse_preserves_sibling_blocks() -> None:
    """The strict block parser must still reject a typo'd ALPACA_PAPER_ACC{N}
    (fail-closed, shared with discovery), but only that block: incomplete
    key/secret pairs are skipped, complete ones are kept."""
    from live_runner.alpaca_adapter import _alpaca_acc_indexes, _alpaca_credential_blocks

    env = {
        "ALPACA_API_KEY_ACC1": "k1",
        "ALPACA_SECRET_KEY_ACC1": "s1",
        # ACC2 has no secret: no block.
        "ALPACA_API_KEY_ACC2": "k2",
        "ALPACA_API_KEY_ACC3": "k3",
        "ALPACA_SECRET_KEY_ACC3": "s3",
        "ALPACA_ENDPOINT_ACC3": "https://paper-api.alpaca.markets/v2",
        "ALPACA_PAPER_ACC4": "tru",
    }

    indexes = _alpaca_acc_indexes(env)
    # ACC4 appears in the index via its paper key but has no key/secret pair:
    # the strict parse must not even be reached for it (skipped earlier).
    assert _alpaca_credential_blocks(env, indexes) == [
        {
            "source_index": "ACC1",
            "api_key": "k1",
            "secret_key": "s1",
            "paper": True,
            "account_id_hint": "",
            "login_hint": "",
        },
        {
            "source_index": "ACC3",
            "api_key": "k3",
            "secret_key": "s3",
            "paper": True,
            "account_id_hint": "",
            "login_hint": "",
        },
    ]

    complete_env = dict(env, ALPACA_API_KEY_ACC4="k4", ALPACA_SECRET_KEY_ACC4="s4")
    acc_indexes = _alpaca_acc_indexes(complete_env)
    with pytest.raises(ValueError, match="ALPACA_PAPER_ACC4"):
        _alpaca_credential_blocks(complete_env, acc_indexes)
