"""The integration suite must not be able to reach production by accident.

219 backtest rows once landed in the production database because a plain
`pytest tests/` ran with the default BASE_URL, which pointed at the live viewer on
this host. Two guards existed and neither was enough, which is the part worth
remembering.

The first was an opt-in: BASE_URL must be set *and* PERMA_ALLOW_LIVE_VIEWER_WRITES
must be set. That is the right instinct - refuse to guess - but the opt-in is a
boolean somebody sets while thinking about *a* viewer, and the accident was
precisely "opted in, still aimed at the live platform".

The second was a `skipif` mark refusing the known production ports. It could not
work, because the suite performs HTTP *while being imported*: it discovers the
strategy matrix at module scope so the tests can be parametrised, and marks attach
to test functions, which are not evaluated until after that request has gone out.
Measured on the code as it stood: importing the module with BASE_URL aimed at
:5556 made a real connection attempt to 127.0.0.1:5556. After the fix, zero
attempts, and the import refuses with a reason.

So these tests assert the property rather than the guard's text, because the
property is what was missing.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent / "integration" / "test_strategy_pipeline.py"
)


def _isolation():
    """Import the side-effect-free guard, not the suite that uses it."""
    from tests.integration import viewer_isolation

    return viewer_isolation


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything opens a socket, and record what was attempted.

    Recording rather than only raising matters: the failure we care about is
    "it reached production and then reported something else", so the test needs to
    be able to say what was contacted.
    """
    attempts: list[object] = []

    def _spy(self, address):
        attempts.append(address)
        raise OSError(f"blocked by test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _spy)
    return attempts


def _import_suite(monkeypatch, base_url: str, **env: str):
    """Import the integration module with a controlled environment.

    Registered in ``sys.modules`` before exec because it defines a @dataclass, and
    dataclasses resolve fields through ``sys.modules[cls.__module__]`` - without it
    the exec fails with an error that says nothing about the real problem.
    """
    monkeypatch.setenv("BASE_URL", base_url)
    monkeypatch.setenv("PERMA_ALLOW_LIVE_VIEWER_WRITES", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    name = "integration_suite_probe"
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(name, _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    return spec, module


PRODUCTION = "http://127.0.0.1:5556"


def test_aiming_the_suite_at_production_makes_no_request_at_all(monkeypatch, no_network):
    """The property that was missing, and the one the 219 rows came through.

    Not "the suite is skipped" - skipped tests have still run whatever they did at
    import time. The requirement is that the request is never made.
    """
    spec, module = _import_suite(monkeypatch, PRODUCTION)

    with pytest.raises(BaseException) as caught:
        spec.loader.exec_module(module)

    assert no_network == [], (
        f"importing the suite with BASE_URL={PRODUCTION} opened a connection to "
        f"{no_network} before any test-level skip could apply. The discovery call "
        "runs at module scope, so the refusal has to happen before it."
    )
    assert "Refusing to contact" in str(caught.value), (
        "the refusal should say why, so a reader knows it was deliberate rather "
        f"than a connection failure: got {str(caught.value)[:120]!r}"
    )


def test_an_isolated_viewer_is_still_reachable(monkeypatch, no_network):
    """The guard must not make the integration suite impossible to run.

    A port this host does not serve production on is permitted, and the suite
    then does attempt its discovery - which is the point of the opt-in.
    """
    spec, module = _import_suite(monkeypatch, "http://127.0.0.1:5599")

    with pytest.raises(BaseException):
        # Fails on the real request to a closed port, which is fine and expected:
        # what is being checked is that the import got as far as trying.
        spec.loader.exec_module(module)

    assert no_network, (
        "an isolated viewer should be contacted; the guard has made the suite "
        "unrunnable rather than safe"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5556",
        "http://127.0.0.1:5556/api",
        "http://localhost:5556",
        "http://127.0.0.1:5557",
    ],
)
def test_the_live_platform_ports_are_refused(url):
    assert _isolation().targets_production(url), (
        f"{url} is where this host serves the live platform, so it must be refused "
        "regardless of the opt-in"
    )


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:5599", "http://127.0.0.1:6000"],
)
def test_other_ports_are_allowed(url):
    assert not _isolation().targets_production(url), (
        f"{url} is not a port this host serves production on, so refusing it would "
        "make the suite impossible to run at all"
    )


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://viewer.internal"])
def test_a_url_with_no_explicit_port_fails_closed(url):
    """An unchecked URL is an unknown URL, and unknown must mean refuse.

    Treating a missing port as "not production" would let `http://127.0.0.1/`
    through on a technicality - it resolves to port 80, which is the whole issue.
    """
    assert _isolation().targets_production(url), (
        f"{url} carries no explicit port, so production cannot be ruled out"
    )


def test_a_malformed_port_is_untrustworthy_not_safe():
    assert _isolation().targets_production("http://127.0.0.1:not-a-port")


def test_the_override_is_a_separate_acknowledgement(monkeypatch):
    """Kept, because a guard with no escape becomes a guard people disable.

    It is a second, separately named variable on purpose: the single opt-in that
    was already set is exactly what was set when the accident happened, so
    reusing it would make this a formality.
    """
    monkeypatch.setenv(_isolation().OVERRIDE_ENV, "1")
    assert not _isolation().targets_production(PRODUCTION), (
        "the explicit override must permit the known production ports, or there "
        "is no way to run the suite against a live platform at all"
    )


def test_no_environment_means_no_discovery_either(monkeypatch, no_network):
    """The original two-factor opt-in still holds, and is checked before the request."""
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("PERMA_ALLOW_LIVE_VIEWER_WRITES", raising=False)
    name = "integration_suite_probe_noenv"
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(name, _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    assert no_network == [], "with no BASE_URL there is nothing to contact"
    assert module.STRATEGY_TEST_MATRIX == []
