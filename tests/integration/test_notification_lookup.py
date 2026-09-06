"""C13: exact notification keys, pending writes, bounded SQL and concurrent index DDL."""

import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud.notification import get_sent_notification_keys, notification_sent, record_notification
from app.database.models import SentNotification, Subscription, User
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]
path = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0105_sent_notification_lookup.py'
spec = importlib.util.spec_from_file_location('notification_lookup_migration', path)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def migrate(factory, direction):
    def execute(connection):
        context = MigrationContext.configure(connection, opts={'transaction_per_migration': True})
        with context.begin_transaction(_per_migration=True), Operations.context(context):
            getattr(migration, direction)()

    # Concurrent index creation must not be nested in an external transaction.
    async with factory.kw['bind'].connect() as connection:
        await connection.run_sync(execute)


@pytest_asyncio.fixture
async def accounts(sessions):
    async with sessions() as db:
        await db.execute(text('TRUNCATE users RESTART IDENTITY CASCADE'))
        users = [User(telegram_id=1000 + index) for index in range(2)]
        db.add_all(users)
        await db.flush()
        subs = [
            Subscription(
                user_id=user.id, remnawave_short_id=uuid4().hex[:12], end_date=datetime.now(UTC) + timedelta(days=1)
            )
            for user in users
        ]
        db.add_all(subs)
        await db.commit()
        return [(user.id, sub.id) for user, sub in zip(users, subs, strict=True)]


async def test_lookup_keeps_owner_type_and_null_day_distinct(sessions, accounts):
    (user, sub), (other_user, other_sub) = accounts
    keys = [(user, sub, 'expiring', None), (user, sub, 'expiring', 3), (other_user, other_sub, 'trial_2h', None)]
    # Historical ownership mismatch must not match the requested valid pair.
    unrelated = [(other_user, sub, 'expiring', 1), (user, sub, 'different_type', None)]
    async with sessions() as db:
        db.add_all(
            [
                SentNotification(user_id=u, subscription_id=s, notification_type=t, days_before=d)
                for u, s, t, d in keys + unrelated
            ]
        )
        await db.commit()
        found = await get_sent_notification_keys(db, accounts, ['expiring', 'trial_2h'])
        assert found == set(keys)
        assert await notification_sent(db, user, sub, 'expiring') is True
        assert await notification_sent(db, user, sub, 'expiring', 0) is False
        assert await notification_sent(db, user, sub, 'expiring', 3) is True
        assert await notification_sent(db, user, sub, 'trial_2h') is False


async def test_unflushed_commit_false_records_are_visible_and_not_duplicated(sessions, accounts):
    user, sub = accounts[0]
    async with sessions() as db:
        await record_notification(db, user, sub, 'trial_2h', commit=False)
        assert await db.scalar(select(func.count()).select_from(SentNotification)) == 0
        assert await notification_sent(db, user, sub, 'trial_2h') is True
        assert await get_sent_notification_keys(db, [(user, sub)], ['trial_2h']) == {(user, sub, 'trial_2h', None)}
        await record_notification(db, user, sub, 'trial_2h', commit=False)
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(SentNotification)) == 1


async def test_lookup_uses_three_bounded_queries_for_1001_pairs(sessions, accounts):
    user, _ = accounts[0]
    async with sessions() as db:
        subs = [
            Subscription(user_id=user, remnawave_short_id=uuid4().hex[:12], end_date=datetime.now(UTC))
            for _ in range(1001)
        ]
        db.add_all(subs)
        await db.commit()
        pairs = [(user, sub.id) for sub in subs]
        db.add(SentNotification(user_id=user, subscription_id=subs[-1].id, notification_type='expiring', days_before=3))
        await db.commit()
        queries = []

        def capture(_connection, _cursor, statement, parameters, _context, _executemany):
            if 'sent_notifications' in statement:
                queries.append((statement, parameters))

        engine = sessions.kw['bind'].sync_engine
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            assert await get_sent_notification_keys(db, pairs, ['expiring']) == {(user, subs[-1].id, 'expiring', 3)}
            assert await get_sent_notification_keys(db, [], ['expiring']) == set()
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert len(queries) == 3
        assert all(len(parameters) <= 1001 for _, parameters in queries)
        assert all('sent_notifications.created_at' not in statement for statement, _ in queries)


async def test_index_migration_preserves_duplicates_and_is_reversible(sessions, accounts):
    user, sub = accounts[0]
    async with sessions() as db:
        db.add_all(
            [SentNotification(user_id=user, subscription_id=sub, notification_type='expiring') for _ in range(2)]
        )
        await db.commit()
    await migrate(sessions, 'downgrade')
    await migrate(sessions, 'upgrade')
    await migrate(sessions, 'upgrade')  # Safe retry after completed concurrent build.
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SentNotification)) == 2
        row = (
            await db.execute(
                text(
                    'SELECT indisvalid, indisunique, pg_get_indexdef(indexrelid) '
                    "FROM pg_index WHERE indexrelid = 'ix_sent_notifications_lookup'::regclass"
                )
            )
        ).one()
        assert row.indisvalid is True
        assert row.indisunique is False
        assert '(subscription_id, user_id, notification_type, days_before)' in row.pg_get_indexdef
    await migrate(sessions, 'downgrade')
    async with sessions() as db:
        assert await db.scalar(text("SELECT to_regclass('ix_sent_notifications_lookup')")) is None
        assert await db.scalar(select(func.count()).select_from(SentNotification)) == 2
    await migrate(sessions, 'upgrade')


