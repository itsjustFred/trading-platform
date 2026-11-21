"""The scalar stat columns must hold what the code computed, and absence must not destroy data.

`backtests` has carried `sharpe_ratio`, `psr`, `cagr` and `p_value` for some time - all `NOT NULL`,
with defaults `0.0/0.0/0.0/1.0` - but `BacktestRecord` never mapped them. Two consequences, both
live and both found by measurement rather than by reading:

- **Every insert took `DEFAULT 0.0`.** The fabricated `0.0` that a corpus audit kept finding across
  ~2,600 rows was *manufactured by the schema*, not produced by the legacy
  ``_stat_number_or_undefined`` backward-compatibility path the audit blamed.
- **`session.merge()` updates mapped columns only.** So re-saving a backtest with freshly computed
  stats left the scalar column at its old value *forever*, while the JSON `stats` blob - which is
  mapped - was updated. The two could therefore disagree permanently, by construction.

The fix maps the four columns and populates them on save. The part worth testing carefully is what
happens when a metric is **not** defined, because the columns are `NOT NULL` and so cannot record
absence at all. Letting the model's default land would mean a re-save whose Sharpe is no longer
computable overwrites a real stored value with `0.0` - destroying data to express absence, which is
the same failure the audit found in the first place, reintroduced by the fix for it. So an undefined
metric **carries its current column value forward**, and only a number moves the column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlmodel import select

import shared.database as database
from shared.models import BacktestMeta, BacktestResult

COLUMNS = ("sharpe_ratio", "psr", "cagr", "p_value")


def _result(backtest_id: str, stats: dict) -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(id=backtest_id, strategies=["s"], engine="native",
                          created_at="2026-10-04"),
        stats=stats,
        trades=[],
        equity_curve=[],
    )


def _save(backtest_id: str, stats: dict) -> None:
    database.save_result_to_sqlite(_result(backtest_id, stats))


def _columns(backtest_id: str) -> dict[str, float]:
    with database.get_session() as session:
        row = session.exec(
            select(*(getattr(database.BacktestRecord, name) for name in COLUMNS))
            .where(database.BacktestRecord.id == backtest_id)
        ).first()
    return dict(zip(COLUMNS, row, strict=True))


def test_the_four_columns_are_actually_mapped() -> None:
    """The precondition for everything else, and the thing that was silently absent.

    Asserted on the model's field set rather than on a save, because the failure this guards is
    *absence*: a test that only checked behaviour after the fix would have passed just as well with
    the columns unmapped and every value arriving from the schema default.
    """
    mapped = set(database.BacktestRecord.model_fields)
    for name in COLUMNS:
        assert name in mapped, (
            f"{name} exists in the physical table but is not mapped on BacktestRecord, so no code "
            "path can ever write it: inserts take the schema default and merges leave it stale"
        )


def test_a_defined_stat_reaches_the_column_and_agrees_with_the_json(isolated_db: tuple) -> None:
    """The staleness bug, end to end.

    Before the fix the JSON said 1.2345 and the column said 0.0, permanently, for the life of the
    row. Asserting the agreement - not merely that the column is non-zero - is what makes this a
    test of the bug rather than of the absence of an exception.
    """
    _save("defined-1", {"sharpe_ratio": 1.2345, "psr": 0.5, "cagr": 0.11, "p_value": 0.03})
    values = _columns("defined-1")
    assert values == {"sharpe_ratio": 1.2345, "psr": 0.5, "cagr": 0.11, "p_value": 0.03}, values

    with database.get_session() as session:
        record = session.exec(
            select(database.BacktestRecord).where(database.BacktestRecord.id == "defined-1")
        ).first()
    stored = record.stats
    assert stored["sharpe_ratio"] == values["sharpe_ratio"], (
        "the scalar column and the JSON blob now disagree, which is the original bug in a new place"
    )


def test_re_saving_updates_the_column_rather_than_leaving_it_stale(isolated_db: tuple) -> None:
    """The other half: a *changed* number must land.

    A column that is written on insert but never updated is the stale half of the same defect, and
    it is the half a caller would notice last.
    """
    _save("stale-1", {"sharpe_ratio": 0.5})
    _save("stale-1", {"sharpe_ratio": -1.25})
    assert _columns("stale-1")["sharpe_ratio"] == -1.25, _columns("stale-1")


def test_an_undefined_metric_carries_the_stored_value_forward(isolated_db: tuple) -> None:
    """**The load-bearing case.**

    The columns are NOT NULL, so "undefined" cannot be stored. If the model's default were allowed
    to land, this re-save would replace a real ``-0.9`` with ``0.0`` - losing data to express
    absence. The value must survive.
    """
    _save("carried-1", {"sharpe_ratio": -0.9, "psr": 0.4})
    _save("carried-1", {"psr": 0.4, "undefined_metrics": ["sharpe_ratio"]})
    assert _columns("carried-1")["sharpe_ratio"] == -0.9, (
        "an undefined metric overwrote a real stored value with the default; absence must "
        "leave the column alone rather than destroying what was there"
    )


def test_a_metric_the_producer_never_named_also_leaves_the_column_alone(
    isolated_db: tuple,
) -> None:
    """The second route to "no number", and it is a different one.

    ``undefined_metrics`` is the explicit signal. A metric simply absent from ``stats`` is a
    legacy artifact written before the convention existed - ``resolve_defined_stat`` documents
    that its default applies only to a metric the producer did not name, precisely so old
    artifacts keep their values. Both routes must leave the column alone.
    """
    _save("absent-1", {"sharpe_ratio": 0.75})
    _save("absent-1", {"psr": 0.7})
    values = _columns("absent-1")
    assert values["sharpe_ratio"] == 0.75, values
    assert values["psr"] == 0.7, values


def test_a_non_finite_number_is_treated_as_undefined(isolated_db: tuple) -> None:
    """NaN and infinity are number-shaped but cannot be stored honestly.

    The columns are ``NOT NULL`` REAL, so ``float('nan')`` commits without complaint and then
    poisons every later read: ``nan > 0`` is False, ``nan == nan`` is False, and JSON built from the
    row is invalid. ``math.isfinite`` is the only check that catches it, because it *is* a float. So
    it counts as absence and the stored value is carried forward - never destroyed, never poisoned.
    """
    _save("nonfinite-1", {"sharpe_ratio": -0.9, "psr": 0.4})
    _save("nonfinite-1", {"sharpe_ratio": float("nan"), "psr": float("inf")})
    values = _columns("nonfinite-1")
    assert values["sharpe_ratio"] == -0.9, values
    assert values["psr"] == 0.4, values


def test_an_unparsable_value_is_treated_as_undefined(isolated_db: tuple) -> None:
    """A producer that emits ``"n/a"`` must not take the whole save down, nor fabricate a 0.0.

    Failing the insert would lose the entire backtest over one malformed field; coercing to ``0.0``
    would assert a Sharpe of zero the producer never computed. Carrying the stored value forward is
    the same rule as for a metric the producer never named, and the warning log is what makes the
    producer bug visible instead of silent.
    """
    _save("junk-1", {"sharpe_ratio": 0.8})
    _save("junk-1", {"sharpe_ratio": "n/a"})
    assert _columns("junk-1")["sharpe_ratio"] == 0.8, _columns("junk-1")


def test_resolve_defined_stat_distinguishes_the_three_routes_to_no_number() -> None:
    """The helper every caller relies on, tested where it is defined rather than only via a save.

    Three routes must stay distinguishable: a value the producer computed, a value it declared
    undefined, and a key it never mentioned. Conflating any two is what the convention exists to
    prevent - an `undefined_metrics` list must not swallow the metrics *not* named in it, and an
    absent key must resolve to the caller's default rather than to `None`.
    """
    from shared.models import resolve_defined_stat

    computed = {"sharpe_ratio": 1.5, "undefined_metrics": ["psr"]}
    assert resolve_defined_stat(computed, "sharpe_ratio") == 1.5, (
        "a metric the producer computed was treated as undefined because another one was"
    )
    assert resolve_defined_stat({"psr": 0.4, "undefined_metrics": ["psr"]}, "psr", 0.9) is None
    assert resolve_defined_stat({"psr": 0.4}, "cagr", 0.7) == 0.7, (
        "a key the producer never mentioned must resolve to the caller's default"
    )
    assert resolve_defined_stat({"psr": None}, "psr", 0.7) == 0.7
    assert resolve_defined_stat(None, "psr", 0.7) == 0.7
    assert resolve_defined_stat(["not", "a", "dict"], "psr", 0.7) == 0.7


def test_p_value_defaults_to_one_not_zero(isolated_db: tuple) -> None:
    """``1.0`` means "no evidence against the null hypothesis"; ``0.0`` means the opposite.

    So a p-value that was never computed must not land on ``0.0`` - that would assert significance
    the backtest never established. This is why ``p_value`` is the one column whose physical default
    is not ``0.0``, and why the carry-forward path has to preserve it.
    """
    _save("pvalue-1", {"psr": 0.3})
    assert _columns("pvalue-1")["p_value"] == 1.0, _columns("pvalue-1")


def test_no_null_is_ever_written_into_a_not_null_column(isolated_db: tuple) -> None:
    """Belt and braces against the NOT NULL constraint.

    The carry-forward rule exists so this never comes up, but a future change that writes ``None``
    would fail at commit time with an opaque IntegrityError rather than at the point of the mistake.
    """
    _save("null-1", {"sharpe_ratio": 1.0, "undefined_metrics": ["psr", "cagr", "p_value"]})
    for name, value in _columns("null-1").items():
        assert value is not None, f"{name} was written as NULL"

    conn = sqlite3.connect(f"file:{Path(isolated_db[2]) / 'backtests.db'}?mode=ro", uri=True)
    try:
        nulls = [
            name for name in COLUMNS
            if conn.execute(
                f"SELECT count(*) FROM backtests WHERE {name} IS NULL"  # noqa: S608 - fixed names
            ).fetchone()[0]
        ]
    finally:
        conn.close()
    assert not nulls, f"NULL written into NOT NULL column(s): {nulls}"


@pytest.mark.parametrize("metric", COLUMNS)
def test_every_column_follows_the_same_rule(isolated_db: tuple, metric: str) -> None:
    """All four behave identically, rather than the rule being verified only for Sharpe.

    Sharpe is the one everybody looks at, which is exactly how the other three would have drifted.
    """
    _save(f"each-{metric}", {metric: 2.5})
    _save(f"each-{metric}", {"undefined_metrics": [metric]})
    assert _columns(f"each-{metric}")[metric] == 2.5, _columns(f"each-{metric}")


def test_the_migration_adds_the_columns_to_a_database_that_lacks_them(
    isolated_db: tuple,
) -> None:
    """The migration these columns should always have had.

    Every case above runs through `create_all`, which - now that the model maps the columns - emits
    them by itself. So nothing here would notice if the migration were missing, and on any database
    built from the model *before* this change (a fresh install, a sandbox, a fixture) the columns
    genuinely do not exist, so every `SELECT backtests.*` fails. That is exactly how the gap
    surfaced: two suites failing with `no such column: backtests.sharpe_ratio`.

    The table is therefore rebuilt by hand, without the columns, and the migration run against it.
    """
    db_path = Path(database.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE backtests")
        conn.execute("CREATE TABLE backtests (id TEXT PRIMARY KEY, stats TEXT)")
        conn.commit()
        before = {r[1] for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    for name in COLUMNS:
        assert name not in before, f"{name} was not actually absent, so this case proves nothing"

    with database.get_engine().connect() as writable:
        database._add_backtests_stat_columns(writable)
        writable.commit()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        after = {r[1]: (r[2], r[3], r[4]) for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    for name in COLUMNS:
        assert name in after, f"the migration did not add {name}"
        _type, notnull, _default = after[name]
        assert notnull == 1, f"{name} should be NOT NULL, got notnull={notnull}"
    # p_value must not come out as 0.0: that asserts significance never established.
    # SQLite reports a server_default with its quotes intact, as the text "'1.0'".
    assert str(after["p_value"][2]).strip("'") == "1.0", after["p_value"]
    assert str(after["sharpe_ratio"][2]).strip("'") == "0.0", after["sharpe_ratio"]


def test_the_migration_is_a_no_op_when_the_columns_are_present(isolated_db: tuple) -> None:
    """Re-running must not raise, because `init_db` runs on every process start."""
    db_path = Path(database.DB_PATH)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'backtests'"
        ).fetchone()[0]
    finally:
        conn.close()
    for name in COLUMNS:
        assert name in ddl, f"{name} is missing from the fixture schema, so this proves nothing"

    with database.get_engine().connect() as writable:
        database._add_backtests_stat_columns(writable)
        writable.commit()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        after = {r[1] for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    assert after >= set(COLUMNS), sorted(after)


def test_init_db_wires_the_migration_in(isolated_db: tuple) -> None:
    """The wiring, not the helper.

    My first version called `_add_backtests_stat_columns` directly, so removing the call from
    `_migrate_schema` left every test in this file passing - a migration nothing invokes, which is
    the shape of thing this whole file exists to catch. Found by mutation.

    So this goes through the real entry point, on a `backtests` table lacking the columns, exactly
    as an old database or a fresh sandbox would have.
    """
    db_path = Path(database.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE backtests")
        conn.execute("CREATE TABLE backtests (id TEXT PRIMARY KEY, stats TEXT)")
        conn.commit()
    finally:
        conn.close()

    database._migrate_schema()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        after = {r[1] for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    missing = [name for name in COLUMNS if name not in after]
    assert not missing, (
        f"_migrate_schema did not add {missing}, so the migration exists but nothing calls it"
    )


def test_create_all_alone_declares_the_server_defaults(tmp_path: Path) -> None:
    """A fresh install must match production *before* any migration runs.

    `Field(default=0.0)` is a Python-side default only, so without an explicit `server_default`
    `create_all` emits `NOT NULL` columns with **no database default** - a schema that diverges from
    production's, where the columns carry `DEFAULT 0.0/0.0/0.0/1.0`. Any INSERT not going through
    the ORM then fails outright. Asserted on `create_all` alone, with no migration in the way,
    because that is the schema a new database gets.
    """
    from sqlmodel import SQLModel, create_engine

    db_file = tmp_path / "create_all_only.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        info = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    for name in COLUMNS:
        assert name in info, f"create_all did not emit {name}"
        assert info[name] is not None, (
            f"{name} has no database-level default; a fresh install would diverge from production "
            "and any non-ORM INSERT would fail"
        )
    # SQLite reports a server_default with its quotes intact, as the text "'1.0'".
    strip = lambda v: str(v).strip("'") if v is not None else None  # noqa: E731
    assert strip(info["p_value"]) == "1.0", info["p_value"]
    assert strip(info["sharpe_ratio"]) == "0.0", info["sharpe_ratio"]
