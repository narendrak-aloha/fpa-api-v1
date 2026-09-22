"""Alembic environment for the FP&A governance store."""

from __future__ import annotations

import re
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool, text

from db.config import database_url
from db.models import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())

target_metadata = Base.metadata


def next_revision_id() -> str:
    # Revisions are numbered 001, 002, ... instead of random hex ids.
    numbers = [
        int(script.revision)
        for script in ScriptDirectory.from_config(config).walk_revisions()
        if re.fullmatch(r"\d+", script.revision)
    ]
    return f"{max(numbers, default=0) + 1:03d}"


def process_revision_directives(context_, revision, directives) -> None:
    script = directives[0]
    cmd_opts = getattr(config, "cmd_opts", None)
    if cmd_opts is not None and getattr(cmd_opts, "autogenerate", False) and script.upgrade_ops.is_empty():
        directives[:] = []
        print("No model changes detected; no migration created.")
        return
    if not (cmd_opts is not None and getattr(cmd_opts, "rev_id", None)):
        script.rev_id = next_revision_id()


def include_name(name, type_, parent_names) -> bool:
    if type_ == "schema":
        return name == SCHEMA
    return True


def configure(connection=None, **kwargs) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema=SCHEMA,
        compare_type=True,
        process_revision_directives=process_revision_directives,
        **kwargs,
    )


def run_migrations_offline() -> None:
    configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # The version table lives in the governance schema, so it must exist first.
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
