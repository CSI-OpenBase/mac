"""Database engine construction and Alembic integration.

Credentials are accepted by the caller and kept in process memory only.  This
module deliberately has no environment-variable or configuration-file fallback
for passwords.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import TYPE_CHECKING, Iterator

from sqlalchemy import Engine, URL, create_engine, inspect as sqlalchemy_inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from .config import Settings

from .schema_contract import (
    STARTUP_REQUIRED_TABLES,
    validate_startup_schema_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MIGRATION_ROOT = Path(__file__).resolve().parent / "_migrations"
DATABASE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Non-secret connection settings for the local MySQL database."""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    database: str = "csi_openbase"
    pool_size: int = 5
    max_overflow: int = 5
    pool_recycle: int = 1800

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("database host cannot be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("database port must be between 1 and 65535")
        if not self.user:
            raise ValueError("database user cannot be empty")
        if not self.database:
            raise ValueError("database name cannot be empty")
        if not DATABASE_IDENTIFIER_RE.fullmatch(self.database):
            raise ValueError(
                "database name must start with a letter or underscore and contain "
                "only ASCII letters, digits, or underscores"
            )


def utc_now() -> datetime:
    """Return a timezone-naive UTC value suitable for MySQL DATETIME(6)."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_utc(value: datetime | None) -> datetime | None:
    """Normalize an aware datetime to naive UTC; naive inputs are already UTC."""

    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def build_mysql_url(config: DatabaseConfig, *, password: str) -> URL:
    """Build a SQLAlchemy URL without rendering the password as text."""

    if not password:
        raise ValueError("database password cannot be empty")
    return URL.create(
        "mysql+pymysql",
        username=config.user,
        password=password,
        host=config.host,
        port=config.port,
        database=config.database,
        query={"charset": "utf8mb4"},
    )


def create_database_engine(
    config: DatabaseConfig,
    *,
    password: str,
    echo: bool = False,
) -> Engine:
    """Create the synchronous application engine.

    ``pool_pre_ping`` handles a MySQL service restart, and ``pool_recycle``
    prevents stale connections after MySQL's idle timeout.
    """

    return create_engine(
        build_mysql_url(config, password=password),
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=config.pool_recycle,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        isolation_level="READ COMMITTED",
    )


def create_engine_from_settings(settings: Settings, *, echo: bool = False) -> Engine:
    """Create an engine from the application's in-memory settings object."""

    return create_database_engine(
        DatabaseConfig(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            database=settings.db_name,
        ),
        password=settings.db_password,
        echo=echo,
    )


def ensure_database_exists(settings: Settings) -> None:
    """Create the active workspace database before opening its application engine."""

    config = DatabaseConfig(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        database=settings.db_name,
    )
    server_url = URL.create(
        "mysql+pymysql",
        username=config.user,
        password=settings.db_password,
        host=config.host,
        port=config.port,
        query={"charset": "utf8mb4"},
    )
    server_engine = create_engine(server_url, poolclass=NullPool)
    try:
        with server_engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
    finally:
        server_engine.dispose()


def bind_workspace_identity(engine: Engine, settings: Settings) -> None:
    """Bind a database to exactly one workspace and reject cross-account reuse."""

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT workspace_slug, platform FROM workspace_identity "
                "WHERE singleton_id = 1 FOR UPDATE"
            )
        ).mappings().first()
        expected = (settings.workspace_slug, settings.platform)
        if row is None:
            connection.execute(
                text(
                    "INSERT INTO workspace_identity "
                    "(singleton_id, workspace_slug, platform) "
                    "VALUES (1, :workspace_slug, :platform)"
                ),
                {
                    "workspace_slug": settings.workspace_slug,
                    "platform": settings.platform,
                },
            )
            return
        actual = (str(row["workspace_slug"]), str(row["platform"]))
        if actual != expected:
            raise RuntimeError(
                f"database {settings.db_name!r} belongs to workspace "
                f"{actual[0]!r} ({actual[1]}), not {expected[0]!r} ({expected[1]})"
            )


