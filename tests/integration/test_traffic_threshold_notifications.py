"""Real database tests: thresholds, cycle resets, durable claims and races."""

import asyncio
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.config import settings
from app.database.models import Subscription, TrafficNotificationState, User
from app.external.remnawave_api import UserStatus
from app.services import traffic_notification_service as traffic
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='isolated PostgreSQL')]
NOW = datetime(2026, 9, 26, tzinfo=UTC)


@pytest_asyncio.fixture
async def account(sessions, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: True)
    monkeypatch.setattr(traffic.cache, 'get', AsyncMock(return_value=None))
    async with sessions() as db:
        await db.execute(text('TRUNCATE users RESTART IDENTITY CASCADE'))
        user = User(telegram_id=1001, remnawave_uuid='legacy-uuid', language='ru', notification_settings={})
        db.add(user)
        await db.flush()
        sub = Subscription(
            user_id=user.id,
            status='active',
            traffic_limit_gb=100,
            traffic_used_gb=0,
            remnawave_uuid='panel-uuid',
            remnawave_short_id=uuid4().hex[:12],
            end_date=NOW + timedelta(days=90),
        )
        db.add(sub)
        await db.commit()
        return sub.id


def sample(percent, *, reset=NOW, status=UserStatus.ACTIVE, uuid='panel-uuid'):
    used = int(percent * traffic.GB)
    return SimpleNamespace(
        uuid=uuid,
        user_traffic=SimpleNamespace(used_traffic_bytes=used),
        used_traffic_bytes=used,
        last_traffic_reset_at=reset,
        status=status,
    )


def service(sessions, bot=None, panel=None):
    return traffic.TrafficNotificationService(
        bot or SimpleNamespace(send_message=AsyncMock()), session_factory=sessions, panel_service=panel or MagicMock()
    )


async def change(sessions, account, **values):
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        for k, v in values.items():
            setattr(sub, k, v)
        await db.commit()


async def test_once_at_each_threshold_across_days_restarts_and_redis_loss(sessions, account):
    bot = SimpleNamespace(send_message=AsyncMock())
    for index, percent in enumerate([79, 80, 86, 86, 90, 99, 100, 110, 110]):
        await service(sessions, bot).process_sample(account, sample(percent), NOW + timedelta(days=index))
    assert bot.send_message.await_count == 3
    messages = [c.args[1] for c in bot.send_message.await_args_list]
    assert '80%' in messages[0] and '90%' in messages[1]
    assert 'исчерпан' in messages[2]
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.highest_threshold == 100 and state.delivery_status == 'sent'


async def test_jump_to_95_sends_only_current_stage(sessions, account):
    svc = service(sessions)
    await svc.process_sample(account, sample(95), NOW)
    await svc.process_sample(account, sample(95), NOW + timedelta(days=3))
    assert svc.bot.send_message.await_count == 1
    assert '90%' in svc.bot.send_message.call_args.args[1]


async def test_real_reset_rearms_but_renewal_without_reset_does_not(sessions, account):
    svc = service(sessions)
    await svc.process_sample(account, sample(86), NOW)
    await change(sessions, account, end_date=NOW + timedelta(days=180))
    await svc.process_sample(account, sample(86), NOW + timedelta(days=1))
    assert svc.bot.send_message.await_count == 1
    await svc.process_sample(account, sample(82, reset=NOW + timedelta(days=2)), NOW + timedelta(days=2))
    assert svc.bot.send_message.await_count == 2


async def test_unstamped_reset_and_added_allowance(sessions, account):
    svc = service(sessions)
    for i, used in enumerate([86, 3, 80]):
        await svc.process_sample(account, sample(used, reset=None), NOW + timedelta(hours=i))
    assert svc.bot.send_message.await_count == 2
    await change(sessions, account, traffic_limit_gb=200)
    await svc.process_sample(account, sample(160, reset=None), NOW + timedelta(hours=4))
    assert svc.bot.send_message.await_count == 3


async def test_concurrent_poll_and_webhook_reserve_once(sessions, account):
    bot = SimpleNamespace(send_message=AsyncMock())
    results = await asyncio.gather(*[service(sessions, bot).process_sample(account, sample(86), NOW) for _ in range(8)])
    assert results.count(True) == 1
    assert bot.send_message.await_count == 1


async def test_old_sample_and_other_subscription_uuid_are_ignored(sessions, account):
    svc = service(sessions)
    await svc.process_sample(account, sample(90), NOW + timedelta(hours=1))
    await svc.process_sample(account, sample(100), NOW)
    await svc.process_sample(account, sample(100, uuid='legacy-uuid'), NOW + timedelta(hours=2))
    assert svc.bot.send_message.await_count == 1


