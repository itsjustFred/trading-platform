from __future__ import annotations

from pathlib import Path


def test_init_db_runs_schema_setup_once(monkeypatch, tmp_path: Path) -> None:
    import shared.database as database

    calls = {"create_all": 0, "migrate": 0}

    def fake_create_all(*_args, **_kwargs):
        calls["create_all"] += 1

    def fake_migrate_schema() -> None:
        calls["migrate"] += 1

    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database.SQLModel.metadata, "create_all", fake_create_all)
    monkeypatch.setattr(database, "_migrate_schema", fake_migrate_schema)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    database.init_db()
    database.init_db()

    assert calls == {"create_all": 1, "migrate": 1}


def test_undefined_metrics_survive_the_metadata_payload() -> None:
    """runner/stats.py names what it could not compute; the DB must not fill it in.

    Substituting a default here turned an undefined PSR into 0.0, which is the
    *worst* attainable PSR, so a backtest with no trades at all was listed as
    "PSR (higher = better) 0.0%" and read as a measured result. p_value had the
    same shape, defaulting to 1.0 -- maximum confidence in the null.
    """
    import shared.database as database

    stats = {"sharpe_ratio": 1.5, "undefined_metrics": ["psr", "p_value", "sharpe_ratio"]}
    assert database._stat_number_or_undefined(stats, "psr", 0.0) is None
    assert database._stat_number_or_undefined(stats, "p_value", 1.0) is None
    # Named as undefined even though a stale value is present: the producer is the
    # only party that knows whether the number is missing or genuinely zero.
    assert database._stat_number_or_undefined(stats, "sharpe_ratio", 0.0) is None

    # A metric the producer did not name keeps its value, and a real zero is a
    # measurement, not an absence.
    measured = {"psr": 0.0, "undefined_metrics": ["p_value"]}
    assert database._stat_number_or_undefined(measured, "psr", 0.0) == 0.0
    assert database._stat_number_or_undefined(measured, "p_value", 1.0) is None

    # Artifacts written before the convention existed carry no list at all and
    # must keep their historical values.
    legacy = {"psr": 0.75}
    assert database._stat_number_or_undefined(legacy, "psr", 0.0) == 0.75
    assert database._stat_number_or_undefined(legacy, "p_value", 1.0) == 1.0
    assert database._stat_number_or_undefined({}, "psr", 0.0) == 0.0


def test_daemon_heartbeat_upserts_and_reports_liveness(isolated_db) -> None:
    """A paused platform is silent, so "no activity" is ambiguous.

    The daemon publishing itself into the services table is what makes "running
    but idle" separable from "dead" for anyone reading the UI. The upsert has to
    work on the first beat, because update_service_status needs an existing row.
    """
    import shared.database as database

    first = database.record_daemon_heartbeat(detail={"connected_adapters": 2})
    assert str(first.id) == database.TRADING_DAEMON_SERVICE_ID
    assert first.name == "trading-daemon"
    assert first.type.value == "trading_daemon"
    assert first.last_activity is not None
    assert first.config["connected_adapters"] == 2

    # A second beat updates the same row rather than creating a second one.
    second = database.record_daemon_heartbeat(detail={"connected_adapters": 0})
    assert second.id == first.id
    assert second.last_activity >= first.last_activity
    assert second.config["connected_adapters"] == 0

    from shared.database import list_services

    rows = [
        s for s in list_services() if str(s.id) == database.TRADING_DAEMON_SERVICE_ID
    ]
    assert len(rows) == 1, "the heartbeat must upsert, not accumulate rows"


