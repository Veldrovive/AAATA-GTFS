"""Alembic environment.

`db.migrate` runs this with the connection it opened (`config.attributes
["connection"]`). From the command line (`uv run alembic ...`, using the
repository's `alembic.ini`) it opens the database under `AAATA_GTFS_DATA_PATH`.
"""

from typing import Any

from alembic import context
from alembic.operations import ops
from sqlalchemy import Connection

from aaata_gtfs import models

config = context.config


def _is_primary_key(table: str, column: str) -> bool:
    t = models.metadata.tables.get(table)
    return t is not None and column in t.primary_key.columns


def _ignore_primary_key_nullability(_context: Any, _revision: Any, directives: list[Any]) -> None:
    """Drop "make NOT NULL" changes to primary keys from autogenerate's output.

    SQLite reports a single-column `PRIMARY KEY` declared without `NOT NULL` (as
    the original schema does) as nullable, while the models declare primary keys
    NOT NULL, as SQLAlchemy needs to insert several rows in one flush. The
    columns never hold NULL, so the difference isn't worth rebuilding the tables.
    """
    script = directives[0]
    if not isinstance(script, ops.MigrationScript):
        return
    for upgrade_ops in script.upgrade_ops_list:
        kept: list[ops.MigrateOperation] = []
        for op in upgrade_ops.ops:
            if isinstance(op, ops.ModifyTableOps):
                for alter in op.ops:
                    if isinstance(alter, ops.AlterColumnOp) and _is_primary_key(alter.table_name, alter.column_name):
                        alter.modify_nullable = None
                op.ops = [o for o in op.ops if not (isinstance(o, ops.AlterColumnOp) and not o.has_changes())]
                if not op.ops:
                    continue
            kept.append(op)
        upgrade_ops.ops = kept


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=models.metadata,
        render_as_batch=True,  # SQLite can't ALTER most things; batch mode rebuilds the table
        compare_type=True,
        process_revision_directives=_ignore_primary_key_nullability,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    from aaata_gtfs import db
    from aaata_gtfs.config import DATA_PATH, db_path

    with db.make_engine(db_path(DATA_PATH)).begin() as conn:
        _run(conn)
