"""Tests for tools/mt5_ci/convert_setfile.py.

The converter is the only place where the reference setfile (UTF-16LE + BOM,
optimisation ranges) is turned into the plain .set file the CI tester run
loads from MQL5\\Profiles\\Tester. A wrong encoding or a leftover ``||range``
tail makes the tester silently ignore an input, so each of those properties is
pinned here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "mt5_ci" / "convert_setfile.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mt5_ci_convert_setfile", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SETFILE_TEXT = """[Tester]
Expert=ZenomTrader Discord IntradayVolatilityBreakout.ex5
Symbol=EURUSD
Period=H1
Model=1
FromDate=2018.01.01
ToDate=2026.06.23
Deposit=100000
Leverage=100
Optimization=0
Visual=0
[TesterInputs]
; Breakout Parameters
ATR_Period=5||5||1||50||N
ATR_Multiplier=0.25||0.25||0.025000||2.500000||N
Use_Candle_Close=true||false||0||true||N
; Trading Hours
Entry_Time=16:30
Magic_Number=123456||123456||1||1234560||N
"""


def _write_setfile(path: Path, text: str = SETFILE_TEXT) -> Path:
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    return path


def test_converts_reference_setfile_to_plain_values(tmp_path: Path) -> None:
    module = _load_module()
    source = _write_setfile(tmp_path / "EURUSD_H1_setfile.ini")

    lines = module.convert(module.decode_setfile(source.read_bytes()))

    assert lines == [
        "; Breakout Parameters",
        "ATR_Period=5",
        "ATR_Multiplier=0.25",
        "Use_Candle_Close=true",
        "; Trading Hours",
        "Entry_Time=16:30",
        "Magic_Number=123456",
    ]
    # The [Tester] run settings are owned by tester.ini in CI; copying them
    # into the .set file would silently override the workflow's window.
    assert not any(line.startswith(("Expert=", "Symbol=", "FromDate=")) for line in lines)


def test_output_is_ascii_without_bom_and_crlf(tmp_path: Path) -> None:
    module = _load_module()
    source = _write_setfile(tmp_path / "EURUSD_H1_setfile.ini")
    output = tmp_path / "out" / "eurusd_h1_ci.set"

    lines = module.convert(module.decode_setfile(source.read_bytes()))
    module.write_setfile(lines, output)

    raw = output.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a UTF-8 BOM makes MT5 ignore the .set file"
    assert not raw.startswith(b"\xff\xfe"), "a UTF-16 BOM makes MT5 ignore the .set file"
    raw.decode("ascii")  # raises if anything non-ASCII slipped through
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_reads_utf8_setfile_too(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "utf8.ini"
    source.write_text(SETFILE_TEXT, encoding="utf-8")

    lines = module.convert(module.decode_setfile(source.read_bytes()))
    assert "ATR_Period=5" in lines


def test_cli_returns_2_on_missing_inputs_section(tmp_path: Path, capsys) -> None:
    module = _load_module()
    source = _write_setfile(tmp_path / "bad.ini", "[Tester]\nExpert=x.ex5\n")
    output = tmp_path / "out.set"

    code = module.main(["--input", str(source), "--output", str(output)])

    assert code == 2
    assert "TesterInputs" in capsys.readouterr().err
    assert not output.exists()


def test_rejects_key_outside_inputs_section(tmp_path: Path) -> None:
    module = _load_module()
    text = "[TesterInputs]\nATR_Period=5\n[Extra]\nFoo=1\n"

    with pytest.raises(module.SetfileError, match="outside"):
        module.convert(text)


def test_rejects_duplicate_input(tmp_path: Path) -> None:
    module = _load_module()
    text = "[TesterInputs]\nATR_Period=5\nATR_Period=6\n"

    with pytest.raises(module.SetfileError, match="duplicate"):
        module.convert(text)


def test_rejects_non_ascii_output(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(module.SetfileError, match="non-ASCII"):
        module.write_setfile(["Entry_Time=16:30\u00a0"], tmp_path / "out.set")


def test_cli_writes_file(tmp_path: Path, capsys) -> None:
    module = _load_module()
    source = _write_setfile(tmp_path / "EURUSD_H1_setfile.ini")
    output = tmp_path / "nested" / "eurusd_h1_ci.set"

    code = module.main(["--input", str(source), "--output", str(output)])

    assert code == 0
    assert output.is_file()
    assert "ATR_Period=5" in output.read_text(encoding="ascii")
    assert "wrote" in capsys.readouterr().out
