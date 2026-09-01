"""Helper-namespace containment tests for the PyneCore adapter (F26).

Seam under test: ``runner.engines.pynecore_adapter._compile_helper_namespace``.
The runner process must never ``exec`` arbitrary imports from generated or
user-supplied helper code; only whitelisted data-science imports are allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runner.engines.pynecore_adapter import _compile_helper_namespace


def test_helper_allows_pandas_import():
    source = "import pandas as pd\n\n\ndef build_trade_context(df, params):\n    return {}\n"
    namespace = _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)
    assert callable(namespace["build_trade_context"])


def test_helper_rejects_subprocess_import():
    source = "import subprocess\n\n\ndef build_trade_context(df, params):\n    return {}\n"
    with pytest.raises(RuntimeError, match="not allowed in strategy helper code"):
        _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)


def test_helper_rejects_socket_and_urllib_imports():
    for source in (
        "import socket\n",
        "from urllib.request import urlopen\n",
        "import requests\n",
        "from pathlib import Path\n",
    ):
        with pytest.raises(RuntimeError, match="not allowed in strategy helper code"):
            _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)


def test_helper_rejects_eval_and_open_calls():
    for source in (
        "def build_trade_context(df, params):\n    return {'x': eval('1+1')}\n",
        "def build_trade_context(df, params):\n    return {'x': open('/etc/passwd').read()}\n",
    ):
        with pytest.raises(RuntimeError, match="not allowed in strategy helper code"):
            _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)


def test_helper_rejects_dunder_traversal():
    source = (
        "def build_trade_context(df, params):\n"
        "    return {'x': df.__class__.__mro__[1].__subclasses__()}\n"
    )
    with pytest.raises(RuntimeError, match="not allowed in strategy helper code"):
        _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)


def test_helper_rejects_unknown_top_level_statements():
    source = "x = open('/etc/passwd')\n\n\ndef build_trade_context(df, params):\n    return {}\n"
    with pytest.raises(RuntimeError, match="Only imports and helper functions"):
        _compile_helper_namespace(Path('demo_context.py'), source, strict_structure=True)
