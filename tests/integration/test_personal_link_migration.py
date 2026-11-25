"""PostgreSQL backfill, interruption and import invariants for personal links."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.bridge_fixtures import database


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs isolated PostgreSQL'),
]


def migration():
    source = Path(__file__).parents[2] / 'migrations/alembic/versions/evo_0112_personal_cabinet_links.py'
    spec = spec_from_file_location('personal_link_migration', source)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_upgrade(sync, module):
    context = MigrationContext.configure(sync)
    with context.begin_transaction(), Operations.context(context):
        module.upgrade()


async def seed(connection):
    await connection.execute(text('CREATE TABLE users (id serial PRIMARY KEY, email text, balance_kopeks integer)'))
    await connection.execute(text('CREATE TABLE cabinet_ws_tickets (token_hash text PRIMARY KEY)'))
    await connection.execute(
        text("INSERT INTO users(email,balance_kopeks) SELECT 'legacy-' || n::text, n FROM generate_series(1,5003) n")
    )
    await connection.commit()


async def snapshot(connection):
    return (await connection.execute(text('SELECT id,email,balance_kopeks FROM users ORDER BY id'))).all()


async def selectors(connection):
    return dict((await connection.execute(text('SELECT id,cabinet_personal_link_id FROM users ORDER BY id'))).all())


async def test_backfill_preserves_rows_new_insert_gets_selector_and_unique_constraint():
    module = migration()
    assert module.down_revision == 'evo_0111'
    async with database(None, seed=False) as (engine, _):
        async with engine.connect() as connection:
            await seed(connection)
            before = await snapshot(connection)
            await connection.rollback()
            await connection.run_sync(run_upgrade, module)
            assert await snapshot(connection) == before
            assigned = await selectors(connection)
            assert len(assigned) == 5003
            assert None not in assigned.values() and len(set(assigned.values())) == 5003
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns('users'))
            selector_column = next(column for column in columns if column['name'] == 'cabinet_personal_link_id')
            assert selector_column['nullable'] is False and 'gen_random_uuid()' in selector_column['default']
            new_selector = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email,balance_kopeks) VALUES ('new-user',200) RETURNING cabinet_personal_link_id"
                    )
                )
            ).scalar_one()
            assert new_selector not in assigned.values()
            await connection.commit()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO users(email,cabinet_personal_link_id) SELECT 'duplicate',cabinet_personal_link_id "
                        'FROM users LIMIT 1'
                    )
                )
            await connection.rollback()
            with pytest.raises(IntegrityError):
                await connection.execute(text('INSERT INTO users(cabinet_personal_link_id) VALUES(NULL)'))
            await connection.rollback()
            assert (await selectors(connection))[1] == assigned[1]
    with pytest.raises(RuntimeError, match='backup'):
        module.downgrade()


async def test_interrupted_backfill_retry_preserves_previously_assigned_links():
    module = migration()
    async with database(None, seed=False) as (engine, _):
        async with engine.connect() as connection:
            await seed(connection)
            with patch.object(module.op, 'create_index', side_effect=RuntimeError('simulated final index failure')):
                with pytest.raises(RuntimeError, match='simulated'):
                    await connection.run_sync(run_upgrade, module)
            await connection.rollback()
            assigned = await selectors(connection)
            assert len(assigned) == 5003 and None not in assigned.values()
            # A concurrent registration after the committed DDL receives its own selector.
            new_id = (
                await connection.execute(
                    text("INSERT INTO users(email) VALUES ('created-during-backfill') RETURNING id")
                )
            ).scalar_one()
            await connection.commit()
            new_selector = (await selectors(connection))[new_id]
            await connection.rollback()
            await connection.run_sync(run_upgrade, module)
            result = await selectors(connection)
            assert {key: result[key] for key in assigned} == assigned
            assert result[new_id] == new_selector
