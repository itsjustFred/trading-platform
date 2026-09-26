"""Tests for signal bot health check."""
from types import SimpleNamespace

import signal_bots.health_check as health_check
from signal_bots.health_check import check_signal_bots, main
from signal_bots.registry import SIGNAL_PROCESSOR_OPTIONS


def test_check_signal_bots_returns_registry_status() -> None:
    """Test signal bot health check."""
    result = check_signal_bots()

    assert "status" in result
    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) > 0
    assert all("name" in r for r in result["results"])
    assert all("status" in r for r in result["results"])
    assert all(r["probe"]["status"] == "ok" for r in result["results"])


def test_check_signal_bots_detects_config_files() -> None:
    """Test signal bot health check includes config paths."""
    result = check_signal_bots()

    assert len(result["results"]) == len(SIGNAL_PROCESSOR_OPTIONS)
    assert any(r["name"] == "nitro" for r in result["results"])
    assert any(r["name"] == "ramjet" for r in result["results"])
    assert any(r["name"].startswith("rocketfuel") for r in result["results"])
    assert not any(r["name"] == "stockkit" for r in result["results"])


def test_check_signal_bots_executes_registered_processor(monkeypatch) -> None:
    calls: list[str] = []

    class ProbeProcessor:
        def process_signal(self, raw_signal, market_data):
            calls.append(raw_signal["symbol"])
            assert not market_data.empty
            return SimpleNamespace(action="buy", symbol="AAPL")

    def factory(_config):
        return ProbeProcessor()

    monkeypatch.setitem(health_check.SIGNAL_PROCESSORS, "ramjet", factory)
    result = health_check.check_signal_bots()
    ramjet = next(item for item in result["results"] if item["name"] == "ramjet")

    assert calls == ["AAPL"]
    assert ramjet["probe"]["status"] == "ok"


def test_signal_bot_health_check_main_returns_json(capsys) -> None:
    """Test command-line entrypoint returns JSON status."""
    assert main() == 0

    captured = capsys.readouterr()
    assert '"status": "ok"' in captured.out
    assert "nitro" in captured.out
    assert "stockkit" not in captured.out
