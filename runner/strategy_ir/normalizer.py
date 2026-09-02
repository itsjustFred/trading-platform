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


def _coerce_parameters(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, dict):
        coerced: dict[str, Any] = {}
        for key, param_value in value.items():
            name = str(key).strip()
            if not name:
                continue
            coerced[name] = param_value
        return coerced

    if isinstance(value, str):
        parsed = _parse_mapping_string(value)
        if parsed is not None:
            return _coerce_parameters(parsed)
        raise TypeError(
            "STRATEGY_META['parameters'] string must contain a JSON/Python dict or key=value pairs."
        )

    if isinstance(value, (list, tuple)):
        coerced = {}
        for item in value:
            if not isinstance(item, dict):
                raise TypeError(
                    "STRATEGY_META['parameters'] list entries must be dict values."
                )

            if "name" in item:
                name = str(item.get("name", "")).strip()
                if not name:
                    raise TypeError(
                        "STRATEGY_META['parameters'] list entries with 'name' must provide a non-empty value."
                    )
                parameter_value = item.get("default")
                if "default" not in item:
                    for fallback_key in ("value", "initial", "start"):
                        if fallback_key in item:
                            parameter_value = item[fallback_key]
                            break
                coerced[name] = parameter_value
                continue

            if len(item) == 1:
                key, parameter_value = next(iter(item.items()))
                name = str(key).strip()
                if not name:
                    raise TypeError(
                        "STRATEGY_META['parameters'] single-entry dict keys must be non-empty."
                    )
                coerced[name] = parameter_value
                continue

            raise TypeError(
                "STRATEGY_META['parameters'] list entries must contain 'name' or a single key-value pair."
            )

        return coerced

    raise TypeError(
        "STRATEGY_META['parameters'] must be a dict, a supported list, or a mapping string."
    )


def _coerce_tags(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, dict):
        coerced: dict[str, Any] = {}
        for key, tag_value in value.items():
            name = str(key).strip()
            if not name:
                continue
            coerced[name] = tag_value
        return coerced

    if isinstance(value, str):
        parsed = _parse_mapping_string(value)
        if parsed is not None:
            return _coerce_tags(parsed)

        label = value.strip()
        if not label:
            return {}
        return {"labels": [label]}

    if isinstance(value, (list, tuple)):
        labels: list[str] = []
        merged: dict[str, Any] = {}

        for item in value:
            if isinstance(item, str):
                label = item.strip()
                if label:
                    labels.append(label)
                continue
            if isinstance(item, dict):
                for key, tag_value in item.items():
                    name = str(key).strip()
                    if not name:
                        continue
                    merged[name] = tag_value
                continue
            raise TypeError(
                "STRATEGY_META['tags'] list entries must be strings or dict values."
            )

        if labels:
            existing = merged.get("labels")
            if isinstance(existing, list):
                merged["labels"] = [*existing, *labels]
            elif existing is None:
                merged["labels"] = labels
            else:
                merged["labels"] = [existing, *labels]

        return merged

    raise TypeError(
        "STRATEGY_META['tags'] must be a dict, a supported list, or a mapping string."
    )


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
