"""Alembic environment. The database URL comes from SwarmOS settings (SWARMOS_DATABASE_URL)."""

from alembic import context

from swarmos import models  # noqa: F401  (register tables)
from swarmos.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    from swarmos.config import get_settings

    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection") or get_engine()
    if hasattr(connectable, "connect") and not hasattr(connectable, "dialect_options"):
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