def test_daemon_heartbeat_failure_cannot_stop_the_daemon(monkeypatch) -> None:
    """A heartbeat that can take the daemon down is worse than no heartbeat.

    The component reporting on liveness must not be able to fail because of it,
    so the loop swallows a database error and keeps going. Asserted by letting it
    survive several failing beats, not by inspecting cancellation semantics.
    """
    import asyncio

    import live_runner.daemon as daemon

    beats: list[int] = []

    # Plain sync, like the real record_daemon_heartbeat: it is called *through*
    # asyncio.to_thread, which invokes it synchronously in a worker. An async
    # double would return an un-awaited coroutine and never run at all.
    def _boom(**_kwargs):
        beats.append(1)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(daemon, "record_daemon_heartbeat", _boom)
    # A short interval so the loop actually iterates inside the test.
    monkeypatch.setattr(daemon, "DAEMON_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def _exercise() -> int:
        task = asyncio.ensure_future(daemon._daemon_heartbeat_loop({}))
        deadline = 0.0
        while len(beats) < 3 and deadline < 2.0:
            await asyncio.sleep(0.01)
            deadline += 0.01
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(beats)

    assert asyncio.run(_exercise()) >= 3, (
        "the loop must survive repeated database failures rather than exiting on "
        "the first one"
    )


def test_live_control_audit_records_success_and_refusal(isolated_db) -> None:
    """Arming and stopping a live deployment had no durable trace at all.

    The request log is a debug line: not queryable, not durable across rotation,
    and it cannot answer "what changed and when". Refused attempts matter as much
    as applied ones -- a gate that stopped something is the interesting event.
    """
    import shared.database as database

    database.record_live_control_audit(
        action="deployment.status",
        target_type="live_deployment",
        target_id="dep-1",
        actor="operator@example",
        detail={"requested_status": "ACTIVE"},
    )
    database.record_live_control_audit(
        action="deployment.status",
        target_type="live_deployment",
        target_id="dep-1",
        actor="operator@example",
        succeeded=False,
        detail={"reason": "broker could not confirm a demo account"},
    )

    rows = database.list_live_control_audit(target_id="dep-1")
    assert len(rows) == 2
    assert {r["succeeded"] for r in rows} == {True, False}
    assert all(r["actor"] == "operator@example" for r in rows)
    actions = {r["action"] for r in rows}
    assert actions == {"deployment.status"}
    # A refusal must keep its reason, or it is not actionable.
    refusal = next(r for r in rows if not r["succeeded"])
    assert "broker" in refusal["detail"]["reason"]


def test_live_control_audit_is_scoped_and_bounded(isolated_db) -> None:
    import shared.database as database

    for index in range(5):
        database.record_live_control_audit(
            action="deployment.status",
            target_type="live_deployment",
            target_id=f"dep-{index % 2}",
        )
    assert len(database.list_live_control_audit(target_id="dep-0")) == 3
    assert len(database.list_live_control_audit(target_id="dep-1")) == 2
    # The limit is clamped, never trusted.
    assert len(database.list_live_control_audit(limit=10_000)) <= 1000
    assert len(database.list_live_control_audit(limit=0)) >= 1


def test_live_control_audit_survives_a_broken_database(isolated_db, monkeypatch) -> None:
    """An audit write that could abort the operation it records is worse than
    no record, so a failure here is swallowed and the caller carries on."""
    import shared.database as database

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "init_db", _boom)
    # Must not raise: the state change it describes still stands.
    payload = database.record_live_control_audit(
        action="deployment.delete", target_type="live_deployment", target_id="dep-9"
    )
    assert payload["action"] == "deployment.delete"
    assert payload["target_id"] == "dep-9"


