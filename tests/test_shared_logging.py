from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from shared.logging import JsonFormatter


def test_json_formatter_uses_required_timestamp_format():
    formatter = JsonFormatter("viewer")
    fixed_time = datetime(2026, 3, 10, 8, 9, 10, tzinfo=timezone.utc)
    record = logging.LogRecord(
        name="trading.viewer",
        level=logging.INFO,
        pathname=__file__,
        lineno=17,
        msg="viewer-start",
        args=(),
        exc_info=None,
    )
    record.created = fixed_time.timestamp()

    rendered = formatter.format(record)
    payload = json.loads(rendered)

    assert payload["timestamp"] == "2026-03-10 08:09:10"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", payload["timestamp"])


def test_json_formatter_includes_required_fields_and_defaults():
    formatter = JsonFormatter("viewer")
    record = logging.LogRecord(
        name="trading.viewer",
        level=logging.ERROR,
        pathname=__file__,
        lineno=42,
        msg="boom",
        args=(),
        exc_info=None,
    )
    record.module = ""
    record.funcName = ""
    record.lineno = "invalid"  # type: ignore[assignment]

    payload = json.loads(formatter.format(record))
    assert payload["level"] == "ERROR"
    assert payload["message"] == "boom"
    assert payload["module"] == "trading.viewer"
    assert payload["function"] == "<unknown>"
    assert payload["line"] == 0


def test_redact_value_handles_nested_sensitive_data_and_exception_text():
    from shared.logging import redact_value

    value = {
        "Authorization": "Bearer super-secret",
        "items": [{"api_key": "key-secret", "value": "ordinary"}],
    }
    error = RuntimeError("token=exception-secret")

    redacted = redact_value({"value": value, "error": error})

    assert redacted["value"]["Authorization"] == "[Filtered]"
    assert redacted["value"]["items"][0]["api_key"] == "[Filtered]"
    assert "exception-secret" not in redacted["error"]


def test_redact_value_keeps_unknown_object_text_and_masks_unstringifiable_objects():
    from shared.logging import redact_text, redact_value

    class _Unknown:
        def __str__(self):
            return "broker api_key=unknown-key-secret status=ok"

    class _Unserializable:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    redacted = redact_value(
        {
            "opaque": "opaque-secret",
            "unknown": _Unknown(),
            "broken": _Unserializable(),
        }
    )

    assert redacted["opaque"] == "[Filtered]"
    assert redacted["unknown"] == "broker api_key=[Filtered] status=ok"
    assert redacted["broken"] == "[Unserializable]"
    assert redact_text(_Unserializable()) == "[Unserializable]"


def test_redact_value_keeps_nested_unknown_object_text_inside_containers():
    from shared.logging import redact_value

    class _Order:
        def __str__(self):
            return "order-42 token=token-secret"

    redacted = redact_value({"orders": (_Order(),), "primary": _Order()})

    assert redacted["orders"] == ("order-42 token=[Filtered]",)
    assert redacted["primary"] == "order-42 token=[Filtered]"


def test_redact_value_handles_camel_case_sensitive_keys():
    from shared.logging import redact_value

    redacted = redact_value({"apiKey": "key-secret", "refreshToken": "token-secret"})

    assert redacted == {"apiKey": "[Filtered]", "refreshToken": "[Filtered]"}


def test_redact_value_filters_sensitive_keys_with_innocuous_values():
    from shared.logging import redact_value

    redacted = redact_value(
        {
            "Authorization": "opaque-value",
            "apiKey": "opaque-value",
            "password": "opaque-value",
        }
    )

    assert redacted == {
        "Authorization": "[Filtered]",
        "apiKey": "[Filtered]",
        "password": "[Filtered]",
    }


def test_redact_text_handles_secret_key_and_authorization_values():
    from shared.logging import redact_text

    cases = {
        "secret_key=secret-key-value": "secret-key-value",
        "app_secret: app-secret-value": "app-secret-value",
        "secretKey=secret-camel-value": "secret-camel-value",
        "Authorization: Basic basic-auth-value": "basic-auth-value",
        "Authorization: Token token-auth-value": "token-auth-value",
        "Authorization: Digest username=digest-user, response=digest-response": "digest-user",
    }

    for raw, secret in cases.items():
        redacted = redact_text(raw)
        assert secret not in redacted
    assert "Basic [Filtered]" in redact_text("Authorization: Basic basic-auth-value")
    assert "Token [Filtered]" in redact_text("Authorization: Token token-auth-value")
    digest_redacted = redact_text(
        "Authorization: Digest username=digest-user, response=digest-response"
    )
    assert "digest-user" not in digest_redacted
    assert "digest-response" not in digest_redacted
