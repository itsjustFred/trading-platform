"""The matrix overview artifact's ``generated_at`` must be a parseable UTC stamp.

The generator used to stamp ``datetime.utcnow()`` (deprecated, and a naive
time). The rewrite keeps the wall clock UTC and appends a single ``Z``; pin that
the stamp stays a single-offset ISO-8601 instant a consumer can parse, rather
than drifting into ``+00:00Z`` (two offsets, unparseable).
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_matrix_grid_overview.py"
EMPTY_REPORT = json.dumps({"strategies": [], "started_at": None, "updated_at": None})


@contextmanager
def _stubbed_paths() -> Iterator[None]:
    """Stub the generator's fixed machine paths for an import.

    The script reads the discovery report and writes the overview artifact at
    absolute, machine-specific paths (it predates tmp_path discipline), so the
    load replaces file IO with an empty report and a captured artifact.
    """

    def _fake_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "w" in mode:
            return _Capture()
        return io.StringIO(EMPTY_REPORT)

    with (
        patch.object(Path, "exists", lambda self: False),
        patch.object(Path, "mkdir", lambda self, *a, **kw: None),
        patch.object(Path, "open", _fake_open),
    ):
        yield


class _Capture(io.StringIO):
    """A StringIO that stays readable after the script's `with` block closes it."""

    def close(self) -> None:  # pragma: no cover - trivial override
        pass


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("matrix_grid_overview_under_test", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with _stubbed_paths():
        spec.loader.exec_module(module)
    return module


def test_generated_at_is_a_parseable_utc_z_timestamp() -> None:
    """The artifact's stamp parses as ISO-8601 with exactly one, terminal, Z."""
    module = _load_generator()
    stamp = module.overview["generated_at"]

    assert stamp.endswith("Z")
    assert stamp.count("Z") == 1
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_generated_at_has_no_double_offset() -> None:
    """The stamp carries no second offset before the Z (the tzinfo=None fix)."""
    module = _load_generator()
    stamp = module.overview["generated_at"]

    body = stamp[:-1]
    assert "+" not in body
    assert not body.endswith("-00:00")


def test_the_written_artifact_carries_the_same_stamp() -> None:
    """What lands in the artifact file is the stamped overview itself."""
    spec = importlib.util.spec_from_file_location("matrix_grid_overview_written", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    written = _Capture()

    def _fake_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        return written if "w" in mode else io.StringIO(EMPTY_REPORT)

    with (
        patch.object(Path, "exists", lambda self: False),
        patch.object(Path, "mkdir", lambda self, *a, **kw: None),
        patch.object(Path, "open", _fake_open),
    ):
        spec.loader.exec_module(module)

    payload = json.loads(written.getvalue())
    assert payload["generated_at"] == module.overview["generated_at"]
    # Only the two-offset form ("+00:00Z") fails this parse. `fromisoformat`
    # has read a terminal "Z" as UTC since 3.11, so the retired naive
    # `utcnow().isoformat() + "Z"` produced the very same string and is NOT
    # rejected here; the clock that stamp came from is pinned by
    # `test_the_stamp_comes_from_utc_not_the_machine_clock` below.
    datetime.fromisoformat(payload["generated_at"])


def test_the_stamp_comes_from_utc_not_the_machine_clock() -> None:
    """The wall clock behind ``generated_at`` is UTC, not the machine's.

    The retired form (``datetime.utcnow().isoformat() + "Z"``) yields the same
    string the aware form yields on a UTC machine, and every assertion about the
    stamp itself therefore accepts both - which is why this pins the clock the
    generator asks for: ``datetime.now`` must be handed UTC, never the local
    zone and never nothing. A naive ``utcnow()`` never calls ``now`` at all, and
    a local ``datetime.now()`` calls it with no zone, so neither passes.
    """
    seen: list[object] = []

    class _RecordingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            seen.append(tz)
            return datetime(2026, 11, 20, 22, 13, 5, tzinfo=tz)

    spec = importlib.util.spec_from_file_location("matrix_grid_overview_utc_clock", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with _stubbed_paths(), patch("datetime.datetime", _RecordingDatetime):
        spec.loader.exec_module(module)

    assert seen == [timezone.utc]
    assert module.overview["generated_at"] == "2026-11-20T22:13:05Z"
