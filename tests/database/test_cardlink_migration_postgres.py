"""Migration creates the same financial schema as fresh metadata on PostgreSQL."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


def _schema(conn):
    inspector = inspect(conn)
    return {
        'columns': [{**column, 'type': str(column['type'])} for column in inspector.get_columns('cardlink_payments')],
        'indexes': sorted(inspector.get_indexes('cardlink_payments'), key=lambda index: index['name']),
        'foreign_keys': sorted(inspector.get_foreign_keys('cardlink_payments'), key=lambda fk: fk['name']),
        'unique_constraints': inspector.get_unique_constraints('cardlink_payments'),
        'pk': inspector.get_pk_constraint('cardlink_payments'),
    }


async def test_migration_matches_bootstrap_without_rewriting_existing_financial_tables(postgres_database):
    path = Path(__file__).parents[2] / 'migrations/alembic/versions/evo_0111_cardlink_payments.py'
    spec = spec_from_file_location('cardlink_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == 'evo_0110'
    async with postgres_engine(postgres_database) as engine:
        async with engine.begin() as conn:
            baseline = await conn.run_sync(_schema)
            old_tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            await conn.execute(text('DROP TABLE cardlink_payments'))

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()

            await conn.run_sync(upgrade)
            assert await conn.run_sync(_schema) == baseline
            assert await conn.run_sync(lambda sync: set(inspect(sync).get_table_names())) == old_tables
    with pytest.raises(RuntimeError, match='backup'):
        migration.downgrade()
