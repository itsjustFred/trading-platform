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
