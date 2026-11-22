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

import pandas
import pytest

_AUDIT_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
    / "audit"
    / "parse_mt5_reference.py"
)
_ENV_VAR = "TRADING_PLATFORM_REFERENCES"

# One closed trade, in the exact shape the EA logs it (same recipe as
# tests/test_mt5_reference_open_validation.py): enough for parse_log_trades to
# pair one deal in/out and match its ``opened @`` line.
_ENTRY_TS = "2018.01.04 16:45:00"
_EXIT_TS = "2018.01.04 18:45:40"
_VOLUME = "1.66"
_ENTRY_PRICE = "1.20824"
_EXIT_PRICE = "1.20643"
_STOP = "1.20000"

# The XLSX half is a closed trade mirroring the log pair plus the report's own
# summary block, so main() can run its parity and summary checks end to end.
_XLSX_TRADE = {
    "entry_time": _ENTRY_TS,
    "exit_time": _EXIT_TS,
    "dir": "LONG",
    "entry": float(_ENTRY_PRICE),
    "exit": float(_EXIT_PRICE),
    "net": -300.46,
}
_XLSX_SUMMARY = {
    "net": "-300.46",
    "gross_profit": "0.00",
    "gross_loss": "-300.46",
    "profit_factor": "0.0000",
    "trade_count": "1",
    "won": "0 (0.00%)",
}


def _deal_line(ts: str, ticket: int, side: str, volume: str, price: str) -> str:
    return (
        f"CS\t0\t11:55:35.528\tTrades\t{ts}   deal #{ticket} {side} "
        f"{volume} EURUSD at {price} done (based on order #{ticket})"
    )


def _open_line(ts: str, side: str, price: str, lots: str) -> str:
    return (
        f"CS\t0\t11:55:35.528\tZenomTrader Discord IntradayVolatilityBreakout "
        f"(EURUSD,H1)\t{ts}   {side} opened @ {price} | Lots: {lots} | "
        f"SL: {_STOP} | Breakout"
    )


def _write_reference_log(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        "\n".join(
            [
                _deal_line(_ENTRY_TS, 2, "buy", _VOLUME, _ENTRY_PRICE),
                _deal_line(_EXIT_TS, 3, "sell", _VOLUME, _EXIT_PRICE),
                _open_line(_ENTRY_TS, "Long", _ENTRY_PRICE, "1.6600000000000001"),
            ]
        ).encode("utf-16")
    )
    return path


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
    """A path that exists but is a file gets its own message, not "missing"."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="is not a directory"):
        _PARSER._resolve_reference_dir(str(file_path))


def test_empty_env_var_is_rejected(monkeypatch):
    """A blanked env var (CI templates leave them empty) must not resolve to cwd."""
    monkeypatch.setenv(_ENV_VAR, "")

    with pytest.raises(ValueError, match="no MT5 reference directory"):
        _PARSER._resolve_reference_dir(None)


def test_empty_reference_dir_flag_does_not_fall_back_to_env_var(tmp_path, monkeypatch):
    """``--reference-dir ""`` must not be read as "flag absent" and silently
    resurrect the env var: an explicitly empty flag is a clear error, so the
    audit can never run against a directory the operator did not name."""
    env_ref = tmp_path / "env-refs"
    env_ref.mkdir()
    monkeypatch.setenv(_ENV_VAR, str(env_ref))

    with pytest.raises(ValueError, match="no MT5 reference directory"):
        _PARSER._resolve_reference_dir("")


def test_cli_argument_resolves_to_existing_directory(tmp_path):
    ref = tmp_path / "refs"
    ref.mkdir()

    assert _PARSER._resolve_reference_dir(str(ref)) == ref.resolve()


def test_relative_reference_dir_resolves_against_cwd(tmp_path, monkeypatch):
    """A relative ``--reference-dir`` must resolve to an absolute path.

    The log paths are built from the resolved directory, so a bare relative
    value must not survive as one (that would also let the same argument name
    two different directories depending on the caller's cwd).
    """
    monkeypatch.chdir(tmp_path)
    ref = tmp_path / "refs"
    ref.mkdir()

    assert _PARSER._resolve_reference_dir("refs") == ref.resolve()


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


def test_main_exits_with_clear_message_when_env_var_is_empty(monkeypatch, capsys):
    monkeypatch.setenv(_ENV_VAR, "")
    monkeypatch.setattr(sys, "argv", ["parse_mt5_reference.py"])

    with pytest.raises(SystemExit) as excinfo:
        _PARSER.main()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--reference-dir" in err
    assert _ENV_VAR in err


@pytest.mark.parametrize("via_env_var", [False, True])
def test_main_reads_logs_from_the_resolved_reference_dir(tmp_path, monkeypatch, via_env_var):
    """main() must parse the MT5 logs from the directory it resolved.

    Every other test stops at the resolver or its error path, so the
    ``ref / log_rel`` join at the single call site was never executed:
    dropping the ``ref /`` prefix, or pointing it at another base, kept the
    whole suite green while the audit read no logs at all (or the wrong ones).
    ``parse_log_trades`` therefore stays real - it is the code under test - and
    only the XLSX side of the audit is stubbed, mirroring the log pair so the
    parity and summary checks can run to completion.
    """
    ref = tmp_path / "references"
    for log_rel, _xlsx_name, _contract in _PARSER.JOBS.values():
        _write_reference_log(ref / log_rel)

    read_logs: list[pathlib.Path] = []
    read_xlsx: list[pathlib.Path] = []
    real_parse_log_trades = _PARSER.parse_log_trades

    def _recording_parse_log_trades(log_path, contract):  # type: ignore[no-untyped-def]
        read_logs.append(log_path)
        return real_parse_log_trades(log_path, contract)

    def _recording_parse_xlsx_trades(xlsx_path):  # type: ignore[no-untyped-def]
        read_xlsx.append(xlsx_path)
        return [dict(_XLSX_TRADE)]

    monkeypatch.setattr(_PARSER, "parse_log_trades", _recording_parse_log_trades)
    monkeypatch.setattr(_PARSER, "parse_xlsx_trades", _recording_parse_xlsx_trades)
    monkeypatch.setattr(_PARSER, "parse_xlsx_summary", lambda df: dict(_XLSX_SUMMARY))
    monkeypatch.setattr(pandas, "read_excel", lambda *args, **kwargs: None)
    # --out must resolve inside cwd or the strategy-comparison directory, so keep
    # the run inside tmp_path instead of writing into the checkout.
    monkeypatch.chdir(tmp_path)
    argv = ["parse_mt5_reference.py", "--out", "out"]
    if via_env_var:
        monkeypatch.setenv(_ENV_VAR, str(ref))
    else:
        argv += ["--reference-dir", str(ref)]
    monkeypatch.setattr(sys, "argv", argv)

    assert _PARSER.main() == 0

    assert read_logs == [ref / log_rel for log_rel, _name, _c in _PARSER.JOBS.values()]
    assert read_xlsx == [_PARSER.CMP / name for _rel, name, _c in _PARSER.JOBS.values()]
    assert ref / "mt5-eurusd-h1/eurusd_h1.log" in read_logs

    out = tmp_path / "out"
    for name in _PARSER.JOBS:
        assert (out / f"mt5_trades_{name}.csv").exists()
        assert (out / f"xlsx_trades_{name}.csv").exists()
    # the parsed CSV carries the fixture trade: the log was really read and
    # paired, not stubbed away.
    assert _ENTRY_TS in (out / "mt5_trades_eurusd.csv").read_text(encoding="utf-8")
