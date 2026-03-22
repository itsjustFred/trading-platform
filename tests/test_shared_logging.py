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

