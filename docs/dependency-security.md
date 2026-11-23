# Dependency security and updates

How this repo keeps its dependencies patched, and which known findings are
deliberately accepted. Decision record: issue #298 (maintainer decision
2026-11-23).

## Update channels

- **Dependabot** (`.github/dependabot.yml`): weekly, grouped, for the
  `github-actions` and `uv` ecosystems. Grouping means each run opens at
  most one PR per ecosystem containing every pending update, and nothing is
  ever auto-merged - a human reviews and merges each batch.
- **Weekly audit** (`.github/workflows/security-audit.yml`, Mondays 06:17
  UTC plus manual dispatch):
  - `pip-audit` scans `requirements.txt` - the locked `uv.lock` export, so
    the audit covers every extra without installing them. pip-audit exits
    non-zero on *any* finding, so its exit status is not the gate:
    `scripts/pip_audit_severity_gate.py` enriches each finding with its
    CVSS v3.1 base score from the OSV database and fails on scores >= 7.0
    (HIGH/CRITICAL). A finding whose severity cannot be determined also
    fails the gate - an unrated advisory is not a cleared one.
  - `npm audit --audit-level=high` in `frontend/` fails on high/critical
    frontend findings.
- The audit job is expected to run red while high-severity findings exist
  in the locked set; each red run is the signal to bump or document, not
  noise to suppress.

Run the Python half locally:

```bash
uvx --python 3.12 --from pip-audit==2.10.1 pip-audit \
  --no-deps -r requirements.txt --format json --output /tmp/pip-audit.json \
  || test -s /tmp/pip-audit.json
python3 scripts/pip_audit_severity_gate.py /tmp/pip-audit.json
```

## Accepted-risk pins

`ctrader-open-api==0.9.2` is an exact pin with no upgrade path: 0.9.3 was
yanked and no newer release exists. Its metadata exact-pins three
transitive dependencies, which is why the weekly audit reports findings
that cannot be fixed by a version bump today:

| Locked package | Held at | Because |
| --- | --- | --- |
| `requests` | 2.32.3 | `ctrader-open-api` requires `requests==2.32.3` (CVE-2024-47081; unreachable here - see the note in `pyproject.toml`) |
| `pyOpenSSL` | 24.1.0 | `ctrader-open-api` requires `pyOpenSSL==24.1.0` |
| `cryptography` | 42.0.8 | `pyOpenSSL==24.1.0` requires `cryptography<43` |
| `protobuf` | 3.20.1 | `ctrader-open-api` requires `protobuf==3.20.1` |

The `ctrader` extra is not installed by default (`uv sync` without
`--all-extras`/`--extra ctrader` never resolves these packages), so the
exposure is limited to deployments that opt into cTrader. Revisit all four
when `ctrader-open-api` ships a release that relaxes its pins; the weekly
audit will keep reporting them until then.

## Python version cap

`requires-python` is `>=3.12,<3.13`. 3.12 is the only tested interpreter:
the CI matrix, `mypy`'s `python_version`, ruff's `target-version` and
`viewer/main.py`'s `ThreadPoolExecutor` mirror (which validates
`sys.version_info == (3, 12)` exactly) all assume it. Widening the cap
requires a tested 3.13 CI leg first; `uv.lock` and `requirements.txt` are
regenerated from `pyproject.toml` (`uv lock`, then
`scripts/sync_requirements_from_lock.py`) and
`tests/test_dependency_hygiene.py` fails if they drift.
