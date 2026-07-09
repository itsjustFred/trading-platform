from __future__ import annotations

import pytest

from shared import sentry as sentry_module
from shared.sentry import SentryConfig, SentryConfigError


def test_sentry_config_disables_without_dsn() -> None:
    config = SentryConfig.from_env({})

    assert config.enabled is False
    assert config.dsn == ""


def test_sentry_config_requires_dsn_when_enabled() -> None:
    with pytest.raises(SentryConfigError, match="SENTRY_DSN"):
        SentryConfig.from_env({"SENTRY_ENABLED": "true"})


def test_sentry_config_validates_sample_rates() -> None:
    with pytest.raises(SentryConfigError, match="SENTRY_SAMPLE_RATE"):
        SentryConfig.from_env(
            {
                "SENTRY_DSN": "https://public@example.com/1",
                "SENTRY_SAMPLE_RATE": "1.5",
            }
        )


def test_sentry_config_reads_enterprise_defaults() -> None:
    config = SentryConfig.from_env(
        {
            "SENTRY_DSN": "https://public@example.com/1",
            "SENTRY_ENVIRONMENT": "production",
            "SENTRY_TRACES_SAMPLE_RATE": "0.25",
            "SENTRY_PROFILES_SAMPLE_RATE": "0.05",
        }
    )

    assert config.enabled is True
    assert config.environment == "production"
    assert config.sample_rate == 1.0
    assert config.traces_sample_rate == 0.25
    assert config.profiles_sample_rate == 0.05
    assert config.send_default_pii is False
    assert config.enable_logs is True


def test_before_send_redacts_sensitive_payloads() -> None:
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret",
                "User-Agent": "pytest",
            }
        },
        "extra": {
            "api_key": "secret",
            "nested": [{"refresh-token": "secret"}, {"safe": "value"}],
            "message": (
                "Authorization: Bearer abc.def?token=leak "
                "https://example.test/callback?api_key=secret&safe=yes"
            ),
        },
    }

    redacted = sentry_module._before_send(event, {})  # noqa: SLF001

    assert redacted is not None
    assert redacted["request"]["headers"]["Authorization"] == "[Filtered]"
    assert redacted["request"]["headers"]["User-Agent"] == "pytest"
    assert redacted["extra"]["api_key"] == "[Filtered]"
    assert redacted["extra"]["nested"][0]["refresh-token"] == "[Filtered]"
    assert redacted["extra"]["nested"][1]["safe"] == "value"
    assert "Bearer [Filtered]" in redacted["extra"]["message"]
    assert "api_key=[Filtered]" in redacted["extra"]["message"]
    assert "safe=yes" in redacted["extra"]["message"]
    assert "secret" not in redacted["extra"]["message"]


def test_initialize_sentry_passes_validated_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_init(**kwargs: object) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(sentry_module.sentry_sdk, "init", fake_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", lambda *args, **kwargs: None)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_context", lambda *args, **kwargs: None)
    sentry_module._reset_for_tests()  # noqa: SLF001

    config = SentryConfig.from_env(
        {
            "SENTRY_DSN": "https://public@example.com/1",
            "SENTRY_ENVIRONMENT": "production",
            "SENTRY_ENABLE_LOGS": "true",
            "SENTRY_SEND_DEFAULT_PII": "false",
            "SENTRY_TRACES_SAMPLE_RATE": "0.1",
        }
    )

    assert sentry_module.initialize_sentry("tests", config=config) is True
    assert calls["dsn"] == "https://public@example.com/1"
    assert calls["environment"] == "production"
    assert calls["enable_logs"] is True
    assert calls["send_default_pii"] is False
    assert calls["traces_sample_rate"] == 0.1
    assert callable(calls["before_send"])


def test_capture_exception_uses_scoped_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class Scope:
        def set_tag(self, key: str, value: object) -> None:
            calls[f"tag:{key}"] = value

        def set_context(self, key: str, value: object) -> None:
            calls[f"context:{key}"] = value

    class ScopeManager:
        def __enter__(self) -> Scope:
            calls["entered"] = True
            return Scope()

        def __exit__(self, *args: object) -> None:
            calls["exited"] = True

    monkeypatch.setattr(sentry_module.sentry_sdk, "push_scope", lambda: ScopeManager())
    monkeypatch.setattr(sentry_module.sentry_sdk, "capture_exception", lambda exc: "event-id")

    event_id = sentry_module.capture_exception(
        RuntimeError("boom"),
        service_name="scheduler",
        context={"task_id": "abc"},
    )

    assert event_id == "event-id"
    assert calls["entered"] is True
    assert calls["exited"] is True
    assert calls["tag:service"] == "scheduler"
    assert calls["context:error_context"] == {"task_id": "abc"}
