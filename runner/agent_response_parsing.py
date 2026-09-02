"""Response extraction and STRATEGY_META normalization for the auto agent.

Pure string/AST helpers: fenced-code-block extraction, markdown title
handling, STRATEGY_META parsing/normalization, and strategy-module assembly.
No I/O and no LLM access, so everything here is unit-testable in isolation.
"""

from __future__ import annotations

import ast
import re
from pprint import pformat
from typing import Any


def _extract_fenced_block(content: str, languages: tuple[str, ...]) -> str | None:
    r"""Return the body of the first fenced block for one of ``languages``.

    No \s* between the fence and the lazy group: on input where the fence is
    followed by a long run of whitespace and never closed, the greedy \s*
    retries the whole lazy scan at every position (super-linear, Sonar S8786).
    The lazy group swallows the leading whitespace and .strip() drops it.
    """
    for language in languages:
        match = re.search(rf"```{language}(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_markdown_response(content: str) -> str:
    return _extract_fenced_block(content or "", ("markdown", "md")) or (content or "").strip()


def extract_python_response(content: str) -> str:
    fenced = _extract_fenced_block(content or "", ("python", "py"))
    if fenced is not None:
        return fenced
    generic = _extract_fenced_block(content or "", ("",))
    if generic is not None:
        return generic
    return (content or "").strip()


def normalize_markdown_doc(markdown_doc: str, *, strategy_name: str | None) -> str:
    content = extract_markdown_response(markdown_doc).strip()
    if not re.search(r"^\s*#\s+.+$", content, flags=re.MULTILINE):
        title = strategy_name or "Auto Strategy"
        content = f"# {title}\n\n{content}".strip()
    return content


def extract_markdown_title(markdown_doc: str) -> str | None:
    match = re.search(r"^\s*#\s+(.+)$", markdown_doc, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def extract_strategy_meta(
    content: str,
    *,
    requested_name: str | None,
    markdown_doc: str,
) -> tuple[str, dict[str, Any]]:
    candidate = extract_python_response(content)

    try:
        module = ast.parse(candidate)
    except SyntaxError:
        module = None

    parsed: Any = None
    if module is not None:
        for node in module.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "STRATEGY_META"
                for target in node.targets
            ):
                parsed = ast.literal_eval(node.value)
                break
        if parsed is None and len(module.body) == 1 and isinstance(module.body[0], ast.Expr):
            parsed = ast.literal_eval(module.body[0].value)

    if parsed is None:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise ValueError("Missing STRATEGY_META dictionary in model response.")
        parsed = ast.literal_eval(candidate[start : end + 1])

    if not isinstance(parsed, dict):
        raise TypeError("STRATEGY_META must be a dictionary.")

    normalized = normalize_strategy_meta(
        parsed, requested_name=requested_name, markdown_doc=markdown_doc
    )
    return format_strategy_meta_assignment(normalized), normalized


def normalize_strategy_meta(
    meta: dict[str, Any],
    *,
    requested_name: str | None,
    markdown_doc: str,
) -> dict[str, Any]:
    normalized = dict(meta)
    resolved_title = (
        extract_markdown_title(markdown_doc)
        or requested_name
        or normalized.get("name")
        or "Auto Strategy"
    )
    normalized["name"] = str(normalized.get("name") or resolved_title).strip() or str(
        resolved_title
    )
    normalized["version"] = str(normalized.get("version") or "1.0.0")

    parameters = normalized.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise TypeError("STRATEGY_META['parameters'] must be a dict.")
    normalized["parameters"] = parameters

    normalized["tags"] = _normalize_meta_tags(normalized.get("tags"))
    return normalized


def _normalize_meta_tags(tags: Any) -> dict[str, Any]:
    if tags is None:
        normalized_tags: dict[str, Any] = {}
    elif isinstance(tags, dict):
        normalized_tags = dict(tags)
    elif isinstance(tags, str):
        parts = [chunk.strip() for chunk in re.split(r"[;,]", tags) if chunk.strip()]
        normalized_tags = {"type": parts}
    elif isinstance(tags, (list, tuple, set)):
        normalized_tags = {"type": [str(item).strip() for item in tags if str(item).strip()]}
    else:
        raise TypeError("STRATEGY_META['tags'] must be a dict, string, or list of tags.")

    def _as_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    if "type" in normalized_tags:
        normalized_tags["type"] = _as_string_list(normalized_tags.get("type"))
    if "asset_class" in normalized_tags:
        normalized_tags["asset_class"] = _as_string_list(normalized_tags.get("asset_class"))

    for key in ("source", "link", "complexity"):
        if key in normalized_tags and normalized_tags[key] is not None:
            normalized_tags[key] = str(normalized_tags[key]).strip()

    return normalized_tags


def format_strategy_meta_assignment(meta: dict[str, Any]) -> str:
    return "STRATEGY_META = " + pformat(meta, width=100, sort_dicts=False)


def slugify(raw_value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", raw_value).strip("_").lower()
    return slug or "auto_strategy"


def extract_engine(markdown_doc: str, *, requested_engine: str | None) -> str:
    if requested_engine:
        return requested_engine.strip().lower()

    section_match = re.search(
        r"##\s*Framework-Empfehlung(.*?)(?:\n##|\Z)",
        markdown_doc,
        flags=re.IGNORECASE | re.DOTALL,
    )
    haystack = section_match.group(1) if section_match else markdown_doc
    engine_match = re.search(r"\b(vectorbt|pynecore|lean)\b", haystack, flags=re.IGNORECASE)
    if not engine_match:
        return "vectorbt"
    return engine_match.group(1).lower()


def assemble_strategy_module(meta_source: str, python_body: str) -> str:
    body = _strip_top_level_assignment(python_body.strip(), "STRATEGY_META")
    if not body:
        raise ValueError("Generated Python body is empty.")

    try:
        module = ast.parse(body)
    except SyntaxError:
        return f"{meta_source}\n\n{body.rstrip()}\n"

    lines = body.splitlines()
    insert_after = 0
    if module.body and isinstance(module.body[0], ast.Expr):
        value = getattr(module.body[0], "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            insert_after = getattr(module.body[0], "end_lineno", 0)

    if insert_after > 0:
        before = "\n".join(lines[:insert_after]).rstrip()
        after = "\n".join(lines[insert_after:]).lstrip("\n")
        pieces = [before, meta_source]
        if after:
            pieces.append(after)
        return "\n\n".join(piece for piece in pieces if piece).rstrip() + "\n"

    return f"{meta_source}\n\n{body.rstrip()}\n"


def _strip_top_level_assignment(source: str, variable_name: str) -> str:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return source

    ranges: list[tuple[int, int]] = []
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == variable_name for target in node.targets
        ):
            ranges.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno) - 1))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == variable_name
        ):
            ranges.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno) - 1))

    if not ranges:
        return source

    lines = source.splitlines()
    filtered: list[str] = []
    for idx, line in enumerate(lines):
        if any(start <= idx <= end for start, end in ranges):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()
