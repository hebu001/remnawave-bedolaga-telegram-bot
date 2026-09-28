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
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/evo_0106_traffic_notification_states.py'
)


@pytest_asyncio.fixture
async def account(sessions, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: True)
    monkeypatch.setattr(traffic.cache, 'get', AsyncMock(return_value=None))
    async with sessions() as db:
        await db.execute(text('TRUNCATE users RESTART IDENTITY CASCADE'))
        user = User(
            telegram_id=1001, remnawave_uuid='legacy-uuid', remnawave_id=7, language='ru', notification_settings={}
        )
        db.add(user)
        await db.flush()
        sub = Subscription(
            user_id=user.id,
            status='active',
            traffic_limit_gb=100,
            traffic_used_gb=0,
            remnawave_uuid='panel-uuid',
            remnawave_id=42,
            remnawave_short_id=uuid4().hex[:12],
            end_date=NOW + timedelta(days=90),
        )
        db.add(sub)
        await db.commit()
        return sub.id


def sample(percent, *, reset=NOW, status=UserStatus.ACTIVE, user_id=42):
    used = int(percent * traffic.GB)
    return SimpleNamespace(
        id=user_id,
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
    await svc.process_sample(account, sample(100, user_id=7), NOW + timedelta(hours=2))
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
    spec = importlib.util.spec_from_file_location('traffic_migration', MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
            # This module shares its schema; restore the current runtime shape
            # after exercising the historical migration in isolation.
            binding_spec = importlib.util.spec_from_file_location(
                'traffic_binding_migration', MIGRATION_PATH.with_name('evo_0109_traffic_panel_binding.py')
            )
            binding = importlib.util.module_from_spec(binding_spec)
            binding_spec.loader.exec_module(binding)
            binding.upgrade()

    async with sessions.kw['bind'].begin() as connection:
        await connection.run_sync(run)
    async with sessions() as db:
        assert await db.scalar(text("SELECT to_regclass('traffic_notification_states')"))


@pytest.mark.parametrize('delivery_status', ['sent', 'reserved', 'uncertain'])
async def test_numeric_upgrade_retains_legacy_uuid_cycle_reservation(sessions, account, delivery_status, monkeypatch):
    import hashlib
    import json

    from app.services.remnawave_identity_backfill import backfill_remnawave_ids
    from tests.services.test_remnawave_identity_backfill import _patch_roster, panel_user

    _patch_roster(monkeypatch, [panel_user(42, short_uuid='historic-short', username='user_1001', telegram_id=1001)])
    # Exact pre-upgrade key: not produced by the implementation under test.
    old_key = hashlib.sha256(json.dumps(['panel-uuid', NOW.isoformat(), 100 * traffic.GB]).encode()).hexdigest()
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        user = await db.get(User, sub.user_id)
        sub.remnawave_id = None
        user.remnawave_id = None
        sub.remnawave_short_uuid = 'historic-short'
        db.add(
            TrafficNotificationState(
                subscription_id=account,
                cycle_key=old_key,
                generation=4,
                highest_threshold=80,
                used_bytes=86 * traffic.GB,
                observed_at=NOW,
                delivery_status=delivery_status,
            )
        )
        await db.commit()
    async with sessions() as db:
        report = await backfill_remnawave_ids(db, dry_run=False)
        assert report.subscriptions_resolved == 1 and not report.conflicts
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id is None and state.delivery_status == delivery_status
        assert (await db.get(Subscription, account)).remnawave_id == 42
    svc = service(sessions)
    await svc.process_sample(account, sample(86), NOW + timedelta(hours=1))
    svc.bot.send_message.assert_not_awaited()
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id == 42
        assert (state.cycle_key, state.generation, state.highest_threshold, state.delivery_status) == (
            old_key,
            4,
            80,
            delivery_status,
        )
    await svc.process_sample(account, sample(95), NOW + timedelta(hours=2))
    assert svc.bot.send_message.await_count == 1


async def test_numeric_upgrade_without_legacy_namespace_retains_unresolved_state(sessions, account):
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        sub.remnawave_uuid = None
        db.add(
            TrafficNotificationState(
                subscription_id=account,
                cycle_key='a' * 64,
                generation=4,
                highest_threshold=80,
                used_bytes=86 * traffic.GB,
                observed_at=NOW,
                delivery_status='uncertain',
            )
        )
        await db.commit()
    svc = service(sessions)
    await svc.process_sample(account, sample(95), NOW + timedelta(hours=1))
    svc.bot.send_message.assert_not_awaited()
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.cycle_key == 'a' * 64 and state.highest_threshold == 80
        assert state.observed_at == NOW and state.delivery_status == 'uncertain'


async def test_new_numeric_account_cycle_fits_persisted_key(sessions, account):
    await change(sessions, account, remnawave_uuid=None)
    svc = service(sessions)
    await svc.process_sample(account, sample(86), NOW)
    await svc.process_sample(account, sample(86), NOW + timedelta(hours=1))
    assert svc.bot.send_message.await_count == 1
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.cycle_key.startswith('id:') and len(state.cycle_key) == 64


async def test_signed_webhook_refetch_uses_numeric_id_and_shares_poll_gate(sessions, account):
    api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=sample(95)))
    panel = MagicMock()
    panel.get_api_client.return_value.__aenter__.return_value = api
    svc = service(sessions, panel=panel)
    await svc.process_sample(account, sample(95), NOW)
    await svc.check_subscription(account)
    api.get_user_by_id.assert_awaited_once_with(42)
    assert svc.bot.send_message.await_count == 1


