"""Normalization helpers for LLM-provided STRATEGY_META payloads."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


def _extract_markdown_title(markdown_doc: str) -> str | None:
    match = re.search(r"^\s*#\s+(.+)$", markdown_doc or "", flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _coerce_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None

    if raw[0] == raw[-1] and raw[0] in {"'", '"'} and len(raw) >= 2:
        return raw[1:-1]

    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _parse_mapping_string(value: str) -> Any | None:
    text = value.strip()
    if not text:
        return None

    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
            if isinstance(parsed, (dict, list, tuple)):
                return parsed
        except (ValueError, SyntaxError, TypeError):
            pass

    parts = [part.strip() for part in text.split(",") if part.strip()]
    if parts and all("=" in part for part in parts):
        mapped: dict[str, Any] = {}
        for part in parts:
            key, raw = part.split("=", 1)
            key_name = key.strip()
            if not key_name:
                continue
            mapped[key_name] = _coerce_scalar(raw)
        if mapped:
            return mapped

    return None


def _coerce_str_dict(value: dict) -> dict[str, Any]:
    """Return a dict with non-empty string keys, skipping blank keys."""
    coerced: dict[str, Any] = {}
    for key, item_value in value.items():
        name = str(key).strip()
        if not name:
            continue
        coerced[name] = item_value
    return coerced


def _coerce_parameters(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, dict):
        return _coerce_str_dict(value)

    if isinstance(value, str):
        parsed = _parse_mapping_string(value)
        if parsed is not None:
            return _coerce_parameters(parsed)
        raise TypeError(
            "STRATEGY_META['parameters'] string must contain a JSON/Python dict or key=value pairs."
        )

    if isinstance(value, (list, tuple)):
        coerced: dict[str, Any] = {}
        for item in value:
            coerced.update(_coerce_parameter_entry(item))
        return coerced

    raise TypeError(
        "STRATEGY_META['parameters'] must be a dict, a supported list, or a mapping string."
    )


def _coerce_parameter_entry(item: Any) -> dict[str, Any]:
    """Coerce a single list entry of ``parameters`` into one named parameter."""
    if not isinstance(item, dict):
        raise TypeError("STRATEGY_META['parameters'] list entries must be dict values.")

    if "name" in item:
        name = str(item.get("name", "")).strip()
        if not name:
            raise TypeError(
                "STRATEGY_META['parameters'] list entries with 'name' must provide a non-empty value."
            )
        return {name: _parameter_default(item)}

    if len(item) == 1:
        key, parameter_value = next(iter(item.items()))
        name = str(key).strip()
        if not name:
            raise TypeError(
                "STRATEGY_META['parameters'] single-entry dict keys must be non-empty."
            )
        return {name: parameter_value}

    raise TypeError(
        "STRATEGY_META['parameters'] list entries must contain 'name' or a single key-value pair."
    )


def _parameter_default(item: dict) -> Any:
    """Resolve the parameter default, falling back to value/initial/start keys."""
    parameter_value = item.get("default")
    if "default" not in item:
        for fallback_key in ("value", "initial", "start"):
            if fallback_key in item:
                parameter_value = item[fallback_key]
                break
    return parameter_value


def _coerce_tags(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, dict):
        return _coerce_str_dict(value)

    if isinstance(value, str):
        parsed = _parse_mapping_string(value)
        if parsed is not None:
            return _coerce_tags(parsed)

        label = value.strip()
        if not label:
            return {}
        return {"labels": [label]}

    if isinstance(value, (list, tuple)):
        return _coerce_tag_list(value)

    raise TypeError(
        "STRATEGY_META['tags'] must be a dict, a supported list, or a mapping string."
    )


def _coerce_tag_list(value: Any) -> dict[str, Any]:
    """Coerce a list/tuple of tag entries into a merged tag mapping."""
    labels: list[str] = []
    merged: dict[str, Any] = {}

    for item in value:
        if isinstance(item, str):
            label = item.strip()
            if label:
                labels.append(label)
            continue
        if isinstance(item, dict):
            merged.update(_coerce_str_dict(item))
            continue
        raise TypeError(
            "STRATEGY_META['tags'] list entries must be strings or dict values."
        )

    if labels:
        merged["labels"] = _merge_labels(merged.get("labels"), labels)

    return merged


def _merge_labels(existing: Any, labels: list[str]) -> list[Any]:
    """Append collected labels to any pre-existing 'labels' tag value."""
    if isinstance(existing, list):
        return [*existing, *labels]
    if existing is None:
        return labels
    return [existing, *labels]


def normalize_strategy_meta_payload(
    meta: dict[str, Any],
    *,
    requested_name: str | None,
    markdown_doc: str,
) -> dict[str, Any]:
    """Normalize raw STRATEGY_META payload into the repository schema."""
    if not isinstance(meta, dict):
        raise TypeError("STRATEGY_META must be a dictionary.")

    normalized = dict(meta)

    resolved_title = (
        _extract_markdown_title(markdown_doc)
        or requested_name
        or normalized.get("name")
        or "Auto Strategy"
    )
    normalized["name"] = str(normalized.get("name") or resolved_title).strip() or str(resolved_title)
    normalized["version"] = str(normalized.get("version") or "1.0.0")

    normalized["parameters"] = _coerce_parameters(normalized.get("parameters"))
    normalized["tags"] = _coerce_tags(normalized.get("tags"))

    return normalized
