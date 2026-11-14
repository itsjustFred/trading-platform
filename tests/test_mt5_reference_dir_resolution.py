"""Reference-directory resolution for the MT5 reference audit parser (#159).

The parser must not hard-code a host path. The reference directory comes from
``--reference-dir`` or the ``TRADING_PLATFORM_REFERENCES`` environment variable
(CLI wins). When neither yields an existing directory the parser fails with a
clear error instead of reading a machine-specific default.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_AUDIT_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
    / "audit"
    / "parse_mt5_reference.py"
)
_ENV_VAR = "TRADING_PLATFORM_REFERENCES"


def _load_parser():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "parse_mt5_reference_dir_under_test", _AUDIT_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PARSER = _load_parser()


@pytest.fixture(autouse=True)
def _no_ambient_reference_env(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)


def test_script_has_no_hardcoded_host_path():
    source = _AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "/home/ubuntu" not in source
    assert ".venv/bin/python" not in source


def test_missing_reference_dir_is_a_clear_error():
    with pytest.raises(ValueError, match="--reference-dir") as excinfo:
        _PARSER._resolve_reference_dir(None)

    assert _ENV_VAR in str(excinfo.value)


def test_nonexistent_reference_dir_is_rejected(tmp_path):
    missing = tmp_path / "not-there"

    with pytest.raises(ValueError, match="does not exist"):
        _PARSER._resolve_reference_dir(str(missing))


def test_reference_dir_must_be_a_directory_not_a_file(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="does not exist"):
        _PARSER._resolve_reference_dir(str(file_path))


def test_cli_argument_resolves_to_existing_directory(tmp_path):
    ref = tmp_path / "refs"
    ref.mkdir()

    assert _PARSER._resolve_reference_dir(str(ref)) == ref.resolve()


def test_env_var_is_used_when_cli_argument_is_absent(tmp_path, monkeypatch):
    ref = tmp_path / "refs"
    ref.mkdir()
    monkeypatch.setenv(_ENV_VAR, str(ref))

    assert _PARSER._resolve_reference_dir(None) == ref.resolve()


def test_cli_argument_takes_precedence_over_env_var(tmp_path, monkeypatch):
    env_ref = tmp_path / "env-refs"
    cli_ref = tmp_path / "cli-refs"
    env_ref.mkdir()
    cli_ref.mkdir()
    monkeypatch.setenv(_ENV_VAR, str(env_ref))

    assert _PARSER._resolve_reference_dir(str(cli_ref)) == cli_ref.resolve()


def test_main_exits_with_clear_message_when_reference_dir_missing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["parse_mt5_reference.py"])

    with pytest.raises(SystemExit) as excinfo:
        _PARSER.main()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--reference-dir" in err
    assert _ENV_VAR in err
