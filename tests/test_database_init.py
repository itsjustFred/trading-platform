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
