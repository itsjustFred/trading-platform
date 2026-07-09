from __future__ import annotations

import pytest

from runner.strategy_ir.normalizer import normalize_strategy_meta_payload


def test_normalize_strategy_meta_uses_markdown_title_when_name_missing():
    normalized = normalize_strategy_meta_payload(
        {"parameters": {"fast": 10}},
        requested_name=None,
        markdown_doc="# Momentum Bot\n\n## Entry\n- test",
    )

    assert normalized["name"] == "Momentum Bot"
    assert normalized["version"] == "1.0.0"
    assert normalized["parameters"] == {"fast": 10}
    assert normalized["tags"] == {}


def test_normalize_strategy_meta_parses_key_value_strings_for_parameters_and_tags():
    normalized = normalize_strategy_meta_payload(
        {
            "name": "KV Bot",
            "parameters": "fast=10, slow=30",
            "tags": "source=unit, type=trend",
        },
        requested_name=None,
        markdown_doc="# Fallback",
    )

    assert normalized["parameters"] == {"fast": 10, "slow": 30}
    assert normalized["tags"] == {"source": "unit", "type": "trend"}


def test_normalize_strategy_meta_merges_mixed_tag_list_entries():
    normalized = normalize_strategy_meta_payload(
        {
            "name": "Tag Bot",
            "parameters": {"fast": 10},
            "tags": ["momentum", {"source": "tv"}, {"labels": ["fx"]}],
        },
        requested_name=None,
        markdown_doc="# Fallback",
    )

    assert normalized["tags"]["source"] == "tv"
    assert normalized["tags"]["labels"] == ["fx", "momentum"]


def test_normalize_strategy_meta_rejects_invalid_parameter_list_entries():
    with pytest.raises(TypeError, match=r"STRATEGY_META\['parameters'\]"):
        normalize_strategy_meta_payload(
            {
                "name": "Bad Params",
                "parameters": [123],
                "tags": {},
            },
            requested_name=None,
            markdown_doc="# Fallback",
        )
