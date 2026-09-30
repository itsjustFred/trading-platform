"""The preflight must resolve credentials exactly as the daemon does.

`_resolve_ctrader_credentials` is implemented twice - in `live_runner/daemon.py`
and in `live_runner/preflight.py` - with the same rule: app id, secret and access
token are required, the refresh token is optional, and `CTRADER_DEMO` defaults to
true. Only the daemon's copy is tested; the preflight's is named in no test file.

That is this session's recurring shape with the roles inverted, and it matters more
here than the count suggests. The preflight exists to check that an account is
safe *before* live trading touches it. If its resolver is subtly more or less
permissive than the one the daemon actually uses, the preflight is validating
something the daemon will not do - a guard that does not guard, which is the whole
class of defect this branch has spent the session removing.

So this asserts the two agree, across a matrix of environments. It also pins the
preflight's copy on its own terms, since a rule that only exists twice and is
exercised once will drift the moment either side is edited.
"""

from __future__ import annotations

import pytest

from live_runner import daemon, preflight

_BASE = {
    "CTRADER_APP_ID": "app-1",
    "CTRADER_APP_SECRET": "secret-1",
    "CTRADER_ACCESS_TOKEN": "token-1",
}


def _resolve(module, values):
    """Call either copy, reporting a raised ValueError as a comparable outcome.

    A refusal is a legitimate answer from both, and "preflight refuses what the
    daemon accepts" is precisely the divergence worth catching - so the exception
    is part of the result rather than something the test hides.
    """
    try:
        return ("ok", module._resolve_ctrader_credentials(values))
    except ValueError as exc:
        return ("refused", str(exc))


CASES = {
    "complete": {**_BASE, "CTRADER_REFRESH_TOKEN": "refresh-1"},
    "no-refresh-token": {**_BASE},
    "demo-absent": {**_BASE, "CTRADER_REFRESH_TOKEN": "r"},
    "demo-true": {**_BASE, "CTRADER_DEMO": "true"},
    "demo-false": {**_BASE, "CTRADER_DEMO": "false"},
    "demo-yes": {**_BASE, "CTRADER_DEMO": "yes"},
    "demo-off": {**_BASE, "CTRADER_DEMO": "off"},
    # The value the daemon strips before parsing and the preflight passes raw.
    # Both must land on the same answer or one of them is lying.
    "demo-padded": {**_BASE, "CTRADER_DEMO": "  true  "},
    "demo-mixed-case": {**_BASE, "CTRADER_DEMO": "TrUe"},
    "demo-malformed": {**_BASE, "CTRADER_DEMO": "maybe"},
    "demo-empty-string": {**_BASE, "CTRADER_DEMO": ""},
    "missing-app-id": {k: v for k, v in _BASE.items() if k != "CTRADER_APP_ID"},
    "missing-secret": {k: v for k, v in _BASE.items() if k != "CTRADER_APP_SECRET"},
    "missing-access-token": {k: v for k, v in _BASE.items() if k != "CTRADER_ACCESS_TOKEN"},
    "all-padded": {k: f"  {v}  " for k, v in _BASE.items()},
    "empty": {},
}


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_preflight_and_daemon_resolve_credentials_identically(case):
    values = CASES[case]
    assert _resolve(preflight, dict(values)) == _resolve(daemon, dict(values)), (
        f"case {case!r}: the preflight and the daemon disagree about the "
        "credentials they will use, so the preflight is not checking what the "
        "daemon does"
    )


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_the_preflight_copy_alone_behaves_as_documented(case):
    """The preflight's copy on its own terms, so it is not merely agreeing by luck.

    Without this, two implementations that were both wrong in the same way would
    satisfy the agreement test above.
    """
    outcome, result = _resolve(preflight, dict(CASES[case]))
    if outcome == "refused":
        assert result, "a refusal must say which variable was wrong"
        return
    if result is None:
        # A required credential is absent, which is a legitimate "cannot trade".
        assert any(
            not str(CASES[case].get(key, "")).strip()
            for key in ("CTRADER_APP_ID", "CTRADER_APP_SECRET", "CTRADER_ACCESS_TOKEN")
        ), (
            f"case {case!r} resolved to no credentials, but all three required "
            "values are present - something is dropping a usable configuration"
        )
        return
    _app_id, _secret, _token, _refresh, demo = result
    assert isinstance(demo, bool), f"CTRADER_DEMO resolved to {demo!r}, not a bool"
    if "CTRADER_DEMO" not in CASES[case]:
        assert demo is True, (
            "CTRADER_DEMO must default to true, or a missing value silently changes side"
        )
