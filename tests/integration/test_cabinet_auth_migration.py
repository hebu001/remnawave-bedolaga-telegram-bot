"""Run revision 0104 against an isolated PostgreSQL schema."""

import importlib.util
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, inspect, text

from app.database.models import User
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]
path = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0104_cabinet_auth_version.py'
spec = importlib.util.spec_from_file_location('cabinet_auth_migration', path)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def migrate(factory, direction):
    def execute(connection):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(execute)


@pytest_asyncio.fixture
async def previous_schema(sessions):
    async with sessions.kw['bind'].begin() as connection:
        await connection.execute(text('TRUNCATE users CASCADE'))
        await connection.execute(
            insert(User).values(auth_type='email', email='old@example.com', password_hash='unchanged-hash')
        )
        await connection.execute(text('DROP TABLE IF EXISTS cabinet_ws_tickets'))
        await connection.execute(text('ALTER TABLE users DROP COLUMN IF EXISTS cabinet_auth_version'))
    return sessions


async def test_upgrade_preserves_existing_password_and_initializes_zero(previous_schema):
    await migrate(previous_schema, 'upgrade')
    async with previous_schema.kw['bind'].connect() as connection:
        row = (await connection.execute(text('SELECT password_hash, cabinet_auth_version FROM users'))).one()
        assert row == ('unchanged-hash', 0)

        def verify(sync):
            inspector = inspect(sync)
            columns = inspector.get_columns('cabinet_ws_tickets')
            assert {column['name'] for column in columns} == {
                'token_hash',
                'user_id',
                'auth_version',
                'origin',
                'expires_at',
                'access_expires_at',
            }
            assert all(not column['nullable'] for column in columns)
            assert inspector.get_foreign_keys('cabinet_ws_tickets')[0]['options']['ondelete'] == 'CASCADE'
            assert {index['name'] for index in inspector.get_indexes('cabinet_ws_tickets')} == {
                'ix_cabinet_ws_tickets_user',
                'ix_cabinet_ws_tickets_expires',
            }

        await connection.run_sync(verify)


async def test_downgrade_is_reversible_before_any_revocation(previous_schema):
    await migrate(previous_schema, 'upgrade')
    await migrate(previous_schema, 'downgrade')
    await migrate(previous_schema, 'upgrade')
    async with previous_schema.kw['bind'].connect() as connection:
        assert await connection.scalar(text('SELECT cabinet_auth_version FROM users')) == 0


async def test_downgrade_cannot_reenable_revoked_credentials(previous_schema):
    await migrate(previous_schema, 'upgrade')
    async with previous_schema.kw['bind'].begin() as connection:
        await connection.execute(text('UPDATE users SET cabinet_auth_version = 1'))
    with pytest.raises(RuntimeError, match='Cannot discard credential revocations'):
        await migrate(previous_schema, 'downgrade')
    async with previous_schema.kw['bind'].connect() as connection:
        assert await connection.scalar(text('SELECT cabinet_auth_version FROM users')) == 1
