"""Severity gate for a pip-audit JSON report (issue #298).

pip-audit reports *that* a locked dependency has a known vulnerability but
not how severe it is, and it exits non-zero on any finding - including
low-severity noise nobody can act on. This gate enriches every finding with
the CVSS v3.1 base score published by the OSV database (https://osv.dev) and
fails the build when any finding scores at or above ``--threshold``
(default 7.0, the bottom of the HIGH band on the CVSS qualitative scale).

A finding whose severity cannot be determined also fails the gate: an
unrated vulnerability is not a cleared one, and silently passing it would
make the weekly audit a false green. If OSV genuinely has no score for an
advisory the fix is upstream data, not a waiver in this file.

Usage::

    pip-audit --no-deps -r requirements.txt --format json \\
        --output pip-audit-report.json || test -s pip-audit-report.json
    python scripts/pip_audit_severity_gate.py pip-audit-report.json

Exit codes: 0 = no blocking finding, 1 = at least one blocking finding,
2 = the report itself is missing or unreadable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_OSV_URL = "https://api.osv.dev"
DEFAULT_THRESHOLD = 7.0
FETCH_WORKERS = 8
FETCH_TIMEOUT = 30.0

# CVSS v3.1 base metrics -> numeric values, per the specification. Only the
# eight base metrics feed the base score; temporal/environmental metrics
# (E, RL, RC, ...) are ignored because a dependency audit has no target
# environment to score them against.
_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.5}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"N": 0.0, "L": 0.22, "H": 0.56}


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FindingResult:
    finding: Finding
    score: float | None
    threshold: float

    @property
    def blocked(self) -> bool:
        # None means "severity could not be determined" - fail closed.
        return self.score is None or self.score >= self.threshold


def _cvss3_base_score(vector: str) -> float | None:
    """Return the CVSS v3.x base score for a vector string, or None.

    Only ``CVSS:3.x`` vectors are scored: the v3 base equation is what every
    current advisory publishes, and guessing at v4/v2 encodings would be
    worse than reporting the severity as undetermined (which blocks).
    """
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    metrics = {
        part.split(":", 1)[0]: part.split(":", 1)[1]
        for part in vector.split("/", 1)[1].split("/")
        if ":" in part
    }
    try:
        av = _AV[metrics["AV"]]
        ac = _AC[metrics["AC"]]
        ui = _UI[metrics["UI"]]
        scope_changed = metrics["S"] == "C"
        pr = (_PR_CHANGED if scope_changed else _PR_UNCHANGED)[metrics["PR"]]
        confidentiality = _CIA[metrics["C"]]
        integrity = _CIA[metrics["I"]]
        availability = _CIA[metrics["A"]]
        if not scope_changed and metrics["S"] != "U":
            return None
    except KeyError:
        return None

    iss = 1 - (1 - confidentiality) * (1 - integrity) * (1 - availability)
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    if impact <= 0:
        return 0.0

    exploitability = 8.22 * av * ac * pr * ui
    if scope_changed:
        base = min(1.08 * (impact + exploitability), 10.0)
    else:
        base = min(impact + exploitability, 10.0)
    # CVSS roundup: smallest number >= base with one decimal digit.
    return math.ceil(base * 10 - 1e-9) / 10


def _record_score(record: dict[str, Any]) -> float | None:
    """Highest CVSS base score published on an OSV vulnerability record."""
    best: float | None = None
    for entry in record.get("severity") or []:
        if entry.get("type") != "CVSS_V3":
            continue
        score = _cvss3_base_score(str(entry.get("score") or ""))
        if score is not None and (best is None or score > best):
            best = score
    return best


def _best_score(records: list[dict[str, Any]]) -> float | None:
    """Highest CVSS base score across every candidate OSV record.

    A vuln id and its aliases can each resolve to a distinct OSV record
    with its own severity entries, and they do not always agree. The gate
    scores the *worst* of them: taking the first record found would let a
    low-scored primary mask a high-scored alias (a false green), while an
    unscored record must not hide a scored sibling either. ``None`` is
    returned only when no candidate record carries a CVSS v3 score at all.
    """
    best: float | None = None
    for record in records:
        score = _record_score(record)
        if score is not None and (best is None or score > best):
            best = score
    return best


def _findings_from_report(report: dict[str, Any]) -> list[Finding]:
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("pip-audit report has no 'dependencies' list")
    findings: list[Finding] = []
    for dep in dependencies:
        for vuln in dep.get("vulns") or []:
            if not dep.get("name") or not vuln.get("id"):
                continue
            findings.append(
                Finding(
                    package=str(dep["name"]),
                    version=str(dep.get("version") or "?"),
                    vuln_id=str(vuln["id"]),
                    aliases=tuple(str(a) for a in vuln.get("aliases") or []),
                )
            )
    return findings


def _skipped_dependencies(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Dependencies pip-audit skipped, as (name, version, skip_reason).

    A skipped package was never audited, so it cannot produce a finding -
    printing it keeps a silently uncovered package visible in the log
    instead of letting the green/yellow summary imply full coverage.
    """
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    skipped = []
    for dep in dependencies:
        reason = dep.get("skip_reason")
        if reason and dep.get("name"):
            skipped.append((str(dep["name"]), str(dep.get("version") or "?"), str(reason)))
    return skipped


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _make_lookup(
    osv_url: str,
    fetch_json: Callable[[str], Any],
) -> Callable[[str, tuple[str, ...]], list[dict[str, Any]]]:
    """OSV record lookup by vuln id with alias fallback, cached across calls.

    Returns *every* candidate record that resolved, not just the first:
    aliases are scored alongside the primary id (see :func:`_best_score`).
    """
    cache: dict[str, dict[str, Any] | None] = {}
    lock = threading.Lock()

    def lookup(vuln_id: str, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
        candidates = tuple(dict.fromkeys((vuln_id, *aliases)))
        with lock:
            pending = [c for c in candidates if c not in cache]
        for candidate in pending:
            try:
                record = fetch_json(f"{osv_url}/v1/vulns/{candidate}")
            except (urllib.error.URLError, TimeoutError, ValueError):
                record = None
            with lock:
                cache[candidate] = record if isinstance(record, dict) else None
        with lock:
            return [cache[c] for c in candidates if cache.get(c) is not None]

    return lookup


def _records_lookup(
    records: dict[str, dict[str, Any] | None],
) -> Callable[[str, tuple[str, ...]], list[dict[str, Any]]]:
    def lookup(vuln_id: str, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            record
            for candidate in dict.fromkeys((vuln_id, *aliases))
            if (record := records.get(candidate)) is not None
        ]

    return lookup


def evaluate(
    findings: list[Finding],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    osv_url: str = DEFAULT_OSV_URL,
    records: dict[str, dict[str, Any] | None] | None = None,
    fetch_json: Callable[[str], Any] = _fetch_json,
) -> list[FindingResult]:
    """Score every finding against OSV; unknown severity stays None."""
    lookup = (
        _records_lookup(records)
        if records is not None
        else _make_lookup(osv_url, fetch_json)
    )
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        osv_records = list(pool.map(lambda f: lookup(f.vuln_id, f.aliases), findings))
    return [
        FindingResult(
            finding=finding,
            score=_best_score(records_for_finding),
            threshold=threshold,
        )
        for finding, records_for_finding in zip(findings, osv_records, strict=True)
    ]


def main(
    argv: list[str] | None = None,
    *,
    records: dict[str, dict[str, Any] | None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="pip-audit JSON report")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="CVSS base score at or above which a finding blocks (default 7.0)",
    )
    parser.add_argument("--osv-url", default=DEFAULT_OSV_URL)
    args = parser.parse_args(argv)

    report_path = args.report.expanduser().resolve()
    if report_path.suffix.lower() != ".json":
        print(f"pip-audit report must be a .json file: {args.report}", file=sys.stderr)
        return 2

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        findings = _findings_from_report(report)
    except (OSError, ValueError) as exc:
        print(f"pip-audit report unreadable: {exc}", file=sys.stderr)
        return 2

    skipped = _skipped_dependencies(report)
    for name, version, reason in skipped:
        print(f"SKIP {name}=={version} (not audited: {reason})")
    if skipped:
        print(f"{len(skipped)} dependencies skipped by pip-audit - unaudited")

    results = evaluate(
        findings,
        threshold=args.threshold,
        osv_url=args.osv_url,
        records=records,
    )

    blocked = 0
    for result in results:
        finding = result.finding
        label = "UNKNOWN" if result.score is None else f"{result.score:.1f}"
        if result.blocked:
            blocked += 1
            mark = "BLOCK"
        else:
            mark = "ok"
        print(
            f"{mark:>5} {finding.package}=={finding.version} "
            f"{finding.vuln_id} cvss={label}"
        )
    print(
        f"{len(results)} finding(s), {blocked} blocking "
        f"(threshold {args.threshold}; unknown severity blocks)"
    )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
