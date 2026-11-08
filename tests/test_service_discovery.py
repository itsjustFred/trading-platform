from live_runner import service_discovery


def test_discover_discord_bots_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".accounts.env"
    env_file.write_text(
        "\n".join(
            [
                "DISCORD_BOT_RAMJET_TOKEN=token-one",
                "DISCORD_BOT_RAMJET_CHANNEL_ID=123456789",
                "DISCORD_BOT_ROCKETFUEL_TOKEN=token-two",
                "DISCORD_BOT_ROCKETFUEL_CHANNEL_ID=987654321",
                "DISCORD_BOT_NITRO_TOKEN=token-three",
                "DISCORD_BOT_NITRO_CHANNEL_ID=555666777",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(service_discovery, "DEFAULT_ACCOUNTS_ENV", env_file)

    bots = service_discovery.discover_discord_bots_from_env()
    by_name = {item["name"]: item for item in bots}

    assert "RAMJET" in by_name
    assert by_name["RAMJET"]["token_env_key"] == "DISCORD_BOT_RAMJET_TOKEN"
    assert by_name["RAMJET"]["channel_id"] == "123456789"

    assert "ROCKETFUEL" in by_name
    assert by_name["ROCKETFUEL"]["token_env_key"] == "DISCORD_BOT_ROCKETFUEL_TOKEN"
    assert by_name["ROCKETFUEL"]["channel_id"] == "987654321"

    assert "NITRO" in by_name
    assert by_name["NITRO"]["token_env_key"] == "DISCORD_BOT_NITRO_TOKEN"
    assert by_name["NITRO"]["channel_id"] == "555666777"


def test_a_token_without_a_channel_is_not_discovered(tmp_path, monkeypatch):
    """A half-configured bot must not be reported as configured.

    Discovery is what the author-discovery tool and the daemon use to decide
    which bots exist. A bot with a token but no channel id can never read
    anything, so including it would replace "no bots configured" with a
    per-channel 401 and hide the real configuration error.
    """
    env_file = tmp_path / ".accounts.env"
    env_file.write_text(
        "\n".join(
            [
                "DISCORD_BOT_RAMJET_TOKEN=token-one",
                "DISCORD_BOT_NITRO_TOKEN=token-two",
                "DISCORD_BOT_NITRO_CHANNEL_ID=555666777",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service_discovery, "DEFAULT_ACCOUNTS_ENV", env_file)

    bots = service_discovery.discover_discord_bots_from_env()

    assert [item["name"] for item in bots] == ["NITRO"]