async def test_postgres_planner_uses_composite_index_without_forcing_it(sessions, accounts):
    user, sub = accounts[0]
    async with sessions() as db:
        await db.execute(
            text(
                'INSERT INTO sent_notifications (user_id, subscription_id, notification_type, days_before) '
                "SELECT :user_id, :sub_id, 'unrelated', g FROM generate_series(1, 12000) AS g"
            ),
            {'user_id': user, 'sub_id': sub},
        )
        await record_notification(db, user, sub, 'trial_2h')
        await db.execute(text('ANALYZE sent_notifications'))
        plan = await db.scalar(
            text(
                'EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) SELECT EXISTS ('
                'SELECT id FROM sent_notifications WHERE subscription_id = :sub_id AND user_id = :user_id '
                "AND notification_type = 'trial_2h' AND days_before IS NULL)"
            ),
            {'user_id': user, 'sub_id': sub},
        )
        assert 'ix_sent_notifications_lookup' in str(plan)
        assert 'Seq Scan' not in str(plan)


async def test_migration_repairs_invalid_concurrent_build_on_retry(sessions, accounts):
    user, sub = accounts[0]
    async with sessions() as db:
        db.add_all([SentNotification(user_id=user, subscription_id=sub, notification_type='old') for _ in range(2)])
        await db.commit()
    await migrate(sessions, 'downgrade')
    async with sessions.kw['bind'].connect() as connection:
        connection = await connection.execution_options(isolation_level='AUTOCOMMIT')
        with pytest.raises(IntegrityError):
            await connection.execute(
                text('CREATE UNIQUE INDEX CONCURRENTLY ix_sent_notifications_lookup ON sent_notifications (user_id)')
            )
    async with sessions() as db:
        assert (
            await db.scalar(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_sent_notifications_lookup'::regclass")
            )
            is False
        )
    await migrate(sessions, 'upgrade')
    async with sessions() as db:
        assert (
            await db.scalar(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_sent_notifications_lookup'::regclass")
            )
            is True
        )
        assert await db.scalar(select(func.count()).select_from(SentNotification)) == 2


async def test_trial_monitoring_respects_batch_history_and_records_only_success(sessions, accounts):
    async with sessions() as db:
        for _user, subscription_id in accounts:
            sub = await db.get(Subscription, subscription_id)
            sub.status = 'active'
            sub.is_trial = True
            sub.end_date = datetime.now(UTC) + timedelta(hours=1)
        await db.commit()
        await record_notification(db, *accounts[0], 'trial_2h')
        service = MonitoringService(bot=MagicMock())
        service._send_trial_ending_notification = AsyncMock(return_value=True)
        service._log_monitoring_event = AsyncMock()
        await service._check_trial_expiring_soon(db)
        assert service._send_trial_ending_notification.await_count == 1
        assert service._send_trial_ending_notification.await_args.args[0].id == accounts[1][0]
        assert await notification_sent(db, *accounts[1], 'trial_2h') is True
        await service._check_trial_expiring_soon(db)
        assert service._send_trial_ending_notification.await_count == 1


@pytest.mark.parametrize(
    'days, notification_type', [(1.5, 'expired_1d'), (2.5, 'expired_discount_wave2'), (5.5, 'expired_discount_wave3')]
)
@pytest.mark.parametrize('has_active_sibling', [False, True])
async def test_followups_preserve_wave_specific_history_and_active_sibling_suppression(
    sessions, accounts, monkeypatch, days, notification_type, has_active_sibling
):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: True)
    for method in (
        'are_notifications_globally_enabled',
        'is_expired_1d_enabled',
        'is_second_wave_enabled',
        'is_third_wave_enabled',
    ):
        monkeypatch.setattr(NotificationSettingsService, method, lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'get_third_wave_trigger_days', lambda: 5)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_discount_percent', lambda: 10)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_valid_hours', lambda: 24)
    monkeypatch.setattr(NotificationSettingsService, 'get_third_wave_discount_percent', lambda: 20)
    monkeypatch.setattr(NotificationSettingsService, 'get_third_wave_valid_hours', lambda: 24)
    async with sessions() as db:
        for _user, subscription_id in accounts:
            sub = await db.get(Subscription, subscription_id)
            sub.status = 'expired'
            sub.is_trial = False
            sub.end_date = datetime.now(UTC) - timedelta(days=days)
        if has_active_sibling:
            db.add(
                Subscription(
                    user_id=accounts[1][0],
                    status='active',
                    is_trial=False,
                    remnawave_short_id='active-sibling',
                    end_date=datetime.now(UTC) + timedelta(days=30),
                )
            )
        await db.commit()
        await record_notification(db, *accounts[0], notification_type)
        # A different historical type must not suppress the current wave.
        await record_notification(db, *accounts[1], 'unrelated')
        service = MonitoringService(bot=MagicMock())
        service._send_expired_day1_notification = AsyncMock(return_value=True)
        service._send_expired_discount_notification = AsyncMock(return_value=True)
        service._log_monitoring_event = AsyncMock()
        await service._check_expired_subscription_followups(db)
        send_count = (
            service._send_expired_day1_notification.await_count
            + service._send_expired_discount_notification.await_count
        )
        assert send_count == (0 if has_active_sibling else 1)
        assert await notification_sent(db, *accounts[1], notification_type) is not has_active_sibling
        await service._check_expired_subscription_followups(db)
        assert (
            service._send_expired_day1_notification.await_count
            + service._send_expired_discount_notification.await_count
            == send_count
        )
