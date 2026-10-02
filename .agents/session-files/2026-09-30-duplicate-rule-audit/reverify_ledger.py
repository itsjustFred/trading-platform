"""Re-verify the historical mutation ledger under the new kill rule.

The 14 mutations in `mutation-testing-report.md` were last re-verified at an older head by a
harness that decided KILLED from `rc != 0` alone. Today that harness was found to report
`KILLED ... failed=0` for a suite that exited 1 with **no test failing** - a fixture teardown
error, or a warning promoted to an error. So every historical KILLED is suspect until its
evidence is re-derived: a kill requires at least one failing test.

Each mutation below is reconstructed from the report's own description, and the exact target
text is read from `risk/models.py` at HEAD so an anchor that has drifted is reported as
ANCHOR-MISS rather than silently applied to the wrong line.

Two passes per mutation, because the harness's own contract distinguishes them:
  1. no `--detector` - proves a kill exists (rc 8 = killed but unattributed, or rc 0 if it
     happens to be attributable). Either way `failed=` must be >= 1.
  2. with `--detector` set to the test discovered in pass 1 - proves the kill is *this*
     mutation's, which is rc=0 and the only value a caller may trust.

M5 and M8 are the same mutation per the report, so M8 is run as an alias of M5.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path("/home/ubuntu/perma-mut")
HARNESS = ROOT / ".agents/session-files/2026-09-30-duplicate-rule-audit/mutate.sh"
#: `risk/models.py` carries M1-M8; the downloaders carry D1-D6. The report scopes the audit to
#: "production files changed by this session", and these three are those files.
TARGETS = {
    "M": pathlib.Path("risk/models.py"),
    "D_ALPACA": pathlib.Path("data_tools/downloaders/alpaca_dl.py"),
    "D_HF": pathlib.Path("data_tools/downloaders/huggingface_dl.py"),
}
VENV = "/home/ubuntu/Desktop/trading-platform/.venv/bin/python"
SUITE = ("tests/test_risk_models.py tests/validation/test_risk_sizing.py tests/live_runner "
         "tests/test_duplicate_rule_implementations.py")

# D1-D6, reconstructed from the same report. "raise -> coerce" is `errors="raise"` becoming
# `errors="coerce"`, which is the mutation that turns a rejected timestamp into a NaT - the
# kind of change that hides a data problem instead of surfacing it.
DOWNLOADER_MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("D1", "alpaca_dl._to_utc_timestamp: `raise` -> `coerce`", "D_ALPACA",
     'ts = pd.to_datetime(value, utc=True, errors="raise")',
     'ts = pd.to_datetime(value, utc=True, errors="coerce")'),
    ("D2", "alpaca_dl._to_utc_timestamp: drop the empty-DatetimeIndex guard", "D_ALPACA",
     "    if isinstance(ts, pd.DatetimeIndex):\n        if len(ts) == 0:\n            return None\n",
     "    if isinstance(ts, pd.DatetimeIndex):\n"),
    ("D3", "alpaca_dl._normalize_supported_timeframe: delete the refusal", "D_ALPACA",
     '    if not canonical:\n        raise ValueError(',
     '    if False:\n        raise ValueError('),
    ("D4", "alpaca_dl: drop `.strip().lower()` from normalisation", "D_ALPACA",
     '    normalized = str(value or "").strip().lower()',
     '    normalized = str(value or "")'),
    ("D5", "huggingface_dl._to_utc_timestamp: `raise` -> `coerce`", "D_HF",
     'ts = pd.to_datetime(value, utc=True, errors="raise")',
     'ts = pd.to_datetime(value, utc=True, errors="coerce")'),
    ("D6", "huggingface_dl._to_utc_timestamp: reintroduce the dead second lookup", "D_HF",
     "    if isinstance(ts, pd.DatetimeIndex):\n        if len(ts) == 0:\n            return None\n        return pd.Timestamp(ts[0])",
     "    if isinstance(ts, pd.DatetimeIndex):\n        if len(ts) == 0:\n            return None\n        ts = ts[1]\n        return pd.Timestamp(ts[0])"),
]

# (id, description, old, new) - old/new are exact text from risk/models.py at HEAD.
MUTATIONS: list[tuple[str, str, str, str]] = [
    ("M1", "_finite_positive: `> 0` becomes `>= 0`",
     '    if numeric <= 0:\n        raise ValueError(f"{field_name} must be > 0")',
     '    if numeric < 0:\n        raise ValueError(f"{field_name} must be > 0")'),
    ("M2", "_finite_fraction: `<= 1.0` becomes `< 1.0`",
     "    if not 0.0 <= numeric <= 1.0:",
     "    if not 0.0 <= numeric < 1.0:"),
    ("M3", "_finite_nonnegative: `< 0` becomes `<= 0`",
     '    if numeric < 0:\n        raise ValueError(f"{field_name} must be >= 0")',
     '    if numeric <= 0:\n        raise ValueError(f"{field_name} must be >= 0")'),
    ("M4", "_finite_float: delete the `bool` guard",
     '    if isinstance(value, bool):\n        raise ValueError(f"{field_name} must be numeric")\n',
     ""),
    ("M5", "_finite_float: `isfinite` becomes a NaN check",
     "    if not math.isfinite(numeric):",
     "    if numeric != numeric:"),
    # The anchor is disambiguated with the following line for the reason the report itself
    # gives about M4: `if value is None: return None` appears in more than one helper, and a
    # mutation that cannot be applied is not a pass - it is an absence of evidence.
    ("M6", "_optional_finite_positive: delete the `None` guard",
     "    if value is None:\n        return None\n    return _finite_positive(value, field_name)",
     "    return _finite_positive(value, field_name)"),
    ("M7", "_parse_bool: drop the `int {0,1}` branch",
     "    if isinstance(value, int) and value in {0, 1}:\n        return bool(value)\n",
     ""),
    ("M8", "same mutation as M5 (the report lists it twice)",
     "    if not math.isfinite(numeric):",
     "    if numeric != numeric:"),
]


def failed_count(stdout: str) -> int:
    match = re.search(r"failed=(\d+)", stdout)
    return int(match.group(1)) if match else -1


def run(label: str, old: str, new: str, detector: str | None,
        target: pathlib.Path) -> subprocess.CompletedProcess:
    args = [str(HARNESS), "--file", str(target), "--suite", SUITE,
            "--label", label, "--old", old, "--new", new]
    if detector:
        args += ["--detector", detector]
    return subprocess.run(["bash", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=1800)


def main() -> int:
    source = (ROOT / TARGETS["M"]).read_text(encoding="utf-8")
    print("  re-verifying %d historical mutations under the 'a kill needs a failing test' rule"
          % (len(MUTATIONS) + len(DOWNLOADER_MUTATIONS)))
    print("  suite: %s" % SUITE)
    failures: list[str] = []
    for mid, desc, old, new in MUTATIONS:
        if source.count(old) != 1:
            print("  %-3s ANCHOR-MISS (%d occurrences) - not applied, not a pass"
                  % (mid, source.count(old)))
            failures.append(mid + " (anchor)")
            continue
        label = f"{mid} re-verify"
        first = run(label, old, new, None, TARGETS["M"])
        failed = failed_count(first.stdout)
        if first.returncode not in (0, 7, 8):
            print("  %-3s %-52s rc=%-2s FAILED-TO-RUN  %s"
                  % (mid, desc[:52], first.returncode,
                     (first.stdout.strip().splitlines() or [""])[0][:60]))
            failures.append(mid + " (rc)")
            continue
        if failed < 1:
            print("  %-3s %-52s rc=%-2s failed=%-2s NOT A KILL UNDER THE NEW RULE"
                  % (mid, desc[:52], first.returncode, failed))
            failures.append(mid + " (failed=0)")
            continue
        detector = None
        for line in first.stdout.splitlines():
            if line.strip().startswith("FAILED "):
                detector = line.strip().split("::")[-1].split(" ")[0]
                break
        second = run(f"{mid} attributed", old, new, detector, TARGETS["M"]) if detector else first
        verdict = "KILLED+ATTRIBUTED" if second.returncode == 0 else "KILLED (unattributed)"
        print("  %-3s %-52s rc=%-2s failed=%-2d %s"
              % (mid, desc[:52], second.returncode, failed, verdict))
        if second.returncode != 0:
            failures.append(mid + " (unattributed)")
    # The downloader half of the ledger.
    for mid, desc, key, old, new in DOWNLOADER_MUTATIONS:
        target = TARGETS[key]
        text = (ROOT / target).read_text(encoding="utf-8")
        if text.count(old) != 1:
            print("  %-3s ANCHOR-MISS (%d occurrences) - not applied, not a pass"
                  % (mid, text.count(old)))
            failures.append(mid + " (anchor)")
            continue
        first = run(f"{mid} re-verify", old, new, None, target)
        failed = failed_count(first.stdout)
        if first.returncode not in (0, 7, 8):
            print("  %-3s %-52s rc=%-2s FAILED-TO-RUN  %s"
                  % (mid, desc[:52], first.returncode,
                     (first.stdout.strip().splitlines() or [""])[0][:60]))
            failures.append(mid + " (rc)")
            continue
        if failed < 1:
            print("  %-3s %-52s rc=%-2s failed=%-2s NOT A KILL UNDER THE NEW RULE"
                  % (mid, desc[:52], first.returncode, failed))
            failures.append(mid + " (failed=0)")
            continue
        detector = None
        for line in first.stdout.splitlines():
            if line.strip().startswith("FAILED "):
                detector = line.strip().split("::")[-1].split(" ")[0]
                break
        second = run(f"{mid} attributed", old, new, detector, target) if detector else first
        verdict = "KILLED+ATTRIBUTED" if second.returncode == 0 else "KILLED (unattributed)"
        print("  %-3s %-52s rc=%-2s failed=%-2d %s"
              % (mid, desc[:52], second.returncode, failed, verdict))
        if second.returncode != 0:
            failures.append(mid + " (unattributed)")
    total = len(MUTATIONS) + len(DOWNLOADER_MUTATIONS)
    print()
    if failures:
        print("  %d of %d DID NOT re-verify: %s" % (len(failures), total, ", ".join(failures)))
        return 1
    print("  ALL %d RE-VERIFY under the new rule: every one killed with >=1 failing test"
          % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())