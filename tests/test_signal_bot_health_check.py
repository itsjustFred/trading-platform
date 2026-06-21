"""Tests for signal bot health check."""
from signal_bots.health_check import main
from signal_bots.health_check import check_signal_bots


def test_check_signal_bots_returns_registry_status() -> None:
    """Test signal bot health check."""
    result = check_signal_bots()
    
    assert "status" in result
    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) > 0
    assert all("name" in r for r in result["results"])
    assert all("status" in r for r in result["results"])


def test_check_signal_bots_detects_config_files() -> None:
    """Test signal bot health check includes config paths."""
    result = check_signal_bots()
    
    assert len(result["results"]) >= 5  # stockkit, ramjet, rocketfuel variants, nitro
    assert any(r["name"] == "nitro" for r in result["results"])
    assert any(r["name"] == "ramjet" for r in result["results"])
    assert any(r["name"].startswith("rocketfuel") for r in result["results"])


def test_signal_bot_health_check_main_returns_json(capsys) -> None:
    """Test command-line entrypoint returns JSON status."""
    assert main() == 0

    captured = capsys.readouterr()
    assert '"status": "ok"' in captured.out
    assert "stockkit" in captured.out
    assert "nitro" in captured.out