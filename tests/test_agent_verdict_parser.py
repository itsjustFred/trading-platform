"""Unit tests for scripts/agents/parse_verdict.py.

The three agent CI workflows used to each carry their own shell regex for the
agent verdict; the copies drifted (one accepted ``Verdict: PASSed`` as PASS).
The parser is now the single reader, and these tests pin its contract:

- the verdict must be the last non-empty line of the extracted report
  (a trailing "Human sign-off required." note is tolerated),
- the token must match the stage's vocabulary exactly - no prefix games,
- the report is the tail starting at the last column-0 ``**Summary:**`` line,
- ``comment_names_head`` ties a posted comment to the commit it analysed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PARSER_PATH = REPO_ROOT / "scripts" / "agents" / "parse_verdict.py"


def _load_parser():
    spec = importlib.util.spec_from_file_location("parse_verdict", PARSER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


parse_verdict = _load_parser()


def _report(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# extract_report
# ---------------------------------------------------------------------------


def test_extract_report_keeps_the_tail_from_the_last_summary_line() -> None:
    raw = (
        "some preamble chatter\n"
        "**Summary:** first attempt\n**Verdict: PASS**\n"
        "agent kept talking\n"
        "**Summary:** the real report\nbody line\n**Verdict: BLOCK** — reason\n"
    )
    assert parse_verdict.extract_report(raw) == (
        "**Summary:** the real report\nbody line\n**Verdict: BLOCK** — reason\n"
    )


def test_extract_report_drops_a_trailing_code_fence() -> None:
    raw = "**Summary:** report\n**Verdict: PASS**\n```\n"
    assert parse_verdict.extract_report(raw) == "**Summary:** report\n**Verdict: PASS**\n"


def test_extract_report_drops_a_fence_before_trailing_blank_lines() -> None:
    """A model that closes its fence and then pads the stream with blank
    lines still ends the report at the fence."""
    raw = "**Summary:** report\n**Verdict: PASS**\n```\n\n\n"
    assert parse_verdict.extract_report(raw) == "**Summary:** report\n**Verdict: PASS**\n"


def test_extract_report_is_empty_without_a_summary_line() -> None:
    assert parse_verdict.extract_report("no report here\n**Verdict: PASS**\n") == ""


# ---------------------------------------------------------------------------
# parse_verdict: verdict must be last, token must be exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "verdict"),
    [
        ("review", "PASS"),
        ("review", "BLOCK"),
        ("review", "CHANGES-REQUESTED"),
        ("review", "INCOMPLETE"),
        ("mutation", "PASS"),
        ("mutation", "GAPS"),
        ("mutation", "INCOMPLETE"),
        ("premortem", "BLOCK"),
        ("premortem", "CHANGES-REQUESTED"),
        ("premortem", "PASS"),
        ("premortem", "INCOMPLETE"),
    ],
)
def test_parse_verdict_accepts_every_documented_token(stage: str, verdict: str) -> None:
    report = _report("**Summary:** x", f"**Verdict: {verdict}**")
    assert parse_verdict.parse_verdict(report, stage) == verdict


@pytest.mark.parametrize(
    "line",
    [
        "**Verdict: PASSed** — looks fine",
        "**Verdict: BLOCKED**",
        "**Verdict: INCOMPLETELY**",
        "**Verdict: PASSED**",
    ],
)
def test_parse_verdict_rejects_prefix_extensions(line: str) -> None:
    report = _report("**Summary:** x", line)
    for stage in ("review", "mutation", "premortem"):
        assert parse_verdict.parse_verdict(report, stage) is None


def test_parse_verdict_rejects_a_verdict_that_is_not_the_last_line() -> None:
    report = _report("**Summary:** x", "**Verdict: PASS**", "trailing chatter")
    assert parse_verdict.parse_verdict(report, "review") is None


def test_parse_verdict_tolerates_a_trailing_sign_off_note() -> None:
    report = _report(
        "**Summary:** x", "**Verdict: CHANGES-REQUESTED (1 severe)**", "Human sign-off required."
    )
    assert parse_verdict.parse_verdict(report, "premortem") == "CHANGES-REQUESTED"


def test_parse_verdict_rejects_an_annotation_between_verdict_and_signoff() -> None:
    report = _report("**Summary:** x", "**Verdict: PASS**", "note", "Human sign-off required.")
    assert parse_verdict.parse_verdict(report, "premortem") is None


def test_parse_verdict_rejects_a_quoted_verdict_line() -> None:
    report = _report("**Summary:** x", "> **Verdict: PASS** quoted")
    assert parse_verdict.parse_verdict(report, "review") is None


def test_parse_verdict_rejects_a_report_without_a_verdict() -> None:
    report = _report("**Summary:** x", "no verdict here")
    assert parse_verdict.parse_verdict(report, "review") is None


def test_parse_verdict_stage_vocabularies_differ() -> None:
    """GAPS is a mutation-only token; review must refuse it."""
    report = _report("**Summary:** x", "**Verdict: GAPS**")
    assert parse_verdict.parse_verdict(report, "mutation") == "GAPS"
    assert parse_verdict.parse_verdict(report, "review") is None
    assert parse_verdict.parse_verdict(report, "premortem") is None


def test_parse_verdict_rejects_an_unknown_stage() -> None:
    report = _report("**Verdict: PASS**")
    with pytest.raises(ValueError, match="unknown stage"):
        parse_verdict.parse_verdict(report, "bogus")


def test_parse_verdict_fails_closed_when_no_line_can_be_a_verdict() -> None:
    """A report whose every line is blank or the sign-off note leaves no
    verdict candidate at all; the parser must return None, never a default
    PASS - a missing verdict masquerading as success is the failure this
    guards."""
    assert parse_verdict.parse_verdict("\nHuman sign-off required.\n", "premortem") is None


# ---------------------------------------------------------------------------
# comment_names_head: a posted comment must name the analysed head
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "footer _Analysed base `abc12345` and head `def67890`._",
        "Analysed base abc12345 head def67890.",
        "head `def67890` only",
        "head def6789012345678901234567890123456789abcd",
    ],
)
def test_comment_names_head_accepts_head_references(text: str) -> None:
    assert parse_verdict.comment_names_head(text, "def6789012345678901234567890123456789abcd")


@pytest.mark.parametrize(
    "text",
    [
        "**Summary:** no footer",
        "Analysed base `abc12345` and head `99999999`._",
        "",
    ],
)
def test_comment_names_head_rejects_missing_or_wrong_head(text: str) -> None:
    assert not parse_verdict.comment_names_head(
        text, "def6789012345678901234567890123456789abcd"
    )


# ---------------------------------------------------------------------------
# comment_is_real_verdict: the quota-overwrite guard's one predicate
# ---------------------------------------------------------------------------


def test_comment_is_real_verdict_needs_both_marker_and_head() -> None:
    """A real verdict for a head carries the verdict tag AND the analysed-head
    footer; either alone is not proof (a quota body can quote a verdict line,
    and every footer - quota bodies included - names a head)."""
    head = "def6789012345678901234567890123456789abcd"
    footer = f"_Analysed base `abc12345` ... head `{head[:8]}`._"
    assert parse_verdict.comment_is_real_verdict(f"**Verdict: PASS**\n{footer}", head)
    assert not parse_verdict.comment_is_real_verdict("**Verdict: PASS**\n", head)
    assert not parse_verdict.comment_is_real_verdict(footer, head)
    assert not parse_verdict.comment_is_real_verdict(
        f"**Verdict: PASS**\n{footer}", "0" * 40
    )


# ---------------------------------------------------------------------------
# the stage vocabularies must equal what each skill documents
# ---------------------------------------------------------------------------


def test_stage_vocabulary_matches_the_skill_docs() -> None:
    """Each skill's ``Verdicts:`` line documents its allowed tokens; the
    parser's per-stage sets must equal them so a skill change cannot drift
    from what the workflow accepts."""
    skill_dirs = {
        "review": "actions-code-review",
        "mutation": "actions-mutation-testing",
        "premortem": "actions-pre-mortem",
    }
    for stage, verdicts in parse_verdict.STAGES.items():
        skill = REPO_ROOT / ".agents" / "skills" / skill_dirs[stage] / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        match = re.search(r"^- Verdicts:\s*(.+)$", text, re.M)
        assert match, f"{skill} has no '- Verdicts:' line"
        documented = set(re.findall(r"`([^`]+)`", match.group(1)))
        assert documented == set(verdicts[1]), (
            f"{stage}: skill documents {sorted(documented)}, "
            f"parser accepts {sorted(verdicts[1])}"
        )