def test_the_demo_identity_gate_compares_in_constant_time() -> None:
    """`hmac.compare_digest`, not `==` or `!=`.

    The fingerprint is what stands between a re-pointed credential and an armed account,
    so the comparison is a security boundary. `==` and `!=` on strings short-circuit on
    the first differing byte, which leaks how much of a hash matched; `hmac.compare_digest`
    does not. Replacing it with `!=` changes no test-visible behaviour and every test in
    the suite stays green - so this is pinned **structurally**, by inspecting the AST
    rather than by asserting a value.

    An AST pin because there is no behavioural assertion available: a timing property
    cannot be observed from a test without becoming a timing test, which would be flaky in
    the opposite direction. The repo already uses this pattern elsewhere for exactly that
    reason.

    The mutation report records `stored != current` as an equivalent mutant for
    behaviour; this test is what makes it *not* equivalent as a security claim.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "shared" / "database.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Every call site, not just the one this test was written against. There are two -
    # `_live_account_fingerprint_mismatch_reason` (read-side gate) and
    # `_invalidate_verification_for_changed_identity` (write-side invalidation) - and
    # pinning one while the other went to `==` is exactly the single-implementation trap
    # this whole sweep keeps finding. Mutating the second one survives the suite.
    compared_sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "hmac"
            ):
                compared_sites.append((node.name, call.func.attr))

    assert compared_sites, "no hmac call sites found; has the module moved?"
    plain = [(name, attr) for name, attr in compared_sites if attr != "compare_digest"]
    assert not plain, (
        "these sites no longer use hmac.compare_digest: " + repr(plain)
    )

    # Both fingerprint call sites must still be there, so this cannot pass by the
    # comparisons being deleted outright.
    for required in (
        "_live_account_fingerprint_mismatch_reason",
        "_invalidate_verification_for_changed_identity",
    ):
        assert any(name == required for name, _ in compared_sites), (
            f"{required} no longer compares a fingerprint in constant time: "
            + repr(compared_sites)
        )

    # And the guard is still the *negative* one: mismatch means refuse.
    gate_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_live_account_fingerprint_mismatch_reason"
    )
    mismatch_branches = [
        node
        for node in ast.walk(gate_fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
    ]
    assert mismatch_branches, (
        "the mismatch guard is gone; a changed broker identity would pass the demo gate"
    )

    # Two sibling branches in that function are **equivalent mutants**, verified rather
    # than assumed, and deliberately left unpinned:
    #
    # - the `except Exception` around the recomputation. Hashing a `str` cannot raise, and
    #   `live_account_identity_fingerprint` is a `sha256(payload.encode()).hexdigest()`
    #   with nothing else in it. The docstring on that function says as much.
    # - the `if not current` branch. `hexdigest()` is always a 64-character string, so
    #   `current` is never empty - including for all-blank inputs, which hash to a real
    #   digest.
    #
    # Both are fail-closed guards on conditions that cannot occur, so removing either
    # changes nothing observable. Recorded here rather than given tests, because a test
    # asserting an unreachable branch asserts nothing.
    source_text = ast.unparse(gate_fn)
    assert "if not current:" in source_text, (
        "the empty-fingerprint guard was removed; it is currently unreachable (a "
        "hexdigest is never empty), but it documents the invariant"
    )


def test_resolve_defined_stat_separates_absent_from_measured_zero() -> None:
    """One definition of the convention, used by both the DB and the viewer.

    The bug this exists to prevent appeared twice, each time because a default
    was applied somewhere new: once for PSR, where `.get(key, 0.0)` published
    the *worst* attainable value for a metric that was never computed, and once
    for the portfolio combined stats.
    """
    from shared.models import resolve_defined_stat

    # Named as undefined: absent, even though a stale value is present.
    assert resolve_defined_stat({"sharpe": 1.0, "undefined_metrics": ["sharpe"]}, "sharpe", 0) is None
    # A real zero is a measurement and must survive.
    assert resolve_defined_stat({"sharpe": 0.0}, "sharpe", 0) == 0.0
    # Pre-convention artifacts carry no list and keep their historical default.
    assert resolve_defined_stat({}, "sharpe", 0) == 0
    assert resolve_defined_stat({"sharpe": 2.5}, "sharpe", 0) == 2.5
    # An explicit null is an absence too, not a zero.
    assert resolve_defined_stat({"sharpe": None}, "sharpe", 0) == 0
    assert resolve_defined_stat({"sharpe": None}, "sharpe") is None
    # A non-dict payload must not explode.
    assert resolve_defined_stat(None, "sharpe", 0) == 0


def test_portfolio_stats_defaults_are_absent_not_zero() -> None:
    """PortfolioStats used to default every metric to 0.0.

    A portfolio that could not be scored then reported a measured 0.0 Sharpe and
    a measured 0.0% max drawdown, which the UI rendered as real figures. The
    fields are now nullable so absence is representable at all.
    """
    from shared.models import PortfolioStats

    empty = PortfolioStats()
    assert empty.sharpe is None
    assert empty.sortino is None
    assert empty.cagr is None
    assert empty.max_drawdown is None
    assert empty.calmar is None
    # Genuinely-counted quantities keep their zero default: no trades really does
    # mean no profit, and that is a fact rather than an absence.
    assert empty.net_profit == 0.0
    assert empty.total_return == 0.0
    assert empty.trade_count if hasattr(empty, "trade_count") else True

    payload = empty.to_dict()
    assert payload["sharpe"] is None
    assert payload["max_drawdown"] is None
    assert payload["net_profit"] == 0.0
