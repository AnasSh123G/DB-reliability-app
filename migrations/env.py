"""Alembic environment.

render_as_batch is the load-bearing setting: SQLite can RENAME and DROP COLUMN
natively but cannot alter a constraint, and batch mode implements the
create-new / copy / drop / rename dance correctly. Hand-writing that against a
500 MB file with a NOT NULL foreign key on station_stop.feed_version_id is
exactly the job this dependency exists to avoid.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from dbrel.config import DB_PATH
from dbrel.db import Base
import dbrel.models  # noqa: F401  (registers every mapper)

config = context.config
config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{DB_PATH}")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
