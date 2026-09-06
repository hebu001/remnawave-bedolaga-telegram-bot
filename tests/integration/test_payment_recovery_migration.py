"""Exercise migration 0103 itself on an isolated PostgreSQL schema."""

import importlib.util
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.models import RenewalSyncTask, SubpageInvoice, User
from tests.integration.test_purchase_atomicity import context, seed_subscription, sessions


__all__ = ['context', 'sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]

_path = (
    Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0103_durable_subpage_orders_and_renewal_sync.py'
)
_spec = importlib.util.spec_from_file_location('payment_recovery_migration', _path)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


async def migrate(factory, direction):
    def execute(connection):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(execute)


@pytest_asyncio.fixture
async def migrated(sessions, context):
    # Base created the rest of the schema. Remove only the two new tables to
    # emulate revision 0102, then run the real upgrade, not metadata.create_all.
    async with sessions.kw['bind'].begin() as connection:
        await connection.execute(text('DROP TABLE IF EXISTS renewal_sync_tasks, subpage_invoices'))
    await migrate(sessions, 'upgrade')
    return sessions


async def test_upgrade_schema_matches_models_and_keeps_existing_users(migrated, context):
    def verify(connection):
        inspector = inspect(connection)
        for model in (SubpageInvoice, RenewalSyncTask):
            table = model.__table__
            columns = {column['name']: column for column in inspector.get_columns(table.name)}
            assert set(columns) == set(table.columns.keys())
            for column in table.columns:
                assert columns[column.name]['nullable'] == column.nullable
            indexes = {entry['name']: entry['column_names'] for entry in inspector.get_indexes(table.name)}
            for index in table.indexes:
                assert indexes[index.name] == [column.name for column in index.columns]
        foreign_keys = inspector.get_foreign_keys('subpage_invoices')
        assert len(foreign_keys) == 4
        assert all(key['options']['ondelete'] == 'SET NULL' for key in foreign_keys)
        assert inspector.get_foreign_keys('renewal_sync_tasks')[0]['options']['ondelete'] == 'CASCADE'
        assert any(
            constraint['column_names'] == ['method', 'provider_payment_id']
            for constraint in inspector.get_unique_constraints('subpage_invoices')
        )

    async with migrated.kw['bind'].connect() as connection:
        await connection.run_sync(verify)
    async with migrated() as db:
        assert await db.scalar(select(User.balance_kopeks).where(User.id == context.user_id)) == 50000
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    'INSERT INTO subpage_invoices (token, short_uuid, period_days, amount_kopeks, method) '
                    "VALUES ('invalid', 'shortuuid', 30, 0, 'wata')"
                )
            )
        await db.rollback()


async def test_empty_downgrade_and_reupgrade(migrated):
    await migrate(migrated, 'downgrade')
    async with migrated.kw['bind'].connect() as connection:
        names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        assert 'subpage_invoices' not in names
        assert 'renewal_sync_tasks' not in names
        assert 'users' in names
    await migrate(migrated, 'upgrade')


@pytest.mark.parametrize('pending_work', ['pending', 'paid', 'review', 'panel'])
async def test_downgrade_refuses_to_discard_pending_work(migrated, context, pending_work):
    sub_id, _ = await seed_subscription(migrated, context)
    async with migrated() as db:
        if pending_work == 'panel':
            db.add(RenewalSyncTask(subscription_id=sub_id, status='pending', reset_devices=True))
        else:
            db.add(
                SubpageInvoice(
                    token='pending-order',
                    short_uuid='shortuuid',
                    user_id=context.user_id,
                    subscription_id=sub_id,
                    period_days=30,
                    amount_kopeks=10000,
                    method='wata',
                    status=pending_work,
                )
            )
        await db.commit()
    with pytest.raises(RuntimeError, match='work remains pending'):
        await migrate(migrated, 'downgrade')
    async with migrated() as db:
        if pending_work == 'panel':
            assert (await db.get(RenewalSyncTask, sub_id)).reset_devices is True
        else:
            assert (await db.get(SubpageInvoice, 'pending-order')).status == pending_work
