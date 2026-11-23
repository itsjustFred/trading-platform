"""Tests for scripts/pip_audit_severity_gate.py (#298).

The gate is the only thing standing between a noisy pip-audit report and a
weekly red build, so the severity math and the fail-closed paths are pinned
here. OSV lookups are injected; the suite's network guard stays untouched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import pip_audit_severity_gate as gate


def _report(*deps: tuple[str, str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": vulns}
            for name, version, vulns in deps
        ]
    }


def _vuln(vid: str, aliases: list[str] | None = None) -> dict[str, Any]:
    return {"id": vid, "fix_versions": ["9.9.9"], "aliases": aliases or []}


def _osv_record(*vectors: str, vuln_id: str = "GHSA-test") -> dict[str, Any]:
    return {
        "id": vuln_id,
        "severity": [{"type": "CVSS_V3", "score": v} for v in vectors],
    }


# CVSS:3.1 vectors with known base scores (cross-checked against the `cvss`
# package): a pure network-available high and the canonical critical.
HIGH_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"  # 7.5
CRITICAL_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"  # 10.0
MODERATE_VECTOR = "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N"  # 3.1


def test_cvss3_base_score_matches_reference_values() -> None:
    assert gate._cvss3_base_score(HIGH_VECTOR) == pytest.approx(7.5)
    assert gate._cvss3_base_score(CRITICAL_VECTOR) == pytest.approx(10.0)
    assert gate._cvss3_base_score(MODERATE_VECTOR) == pytest.approx(3.1)


def test_cvss3_base_score_rejects_incomplete_or_foreign_vectors() -> None:
    assert gate._cvss3_base_score("not-a-vector") is None
    assert gate._cvss3_base_score("CVSS:3.1/AV:N/AC:L") is None
    assert gate._cvss3_base_score("CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P") is None


def test_high_severity_finding_blocks() -> None:
    report = _report(("demo", "1.0", [_vuln("PYSEC-1")]))
    records = {"PYSEC-1": _osv_record(HIGH_VECTOR)}

    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].blocked
    assert rows[0].score == pytest.approx(7.5)


def test_moderate_and_low_findings_pass() -> None:
    report = _report(
        ("demo", "1.0", [_vuln("PYSEC-low"), _vuln("PYSEC-med")]),
    )
    records = {
        "PYSEC-low": _osv_record("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:L"),
        "PYSEC-med": _osv_record(MODERATE_VECTOR),
    }
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert not any(row.blocked for row in rows)


def test_unknown_severity_fails_closed() -> None:
    report = _report(("demo", "1.0", [_vuln("PYSEC-mystery")]))
    records = {"PYSEC-mystery": {"id": "PYSEC-mystery", "severity": []}}
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].blocked
    assert rows[0].score is None


def test_missing_osv_record_fails_closed() -> None:
    report = _report(("demo", "1.0", [_vuln("PYSEC-gone", ["GHSA-gone"])]))
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records={},
    )
    assert rows[0].blocked
    assert rows[0].score is None


def test_alias_fallback_finds_the_osv_record() -> None:
    report = _report(
        ("demo", "1.0", [_vuln("PYSEC-1", ["GHSA-aaaa-bbbb-cccc"])]),
    )
    records = {"GHSA-aaaa-bbbb-cccc": _osv_record(HIGH_VECTOR)}
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].score == pytest.approx(7.5)
    assert rows[0].blocked


def test_high_scored_alias_is_not_masked_by_low_scored_primary() -> None:
    """The severity is the max over every candidate record, not the first."""
    report = _report(("demo", "1.0", [_vuln("PYSEC-1", ["GHSA-high"])]))
    records = {
        "PYSEC-1": _osv_record(MODERATE_VECTOR, vuln_id="PYSEC-1"),
        "GHSA-high": _osv_record(HIGH_VECTOR, vuln_id="GHSA-high"),
    }
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].score == pytest.approx(7.5)
    assert rows[0].blocked


def test_unscored_primary_does_not_hide_scored_alias() -> None:
    """An unscored record is not UNKNOWN while a scored sibling exists."""
    report = _report(("demo", "1.0", [_vuln("PYSEC-1", ["GHSA-med"])]))
    records = {
        "PYSEC-1": {"id": "PYSEC-1", "severity": []},
        "GHSA-med": _osv_record(MODERATE_VECTOR, vuln_id="GHSA-med"),
    }
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].score == pytest.approx(3.1)
    assert not rows[0].blocked


def test_unscored_everywhere_is_unknown() -> None:
    report = _report(("demo", "1.0", [_vuln("PYSEC-1", ["GHSA-x"])]))
    records = {
        "PYSEC-1": {"id": "PYSEC-1", "severity": []},
        "GHSA-x": {"id": "GHSA-x"},
    }
    rows = gate.evaluate(
        gate._findings_from_report(report),
        threshold=7.0,
        records=records,
    )
    assert rows[0].score is None
    assert rows[0].blocked


def test_main_exit_codes(tmp_path, capsys) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(_report(("demo", "1.0", [_vuln("PYSEC-1")]))),
        encoding="utf-8",
    )
    records = {"PYSEC-1": _osv_record(HIGH_VECTOR)}
    assert gate.main(
        [str(report_path), "--osv-url", "https://osv.test"],
        records=records,
    ) == 1
    out = capsys.readouterr().out
    assert "demo" in out
    assert "PYSEC-1" in out

    clean_path = tmp_path / "clean.json"
    clean_path.write_text(
        json.dumps(_report(("demo", "1.0", [_vuln("PYSEC-ok")]))),
        encoding="utf-8",
    )
    clean_records = {
        "PYSEC-ok": _osv_record(
            "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:L"
        )
    }
    assert gate.main(
        [str(clean_path), "--osv-url", "https://osv.test"],
        records=clean_records,
    ) == 0


def test_main_reports_dependencies_pip_audit_skipped(tmp_path, capsys) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "vulns": [_vuln("PYSEC-ok")],
                    },
                    {
                        "name": "skipped-pkg",
                        "version": "2.0",
                        "skip_reason": "could not be audited",
                        "vulns": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    records = {
        "PYSEC-ok": _osv_record(
            "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:L"
        )
    }
    assert gate.main(
        [str(report_path), "--osv-url", "https://osv.test"],
        records=records,
    ) == 0
    out = capsys.readouterr().out
    assert "SKIP skipped-pkg==2.0" in out
    assert "not audited" in out


def test_main_rejects_missing_or_malformed_report(tmp_path) -> None:
    assert gate.main([str(tmp_path / "nope.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gate.main([str(bad)]) == 2
