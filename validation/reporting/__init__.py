"""Validation reporting package.

Provides JSON and Markdown report generation for strategy validation.
"""

from __future__ import annotations

from validation.reporting.json_report import (
    ValidationReport,
    generate_json_report,
    generate_markdown_report,
    serialize_validation_report,
)

__all__ = [
    "generate_json_report",
    "generate_markdown_report",
    "serialize_validation_report",
    "ValidationReport",
]