async def test_legacy_retry_deadline_survives_numeric_upgrade(sessions, account):
    import hashlib
    import json

    old_key = hashlib.sha256(json.dumps(['panel-uuid', NOW.isoformat(), 100 * traffic.GB]).encode()).hexdigest()
    deadline = NOW + timedelta(hours=2)
    async with sessions() as db:
        db.add(
            TrafficNotificationState(
                subscription_id=account,
                cycle_key=old_key,
                generation=2,
                highest_threshold=0,
                used_bytes=86 * traffic.GB,
                observed_at=NOW,
                delivery_status='retry',
                next_attempt_at=deadline,
            )
        )
        await db.commit()
    svc = service(sessions)
    await svc.process_sample(account, sample(86), NOW + timedelta(hours=1))
    svc.bot.send_message.assert_not_awaited()
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.next_attempt_at == deadline and state.generation == 2
        assert state.panel_user_id == 42
    await svc.process_sample(account, sample(86), deadline)
    assert svc.bot.send_message.await_count == 1


@pytest.mark.parametrize('multi_tariff', [False, True])
@pytest.mark.parametrize('already_bound', [False, True])
@pytest.mark.parametrize('write_path', ['writer', 'service_cleanup'])
async def test_real_recreation_before_first_poll_starts_numeric_cycle(
    sessions, account, monkeypatch, already_bound, write_path, multi_tariff
):
    """42 -> 99 must rearm even when usage rises and resetAt stays absent."""
    import hashlib
    import json
    from contextlib import asynccontextmanager

    from app.services.panel_sync import push_subscription
    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: multi_tariff)
    old_id = 42 if multi_tariff else 7
    old_uuid = 'panel-uuid' if multi_tariff else 'legacy-uuid'
    old_key = hashlib.sha256(json.dumps([old_uuid, None, 100 * traffic.GB]).encode()).hexdigest()
    async with sessions() as db:
        db.add(
            TrafficNotificationState(
                subscription_id=account,
                panel_user_id=old_id if already_bound else None,
                cycle_key=old_key,
                generation=4,
                highest_threshold=80,
                used_bytes=85 * traffic.GB,
                observed_at=NOW,
                delivery_status='reserved',
            )
        )
        await db.commit()
    created = SimpleNamespace(
        id=99, short_uuid='new99', subscription_url='https://example.test/new99', happ_crypto_link=None
    )
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    api.create_user.return_value = created

    @asynccontextmanager
    async def client(_self=None):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        await db.refresh(sub, ['user', 'tariff'])
        if write_path == 'writer':
            result = await push_subscription(api, sub.user, sub, db=db, multi_tariff=multi_tariff, reset_devices=False)
            assert result.panel_user_id == 99
            await db.commit()
            assert sub.remnawave_uuid == 'panel-uuid', 'historical UUID remains inert after writer recreation'
        else:
            result = await SubscriptionService().create_remnawave_user(db, sub)
            assert result.id == 99
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id == old_id, (
            'capture old ID before cleanup/overwrite even without any post-upgrade poll'
        )
        assert (state.cycle_key, state.generation, state.highest_threshold, state.delivery_status) == (
            old_key,
            4,
            80,
            'reserved',
        )
    api.create_user.assert_awaited_once()
    svc = service(sessions)
    assert await svc.process_sample(account, sample(86, reset=None, user_id=99), NOW + timedelta(hours=1))
    assert not await svc.process_sample(account, sample(86, reset=None, user_id=99), NOW + timedelta(hours=2))
    assert svc.bot.send_message.await_count == 1
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id == 99 and state.generation == 5 and state.cycle_key.startswith('id:')
        first_key = state.cycle_key
    # A later reset and allowance change stay in numeric mode despite old UUID.
    reset = NOW + timedelta(hours=3)
    assert await svc.process_sample(account, sample(86, reset=reset, user_id=99), reset)
    assert not await svc.process_sample(account, sample(86, reset=reset, user_id=99), reset + timedelta(minutes=1))
    await change(sessions, account, traffic_limit_gb=200)
    assert await svc.process_sample(account, sample(160, reset=reset, user_id=99), reset + timedelta(hours=1))
    assert not await svc.process_sample(account, sample(160, reset=reset, user_id=99), reset + timedelta(hours=2))
    assert svc.bot.send_message.await_count == 3
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id == 99 and state.generation == 7
        assert state.cycle_key.startswith('id:') and state.cycle_key != first_key


