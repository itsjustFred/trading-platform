"""Deterministic engine/config initialization tests (Cleanup 20-D).

Importing shared.database must not create a DB engine bound to a
process-specific DATA_DIR, and .env must be loaded at exactly one site so
every process resolves identical configuration.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_database_import_does_not_create_engine(monkeypatch, tmp_path: Path) -> None:
    """Importing shared.database must not eagerly create a SQLAlchemy engine."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import shared.database as database

    monkeypatch.setattr(database, "_ENGINE", None)

    created: list[str] = []

    class _RecordingEngine:
        def __init__(self, url: str, **_kwargs) -> None:
            self.url = url
            created.append(url)

    monkeypatch.setattr(database, "create_engine", _RecordingEngine)
    monkeypatch.setattr(database, "event", SimpleNamespace(listen=lambda *a, **k: None))

    # No engine may exist before the first get_engine() call...
    assert database._ENGINE is None
    assert not created
    # ...and the first call creates exactly one.
    engine = database.get_engine()
    assert created == [engine.url]


def test_database_engine_is_lazy_and_cached(monkeypatch, tmp_path: Path) -> None:
    """The engine is created on first access and memoized for the process."""
    import shared.database as database

    first = database.get_engine()
    second = database.get_engine()
    assert first is second


def test_database_engine_reflects_data_dir_override(monkeypatch, tmp_path: Path) -> None:
    """Changing DATA_DIR via set_data_dir must move the DB path AND the engine.

    DB_PATH and the active engine must never disagree about the SQLite file:
    set_data_dir disposes the cached engine so the next get_engine() binds to
    the new path.
    """
    import shared.constants as constants
    import shared.database as database

    engine_before = database.get_engine()
    constants.set_data_dir(tmp_path)
    try:
        assert database.DB_PATH == tmp_path / "backtests.db"
        engine_after = database.get_engine()
        assert engine_after is not engine_before, "stale engine must be replaced"
        assert engine_after.url.render_as_string(hide_password=False) == (
            f"sqlite:///{(tmp_path / 'backtests.db').as_posix()}"
        )
        with database.get_session() as session:
            assert str(session.bind.url.render_as_string(hide_password=False)) == (
                f"sqlite:///{(tmp_path / 'backtests.db').as_posix()}"
            )
    finally:
        constants.reset_data_dir()


def test_database_engine_honors_environment_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit DATABASE_URL wins over the derived sqlite path."""
    import shared.database as database

    url = f"sqlite:///{(tmp_path / 'explicit.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = database._create_database_engine()
    assert engine.url.render_as_string(hide_password=False) == url


def test_only_constants_loads_dotenv(monkeypatch, tmp_path: Path) -> None:
    """shared.constants is the single .env load site; sentry/database must not reload."""
    import shared.database as database
    import shared.sentry as sentry

    assert getattr(database, "load_dotenv", None) is None
    assert getattr(sentry, "load_dotenv", None) is None


def test_database_url_from_env_is_used_without_dotenv_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DATABASE_URL set before import is honored; import order must not matter."""
    import shared.database as database

    url = f"sqlite:///{(tmp_path / 'explicit2.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert database._create_database_engine().url.render_as_string(hide_password=False) == url


def test_data_dir_resolution_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    """Same configuration -> same DATA_DIR, regardless of filesystem contents."""
    import shared.constants as constants

    empty = tmp_path / "empty"
    empty.mkdir()
    populated = tmp_path / "populated"
    (populated / "ohlcv").mkdir(parents=True)

    monkeypatch.setenv("DATA_DIR", str(empty))
    resolved_empty = constants._resolve_data_dir(str(empty))
    monkeypatch.setenv("DATA_DIR", str(populated))
    resolved_populated = constants._resolve_data_dir(str(populated))

    assert resolved_empty == empty.resolve()
    assert resolved_populated == populated.resolve()


def test_data_dir_relative_resolution_is_repo_anchored(tmp_path: Path, monkeypatch) -> None:
    """Relative DATA_DIR values resolve against the repo root in every process."""
    import shared.constants as constants

    monkeypatch.setenv("DATA_DIR", "data-local")
    resolved = constants._resolve_data_dir("data-local")
    assert resolved == (constants.PROJECT_ROOT / "data-local").resolve()


def test_reset_data_dir_restores_environment_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    """reset_data_dir() re-resolves DATA_DIR from the environment."""
    import shared.constants as constants

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    constants.set_data_dir(tmp_path / "override")
    assert constants.DATA_DIR == (tmp_path / "override").resolve()
    constants.reset_data_dir()
    assert constants.DATA_DIR == tmp_path.resolve()
