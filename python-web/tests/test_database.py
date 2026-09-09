from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

import admin_app.database as database_module
import admin_app.main as main_module
from admin_app.config import Settings


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _settings(tmp_path: Path, *, workspace_slug: str = "creator-one") -> Settings:
    return Settings(
        repository_root=tmp_path,
        workspace_slug=workspace_slug,
        workspace_dir=tmp_path / workspace_slug,
        db_name="creator_one",
        db_password="database-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )


def test_initialize_database_engine_runs_preflight_before_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    engine = DisposableEngine()
    events: list[str] = []

    monkeypatch.setattr(
        database_module,
        "ensure_database_exists",
        lambda actual: events.append("ensure") if actual is settings else None,
    )

    def create_engine(actual, *, echo=False):
        assert actual is settings
        assert echo is False
        events.append("create")
        return engine

    monkeypatch.setattr(database_module, "create_engine_from_settings", create_engine)
    monkeypatch.setattr(
        database_module,
        "assert_existing_workspace_identity",
        lambda actual_engine, actual_settings: events.append("preflight"),
    )
    monkeypatch.setattr(
        database_module,
        "upgrade_database",
        lambda actual_engine: events.append("upgrade"),
    )
    monkeypatch.setattr(
        database_module,
        "verify_schema_contract",
        lambda actual_engine: events.append("verify"),
    )
    monkeypatch.setattr(
        database_module,
        "bind_workspace_identity",
        lambda actual_engine, actual_settings: events.append("bind"),
    )

    returned = database_module.initialize_database_engine(settings)

    assert returned is engine
    assert events == ["ensure", "create", "preflight", "upgrade", "verify", "bind"]
    assert engine.disposed is False


def test_foreign_existing_workspace_identity_blocks_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, workspace_slug="expected-creator")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE workspace_identity ("
            "singleton_id INTEGER PRIMARY KEY, "
            "workspace_slug VARCHAR(80) NOT NULL, "
            "platform VARCHAR(32) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO workspace_identity "
            "(singleton_id, workspace_slug, platform) "
            "VALUES (1, 'different-creator', 'douyin')"
        )
    upgraded: list[object] = []
    monkeypatch.setattr(database_module, "ensure_database_exists", lambda _: None)
    monkeypatch.setattr(
        database_module, "create_engine_from_settings", lambda *_args, **_kwargs: engine
    )
    monkeypatch.setattr(
        database_module, "upgrade_database", lambda actual: upgraded.append(actual)
    )

    with pytest.raises(RuntimeError, match="belongs to workspace 'different-creator'"):
        database_module.initialize_database_engine(settings)

    assert upgraded == []


def test_create_app_with_external_engine_uses_full_migration_safety_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    engine = DisposableEngine()
    events: list[str] = []
    monkeypatch.setattr(
        main_module,
        "assert_existing_workspace_identity",
        lambda actual_engine, actual_settings: events.append("preflight"),
    )
    monkeypatch.setattr(
        main_module,
        "upgrade_database",
        lambda actual_engine: events.append("upgrade"),
    )
    monkeypatch.setattr(
        main_module,
        "verify_schema_contract",
        lambda actual_engine: events.append("verify"),
    )
    monkeypatch.setattr(
        main_module,
        "bind_workspace_identity",
        lambda actual_engine, actual_settings: events.append("bind"),
    )

    app = main_module.create_app(
        settings,
        engine=engine,
        migrate=True,
        start_worker=False,
    )

    assert app.state.engine is engine
    assert events == ["preflight", "upgrade", "verify", "bind"]
    assert engine.disposed is False