@pytest.mark.parametrize(
    'changes,panel',
    [
        ({'traffic_limit_gb': 0}, sample(90)),
        ({'status': 'expired'}, sample(90)),
        ({}, sample(90, status=UserStatus.DISABLED)),
        ({}, sample(90, status=UserStatus.EXPIRED)),
    ],
)
async def test_ineligible_subscriptions_are_skipped(sessions, account, changes, panel):
    await change(sessions, account, **changes)
    svc = service(sessions)
    assert not await svc.process_sample(account, panel, NOW)
    svc.bot.send_message.assert_not_awaited()


async def test_disabled_preference_and_missing_counter_are_skipped(sessions, account):
    async with sessions() as db:
        user = await db.scalar(select(User))
        user.notification_settings = {'traffic_warning_enabled': False}
        await db.commit()
    svc = service(sessions)
    await svc.process_sample(account, sample(90), NOW)
    missing = sample(100)
    missing.user_traffic = None
    await svc.process_sample(account, missing, NOW)
    svc.bot.send_message.assert_not_awaited()


async def test_limited_subscription_gets_final_notice(sessions, account):
    await change(sessions, account, status='limited')
    svc = service(sessions)
    await svc.process_sample(account, sample(100, status=UserStatus.LIMITED), NOW)
    await svc.process_sample(account, sample(100, status=UserStatus.LIMITED), NOW + timedelta(days=3))
    assert svc.bot.send_message.await_count == 1
    assert 'исчерпан' in svc.bot.send_message.call_args.args[1]


async def test_old_daily_marker_is_carried_forward(sessions, account, monkeypatch):
    await change(sessions, account, traffic_used_gb=86)
    monkeypatch.setattr(traffic.cache, 'get', AsyncMock(return_value='1'))
    svc = service(sessions)
    await svc.process_sample(account, sample(86), NOW)
    svc.bot.send_message.assert_not_awaited()
    await svc.process_sample(account, sample(90), NOW + timedelta(hours=1))
    assert svc.bot.send_message.await_count == 1


async def test_ambiguous_telegram_failure_is_not_retried_daily(sessions, account):
    svc = service(sessions)
    svc.bot.send_message.side_effect = TelegramNetworkError(method=MagicMock(), message='timeout')
    await svc.process_sample(account, sample(86), NOW)
    svc.bot.send_message.side_effect = None
    await svc.process_sample(account, sample(86), NOW + timedelta(days=1))
    assert svc.bot.send_message.await_count == 1


async def test_flood_rejection_can_retry_after_delay(sessions, account):
    svc = service(sessions)
    svc.bot.send_message.side_effect = TelegramRetryAfter(method=MagicMock(), message='flood', retry_after=60)
    now = datetime.now(UTC)
    await svc.process_sample(account, sample(86), now)
    svc.bot.send_message.side_effect = None
    await svc.process_sample(account, sample(86), now + timedelta(seconds=30))
    assert svc.bot.send_message.await_count == 1
    await svc.process_sample(account, sample(86), now + timedelta(seconds=120))
    assert svc.bot.send_message.await_count == 2


async def test_poll_fetches_panel_data_and_never_sends_stale_db_usage(sessions, account):
    await change(sessions, account, traffic_used_gb=86)
    api = SimpleNamespace(
        get_all_users_page_stream=AsyncMock(
            return_value={
                'users': [sample(3)],
                'hasMore': False,
            }
        )
    )
    panel = MagicMock()
    panel.get_api_client.return_value.__aenter__.return_value = api
    svc = service(sessions, panel=panel)
    await svc.check_all()
    svc.bot.send_message.assert_not_awaited()
    api.get_all_users_page_stream.side_effect = TimeoutError()
    await svc.check_all()
    svc.bot.send_message.assert_not_awaited()
    api.get_all_users_page_stream.side_effect = None
    api.get_all_users_page_stream.return_value = {'users': [sample(95)], 'hasMore': False}
    await svc.check_all()
    assert svc.bot.send_message.await_count == 1


async def test_migration_upgrade_and_downgrade(sessions):
    path = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0106_traffic_notification_states.py'
    spec = importlib.util.spec_from_file_location('traffic_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()

    async with sessions.kw['bind'].begin() as connection:
        await connection.run_sync(run)
    async with sessions() as db:
        assert await db.scalar(text("SELECT to_regclass('traffic_notification_states')"))