async def test_history_binding_uses_subscription_then_state_lock_order(sessions, account):
    from app.services.panel_sync.traffic_identity import remember_previous_traffic_identity

    svc = service(sessions)
    await svc.process_sample(account, sample(85, reset=None), NOW)
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        state.panel_user_id = None
        await db.commit()
    entered = asyncio.Event()
    backend_pid = None

    async def replacement():
        nonlocal backend_pid
        async with sessions() as db:
            backend_pid = await db.scalar(text('SELECT pg_backend_pid()'))
            entered.set()
            await remember_previous_traffic_identity(db, account, 42)
            sub = await db.get(Subscription, account)
            sub.remnawave_id = 99
            await db.commit()

    async with sessions() as polling:
        await polling.execute(select(Subscription.id).where(Subscription.id == account).with_for_update())
        writer = asyncio.create_task(replacement())
        await entered.wait()
        # Wait for the replacement transaction to actually block on our row;
        # scheduling alone is not proof of overlapping lock acquisition.
        async with asyncio.timeout(3):
            async with sessions() as observer:
                while not await observer.scalar(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid").bindparams(
                        pid=backend_pid
                    )
                ):
                    await asyncio.sleep(0.01)
        # If the writer takes the state lock first, this UPDATE deadlocks after
        # it tries to flush the subscription. The shared order permits progress.
        state = await polling.get(TrafficNotificationState, account)
        state.used_bytes = 86 * traffic.GB
        await polling.flush()
        await polling.commit()
    async with asyncio.timeout(3):
        await writer
    async with sessions() as db:
        state = await db.get(TrafficNotificationState, account)
        assert state.panel_user_id == 42 and state.used_bytes == 86 * traffic.GB
        assert (await db.get(Subscription, account)).remnawave_id == 99


async def test_service_cleanup_matches_paid_user_subscription_lock_order_without_losing_pending_fields(
    sessions, account, monkeypatch
):
    """A renewal holds User first; cleanup must not grab Subscription ahead of it."""
    from contextlib import asynccontextmanager

    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    svc = service(sessions)
    await svc.process_sample(account, sample(85, reset=None), NOW)
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    api.create_user.return_value = SimpleNamespace(
        id=99, short_uuid='new99', subscription_url='https://example.test/new99', happ_crypto_link=None
    )

    @asynccontextmanager
    async def client(_self=None):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    entered = asyncio.Event()
    backend_pid = None
    new_end = datetime.now(UTC) + timedelta(days=123)

    async def cleanup():
        nonlocal backend_pid
        async with sessions() as db:
            backend_pid = await db.scalar(text('SELECT pg_backend_pid()'))
            sub = await db.get(Subscription, account)
            await db.refresh(sub, ['user', 'tariff'])
            sub.user.email = 'pending@example.test'
            sub.end_date = new_end
            sub.device_limit = 17
            entered.set()
            return await SubscriptionService().create_remnawave_user(db, sub)

    task = None
    try:
        async with sessions() as payment:
            user = await payment.scalar(select(User).with_for_update())
            task = asyncio.create_task(cleanup())
            await entered.wait()
            async with asyncio.timeout(3):
                async with sessions() as observer:
                    while not await observer.scalar(
                        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid").bindparams(
                            pid=backend_pid
                        )
                    ):
                        await asyncio.sleep(0.01)
                # Same ordering as paid renewal, after obtaining the balance lock.
                await payment.execute(select(Subscription.id).where(Subscription.id == account).with_for_update())
            user.balance_kopeks = 54321
            await payment.commit()
        async with asyncio.timeout(3):
            assert (await task).id == 99
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        user = await db.get(User, sub.user_id)
        assert sub.end_date == new_end and sub.device_limit == 17
        assert user.email == 'pending@example.test' and user.balance_kopeks == 54321
        assert sub.remnawave_id == 99
        assert (await db.get(TrafficNotificationState, account)).panel_user_id == 42
