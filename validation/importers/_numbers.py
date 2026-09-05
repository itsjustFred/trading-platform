"""Shared parsing helpers for MT5 report importers.

MT5 exports numbers using the report locale.  German reports use ``.`` as a
thousands separator and ``,`` as the decimal separator, while ASCII reports
use ``.`` as the decimal separator.  Naive ``replace(",", ".")`` parsing
silently corrupts dot-thousands values ("2.500" -> 2.5) and aborts on
mixed-format values ("1.234,56" -> "1.234.56" -> ValueError).

Because "2.500" alone is genuinely ambiguous, the report locale must be
detected first (:func:`detect_decimal_comma`) and handed to
:func:`parse_locale_number`.
"""

from __future__ import annotations

import re

# German grouping: 1-3 leading digits then groups of exactly three, followed
# by a comma decimal part ("1.234,56", "2.500", "1.234.567,89").
_GERMAN_GROUPED = re.compile(r"^\d{1,3}(\.\d{3})+,\d{1,4}$")
# ASCII grouping: "12,345.67".
_ASCII_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+\.\d{1,4}$")

def detect_decimal_comma(values: list[str | None]) -> bool:
    """Return True when any value proves the report uses German formatting.

    A value matching the German grouped pattern ("1.234,56") selects the
    German convention; an ASCII grouped value ("12,345.67") would select the
    ASCII convention.  Ambiguous or ungrouped values decide nothing.
    """
    for raw in values:
        text = _normalise_number(raw)
        if text is None:
            continue
        if _GERMAN_GROUPED.match(text):
            return True
        if _ASCII_GROUPED.match(text):
            return False
    return False

def _normalise_number(raw: str | None) -> str | None:
    """Return the stripped, separator-free text of *raw* (or None)."""
    if raw is None:
        return None
    text = str(raw).strip().replace(" ", "").replace("\u00a0", "")
    return text or None

def _split_german(text: str, last_comma: int) -> tuple[str, str] | None:
    """Split a German-convention value into (integer digits, fraction digits).

    Returns ``None`` when the value cannot be split unambiguously.
    """
    if last_comma != -1:
        # The comma is the decimal mark; dots can only be group separators.
        digits = text[:last_comma].replace(".", "")
        decimal = text[last_comma + 1 :]
        if "," in decimal or len(decimal) > 8:
            return None
        return digits, decimal

    # Dot only: thousands group when the digits group by three,
    # decimal mark otherwise ("1.08500" stays 1.085).
    if _GROUPED_DOT.match(text):
        return text.replace(".", ""), ""
    if text.count(".") != 1:
        return None
    digits, _, decimal = text.partition(".")
    return digits, decimal

def _split_ascii(text: str, last_dot: int) -> tuple[str, str] | None:
    """Split an ASCII-convention value into (integer digits, fraction digits).

    Returns ``None`` when the value cannot be split unambiguously.
    """
    if last_dot != -1:
        # The dot is the decimal mark; commas can only be group separators.
        if text.count(".") != 1:
            return None
        digits = text[:last_dot].replace(",", "")
        decimal = text[last_dot + 1 :]
        return digits, decimal
    if text.count(",") != 1:
        return None
    digits, _, decimal = text.partition(",")
    return digits, decimal


def _split_sign(text: str | None) -> tuple[str | None, bool]:
    """Strip a leading sign from *text*; return (digits-only text, negative).

    Returns ``(None, False)`` when *text* is empty or sign-only.
    """
    if text is None:
        return None, False
    negative = text.startswith("-")
    if negative or text.startswith("+"):
        text = text[1:]
    return (text or None), negative


def parse_locale_number(raw: str | None, decimal_comma: bool) -> float | None:
    """Parse a locale-formatted number into a float.

    German mode (``decimal_comma=True``): a dot is a thousands separator when
    the digits form groups of exactly three (``"2.500"`` -> ``2500.0``) and a
    decimal mark otherwise (``"1.08500"`` -> ``1.085``); the comma is always
    the decimal mark (``"1.234,56"`` -> ``1234.56``).

    ASCII mode (``decimal_comma=False``): the dot is the decimal mark
    (``"2.500"`` -> ``2.5``), commas are thousands separators
    (``"12,345.67"`` -> ``12345.67``).

    Returns ``None`` when the value cannot be parsed unambiguously (empty,
    non-numeric, or mixed grouping such as "1.2.3") so callers can skip the
    value instead of storing a corrupted one.
    """
    text, negative = _split_sign(_normalise_number(raw))
    if text is None:
        return None

    last_comma = text.rfind(",")
    last_dot = text.rfind(".")

    if last_comma == -1 and last_dot == -1:
        digits, decimal = text, ""
    elif decimal_comma:
        split = _split_german(text, last_comma)
        if split is None:
            return None
        digits, decimal = split
    else:
        split = _split_ascii(text, last_dot)
        if split is None:
            return None
        digits, decimal = split

    if not digits or not digits.isdigit():
        return None
    if decimal and not decimal.isdigit():
        return None

    value = float(f"{digits}.{decimal}" if decimal else digits)
    return -value if negative else value


_GROUPED_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")


def parse_german_number(raw: str | None) -> float | None:
    """Parse a German-locale number ("2.500" -> 2500.0).

    Thin convenience wrapper over :func:`parse_locale_number` with
    ``decimal_comma=True``; use :func:`parse_locale_number` directly when the
    report locale is detected rather than known.
    """
    return parse_locale_number(raw, decimal_comma=True)
