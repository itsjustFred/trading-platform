"""Trust-boundary tests for LLM-generated strategy code (Cleanup 16-C).

Seams under test:
- ``runner.codegen_safety.screen_strategy_source``: static screening of
  generated strategy code before it is ever executed.
- ``runner.auto_agent.run_auto_agent``: the human-review gate that keeps
  generated code registry-inert until a hash-bound approval exists, plus the
  failure cleanup that removes the last generated file when validation fails.
"""

from __future__ import annotations

import pytest

from runner.codegen_safety import screen_strategy_source


def test_screening_rejects_subprocess_import():
    result = screen_strategy_source("import subprocess\n\nx = 1\n")
    assert not result.allowed
    assert result.findings
    assert any("subprocess" in finding.message for finding in result.findings)


def test_screening_rejects_os_system_import():
    result = screen_strategy_source("import os\n\nos.system('rm -rf /')\n")
    assert not result.allowed


def test_screening_rejects_ftplib_import():
    result = screen_strategy_source("from ftplib import FTP\n")
    assert not result.allowed


def test_screening_rejects_dynamic_eval_and_exec():
    result = screen_strategy_source("value = eval('1 + 1')\n")
    assert not result.allowed
    result = screen_strategy_source("exec('import os')\n")
    assert not result.allowed


def test_screening_rejects_attribute_escape_hatches():
    for source in (
        "import builtins\n\nbuiltins.exec('x')\n",
        "value = globals()['os']\n",
        "import pandas\n\npandas.read_pickle('snapshot.pkl')\n",
        "import base64\n\nbase64.b64decode('aGF4')\n",
    ):
        assert not screen_strategy_source(source).allowed, source


def test_screening_rejects_syntax_errors():
    result = screen_strategy_source("def broken(:\n    pass\n")
    assert not result.allowed
    assert any("syntax" in finding.message.lower() for finding in result.findings)


def test_screening_allows_normal_strategy_code():
    source = '''"""Generated strategy."""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast = df["close"].rolling(params.get("fast", 10)).mean()
    slow = df["close"].rolling(params.get("slow", 30)).mean()
    entries = (fast > slow).fillna(False)
    exits = (fast < slow).fillna(False)
    indicators_dict = {"fast_sma": fast, "slow_sma": slow}
    return entries, exits, indicators_dict
'''
    result = screen_strategy_source(source)
    assert result.allowed, [finding.message for finding in result.findings]
