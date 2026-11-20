#!/usr/bin/env python3
"""Convert an MT5 Strategy Tester ``.ini`` setfile into a tester ``.set``.

The reference setfiles (``strategy-comparisons/intraday_volatility_breakout/audit``)
are saved by the MT5 Strategy Tester UI: UTF-16LE with a BOM, a ``[Tester]``
section with run settings and a ``[TesterInputs]`` section whose values carry
optimisation ranges (``ATR_Period=5||5||1||50||N``).

``tester.ini`` reads ``ExpertParameters`` from ``MQL5\\Profiles\\Tester\\`` as a
plain set file: ``name=value`` lines, no sections, no optimisation ranges. This
script performs that conversion and writes ASCII without a BOM (a BOM makes the
terminal ignore the file).

Both paths are confined to the current directory unless ``--root`` is given
(repeatable), because the CI job writes into the portable terminal directory
outside the checkout.

Usage::

    python tools/mt5_ci/convert_setfile.py --input EURUSD_H1_setfile.ini \\
        --output MQL5/Profiles/Tester/eurusd_h1_ci.set
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

try:  # imported as a module (tests): the repo root is on sys.path
    from tools.mt5_ci import _path_guard
except ImportError:  # executed as a script: its own directory is sys.path[0]
    import _path_guard

INPUT_SECTION = "[TesterInputs]"
# Sections whose keys describe the run itself; tester.ini sets them explicitly
# for the CI run, so they must not be copied into the .set file.
DROPPED_SECTIONS = {"[Tester]"}


class SetfileError(ValueError):
    """Raised when the input does not look like a Strategy Tester setfile."""


def decode_setfile(raw: bytes) -> str:
    """Decode setfile bytes (UTF-16 with or without BOM, else UTF-8)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise SetfileError(f"setfile is neither UTF-16 nor UTF-8: {exc}") from exc


def _plain_value(raw_value: str) -> str:
    """Drop the ``||start||step||stop||flag`` optimisation tail."""
    return raw_value.split("||", 1)[0].strip()


def _parse_section(line: str, number: int) -> str:
    """Validate and return a ``[Section]`` header line."""
    if not line.endswith("]"):
        raise SetfileError(f"line {number}: malformed section header {line!r}")
    return line


def _parse_entry(line: str, number: int, section: str | None, seen: set[str]) -> str | None:
    """Return the ``name=value`` line for one entry, or None when it is dropped."""
    key, sep, value = line.partition("=")
    key = key.strip()
    if not sep or not key:
        raise SetfileError(f"line {number}: expected key=value, got {line!r}")
    if section in DROPPED_SECTIONS:
        return None
    if section != INPUT_SECTION:
        raise SetfileError(
            f"line {number}: {key!r} appears outside {INPUT_SECTION} (section={section!r})"
        )
    if key in seen:
        raise SetfileError(f"line {number}: duplicate input {key!r}")
    seen.add(key)
    return f"{key}={_plain_value(value)}"


def convert(text: str) -> list[str]:
    """Return the ``name=value`` lines (plus ``;`` comments) for a .set file."""
    lines: list[str] = []
    section: str | None = None
    saw_inputs = False
    seen: set[str] = set()

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("["):
            section = _parse_section(line, number)
            saw_inputs = saw_inputs or section == INPUT_SECTION
            continue
        if line.startswith(";"):
            if section not in DROPPED_SECTIONS:
                lines.append(line)
            continue

        entry = _parse_entry(line, number, section, seen)
        if entry is not None:
            lines.append(entry)

    if not saw_inputs:
        raise SetfileError(f"no {INPUT_SECTION} section found")
    if not seen:
        raise SetfileError(f"{INPUT_SECTION} contains no inputs")
    return lines


def write_setfile(lines: list[str], output: Path) -> None:
    """Write the .set file as ASCII without a BOM and with CRLF line endings."""
    payload = "\r\n".join(lines) + "\r\n"
    try:
        encoded = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SetfileError(f"non-ASCII character in converted setfile: {exc}") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)


def allowed_roots(raw_roots: Sequence[str]) -> tuple[Path, ...]:
    """Resolve the ``--root`` arguments, defaulting to the current directory."""
    return _path_guard.allowed_roots(raw_roots)


def resolve_cli_path(raw: Path, roots: Sequence[Path], what: str) -> Path:
    """Resolve a CLI path, refusing one outside the allowed roots.

    A crafted ``--input``/``--output`` (``..``, absolute path, symlink escape)
    must not read or write outside the roots the caller allows, so the value
    used at the file sink is this resolved path, never the raw argument.
    """
    return _path_guard.resolve_cli_path(raw, roots, what, error=SetfileError)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path, help="source setfile (.ini)")
    parser.add_argument("--output", required=True, type=Path, help="destination .set file")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="allowed root for --input/--output (repeatable, default: current directory)",
    )
    args = parser.parse_args(argv)

    try:
        roots = allowed_roots(args.root)
        source = resolve_cli_path(args.input, roots, "--input")
        destination = resolve_cli_path(args.output, roots, "--output")
        text = decode_setfile(source.read_bytes())
        lines = convert(text)
        write_setfile(lines, destination)
    except (OSError, SetfileError) as exc:
        print(f"convert_setfile: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {destination} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
