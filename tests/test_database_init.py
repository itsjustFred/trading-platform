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
