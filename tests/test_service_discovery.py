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


def test_duplicate_channel_across_two_tokens_is_reported():
    """Two tokens on one channel means every message is processed twice.

    The grouping is inferred from naming alone, so nothing caught this. Depending
    on which path wins, that is a duplicated signal and potentially a duplicated
    order - and the per-broker duplicate-signal recovery is the wrong layer to
    rely on for it.
    """
    discovered = [
        {"name": "ROCKETFUEL", "channel_id": "123"},
        {"name": "RAMJET", "channel_id": "123"},
        {"name": "NITRO", "channel_id": "999"},
    ]

    problems = service_discovery.validate_discord_channel_grouping(discovered)

    assert len(problems) == 1
    assert "123" in problems[0]
    assert "ROCKETFUEL" in problems[0] and "RAMJET" in problems[0]
    # And it says what the consequence is, not just that something is off.
    assert "twice" in problems[0] or "more than once" in problems[0]


def test_grouping_reports_services_and_deployments_that_disagree():
    """A service with no deployment ingests for nothing; a deployment with no
    service silently never receives signals. Both fail quietly by default."""
    discovered = [
        {"name": "ROCKETFUEL", "channel_id": "111"},
        {"name": "RAMJET", "channel_id": "222"},
    ]

    problems = service_discovery.validate_discord_channel_grouping(
        discovered, deployment_channels={"222", "333"}
    )

    joined = " ".join(problems)
    # 111 has a service but no deployment.
    assert any("111" in p and "no live deployment" in p for p in problems)
    # 333 has a deployment but no service.
    assert any("333" in p and "no Discord service" in p for p in problems)
    # 222 agrees on both sides, so it is not reported.
    assert "222" not in joined
    assert len(problems) == 2


def test_grouping_is_quiet_when_configuration_agrees():
    discovered = [
        {"name": "ROCKETFUEL", "channel_id": "111"},
        {"name": "RAMJET", "channel_id": "222"},
    ]
    assert (
        service_discovery.validate_discord_channel_grouping(
            discovered, deployment_channels={"111", "222"}
        )
        == []
    )
    # No deployment set supplied: nothing to cross-check, so nothing claimed.
    assert service_discovery.validate_discord_channel_grouping(discovered) == []


def test_grouping_ignores_incomplete_pairs():
    """A bot without a channel is skipped by discovery, so it cannot be a
    duplicate - and must not be reported as one."""
    discovered = [
        {"name": "ROCKETFUEL", "channel_id": "111"},
        {"name": "RAMJET", "channel_id": ""},
        {"name": "", "channel_id": "111"},
    ]
    assert service_discovery.validate_discord_channel_grouping(discovered) == []