def assert_existing_workspace_identity(engine: Engine, settings: Settings) -> None:
    """Reject an already-bound foreign database before running any migration."""

    inspector = sqlalchemy_inspect(engine)
    if "workspace_identity" not in set(inspector.get_table_names()):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("workspace_identity")}
    required = {"singleton_id", "workspace_slug", "platform"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "existing workspace_identity table is malformed; missing columns: "
            + ", ".join(missing)
        )
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT workspace_slug, platform FROM workspace_identity "
                "WHERE singleton_id = 1"
            )
        ).mappings().first()
    if row is None:
        return
    actual = (str(row["workspace_slug"]), str(row["platform"]))
    expected = (settings.workspace_slug, settings.platform)
    if actual != expected:
        raise RuntimeError(
            f"database {settings.db_name!r} belongs to workspace "
            f"{actual[0]!r} ({actual[1]}), not {expected[0]!r} ({expected[1]})"
        )


def verify_schema_contract(engine: Engine) -> None:
    """Fail at startup when a pre-existing table has an obsolete shape."""

    inspector = sqlalchemy_inspect(engine)
    table_names = set(inspector.get_table_names())
    actual_columns: dict[str, set[str]] = {}
    actual_unique_keys: dict[str, set[tuple[str, ...]]] = {}
    for table in STARTUP_REQUIRED_TABLES:
        if table not in table_names:
            continue
        actual_columns[table] = {
            str(column["name"]) for column in inspector.get_columns(table)
        }
        unique_keys: set[tuple[str, ...]] = set()
        primary = inspector.get_pk_constraint(table).get("constrained_columns") or []
        if primary:
            unique_keys.add(tuple(str(column) for column in primary))
        for constraint in inspector.get_unique_constraints(table):
            columns = constraint.get("column_names") or []
            if columns:
                unique_keys.add(tuple(str(column) for column in columns))
        for index in inspector.get_indexes(table):
            columns = index.get("column_names") or []
            if index.get("unique") and columns:
                unique_keys.add(tuple(str(column) for column in columns))
        actual_unique_keys[table] = unique_keys
    validate_startup_schema_contract(actual_columns, actual_unique_keys)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    """Yield one connection inside a committed-or-rolled-back transaction."""

    with engine.begin() as connection:
        yield connection


def upgrade_database(
    engine: Engine,
    *,
    revision: str = "head",
    alembic_ini: Path | None = None,
) -> None:
    """Run Alembic with an already-authenticated SQLAlchemy connection.

    Passing the connection through Alembic attributes avoids serializing a URL
    containing credentials into ``alembic.ini`` or process arguments.
    """

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:  # pragma: no cover - dependency setup failure
        raise RuntimeError("Alembic is required to run database migrations") from exc

    source_migrations = PROJECT_ROOT / "migrations"
    migration_root = (
        source_migrations if source_migrations.is_dir() else PACKAGE_MIGRATION_ROOT
    )
    ini_path = alembic_ini or (
        PROJECT_ROOT / "alembic.ini"
        if (PROJECT_ROOT / "alembic.ini").is_file()
        else PACKAGE_MIGRATION_ROOT / "alembic.ini"
    )
    if not ini_path.is_file() or not migration_root.is_dir():
        raise RuntimeError("CSI OpenBase database migration resources are missing")
    alembic_config = Config(str(ini_path))
    alembic_config.set_main_option(
        "script_location", str(migration_root)
    )
    with engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, revision)


def initialize_database_engine(
    settings: Settings, *, echo: bool = False, migrate: bool = True
) -> Engine:
    """Create, migrate, bind, and return the active workspace database engine."""

    ensure_database_exists(settings)
    engine = create_engine_from_settings(settings, echo=echo)
    try:
        if migrate:
            assert_existing_workspace_identity(engine, settings)
            upgrade_database(engine)
            verify_schema_contract(engine)
            bind_workspace_identity(engine, settings)
    except Exception:
        engine.dispose()
        raise
    return engine
