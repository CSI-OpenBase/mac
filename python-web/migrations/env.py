"""Alembic environment restricted to administration-owned tables."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from admin_app.models import AdminBase


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = AdminBase.metadata
MANAGED_TABLES = frozenset({"collection_jobs", "collection_schedules"})


def include_object(object_, name, type_, reflected, compare_to):
    """Keep autogenerate away from the separately versioned business tables."""

    if type_ == "table":
        return name in MANAGED_TABLES
    table = getattr(object_, "table", None)
    if table is not None:
        return table.name in MANAGED_TABLES
    return True


def configure(connection=None, *, url: str | None = None) -> None:
    options = {
        "target_metadata": target_metadata,
        "include_object": include_object,
        "compare_type": True,
        "compare_server_default": True,
        "render_as_batch": False,
    }
    if connection is not None:
        context.configure(connection=connection, **options)
    else:
        context.configure(
            url=url,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **options,
        )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url").strip()
    if not url:
        raise RuntimeError(
            "offline migrations require a non-secret sqlalchemy.url override"
        )
    configure(url=url)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        configure(supplied_connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    url = config.get_main_option("sqlalchemy.url").strip()
    if not url:
        raise RuntimeError(
            "no database connection supplied; use "
            "admin_app.database.upgrade_database(engine)"
        )
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
