"""Real-DB ownership guards at grace write boundaries, including durable retries."""

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update

from app.config import settings
from app.database.models import GraceAccessSessionModel, RenewalSyncTask, Subscription, User
from app.external.remnawave_api import UserStatus
from app.services import grace_access_runtime as grace, renewal_sync_service as renewal_sync
from app.services.grace_access_service import GraceAccessMode
from app.services.panel_sync import PanelAccountOwnedByAnotherUser
from app.services.subscription_service import SubscriptionService
from tests.crud.test_renewal_heals_grace_overlay_echo import _billing, _session
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


async def seed(sessions, monkeypatch, *, foreign=True, paid=False):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: False)
    monkeypatch.setattr(grace.grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
    monkeypatch.setattr(grace, 'AsyncSessionLocal', sessions)
    now = datetime.now(UTC)
    billing = replace(
        _billing(end_at=now - timedelta(hours=1), squads=(), limit_gb=100), subscription_id=11, remnawave_id=737
    )
    session = replace(
        _session(started_at=now - timedelta(minutes=30), billing_before=billing, completion=None),
        subscription_id=11,
        remnawave_id=737,
    )
    session = replace(session, panel_before=replace(session.panel_before, remnawave_id=737))
    async with sessions() as db:
        await db.execute(text('TRUNCATE users RESTART IDENTITY CASCADE'))
        db.add(
            User(
                id=1,
                telegram_id=101,
                email='a@example.test',
                first_name='A',
                status='active',
                remnawave_id=737,
                balance_kopeks=12345,
            )
        )
        db.add(User(id=2, telegram_id=102, first_name='B', status='active', balance_kopeks=45678))
        await db.flush()
        db.add(
            Subscription(
                id=11,
                user_id=1,
                remnawave_short_id='owner-a',
                remnawave_id=None if foreign else 737,
                status='active' if paid else 'expired',
                is_trial=False,
                traffic_limit_gb=100,
                device_limit=2,
                connected_squads=[],
                end_date=now + timedelta(days=30) if paid else billing.end_at,
            )
        )
        db.add(
            Subscription(
                id=12,
                user_id=2,
                remnawave_short_id='owner-b',
                remnawave_id=737 if foreign else 888,
                status='active',
                is_trial=False,
                traffic_limit_gb=100,
                device_limit=3,
                connected_squads=[],
                end_date=now + timedelta(days=30),
            )
        )
        await db.flush()
        db.add(grace._session_to_model(session))
        await db.commit()
    panel = SimpleNamespace(
        id=737,
        telegram_id=101,
        short_uuid='a-short',
        subscription_url='https://example.test/a',
        happ_crypto_link=None,
        expire_at=session.grace_until,
        status=UserStatus.ACTIVE,
        used_traffic_bytes=0,
    )
    api = AsyncMock()
    api.get_user_by_id.return_value = panel
    api.update_user.return_value = panel
    api.find_users_by_email.return_value = []
    api.find_users_by_telegram_id.return_value = []
    api.get_user_by_short_uuid.return_value = None

    @asynccontextmanager
    async def client(_self=None):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    return api, session


@pytest.mark.parametrize('method', ['create_remnawave_user', 'update_remnawave_user'])
async def test_foreign_open_grace_metadata_is_never_written(sessions, monkeypatch, method):
    api, opened = await seed(sessions, monkeypatch)
    async with sessions() as db:
        sub = await db.get(Subscription, 11)
        assert await getattr(SubscriptionService(), method)(db, sub, reset_traffic=True) is None
    api.update_user.assert_not_awaited()
    api.create_user.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()
    api.reset_user_devices.assert_not_awaited()
    async with sessions() as db:
        assert list((await db.scalars(select(User.balance_kopeks).order_by(User.id))).all()) == [12345, 45678]
        assert (await db.get(Subscription, 12)).remnawave_id == 737
        assert (await db.get(GraceAccessSessionModel, opened.id)).state == 'active'


async def test_paid_foreign_grace_stays_pending_without_reset_or_false_ack(sessions, monkeypatch):
    api, opened = await seed(sessions, monkeypatch, paid=True)
    async with sessions() as db:
        db.add(RenewalSyncTask(subscription_id=11, reset_traffic=True, reset_devices=True, sync_squads=True))
        await db.commit()
    assert not await renewal_sync.process_renewal_sync(11, session_factory=sessions, force=True)
    api.update_user.assert_not_awaited()
    api.create_user.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()
    api.reset_user_devices.assert_not_awaited()
    async with sessions() as db:
        task = await db.get(RenewalSyncTask, 11)
        assert task.status == 'pending' and task.reset_traffic and task.reset_devices
        assert task.last_error == 'panel_returned_none'
        assert (await db.get(GraceAccessSessionModel, opened.id)).state == 'active'
        assert (await db.get(User, 1)).balance_kopeks == 12345


async def test_recovered_gateway_checks_actual_billing_target_not_supplied_snapshot(sessions, monkeypatch):
    api, opened = await seed(sessions, monkeypatch, paid=True)
    async with sessions() as db:
        await grace.lock_grace_sensitive_panel_updates(db, (11,))
        with pytest.raises(PanelAccountOwnedByAnotherUser):
            await grace.apply_recovered_grace_update_locked(
                db, api, 11, update_kwargs={'user_id': 999, 'email': 'a@example.test'}, source='regression'
            )
        await db.rollback()
    api.update_user.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()
    async with sessions() as db:
        assert (await db.get(GraceAccessSessionModel, opened.id)).state == 'active'


async def test_safe_grace_gateway_refuses_foreign_metadata_patch(sessions, monkeypatch):
    api, _ = await seed(sessions, monkeypatch)
    with pytest.raises(PanelAccountOwnedByAnotherUser):
        await grace.update_panel_user_grace_safe(
            api, 11, user_id=737, email='a@example.test', expire_at=datetime.now(UTC)
        )
    api.update_user.assert_not_awaited()


@pytest.mark.parametrize('boundary', ['overlay', 'restore', 'billing'])
async def test_db_backed_overlay_gateways_refuse_foreign_account(sessions, monkeypatch, boundary):
    api, opened = await seed(sessions, monkeypatch)
    async with sessions() as db:
        gateway = grace.RemnawaveGracePanelGateway(db=db, subscription_id=11)
        with pytest.raises(PanelAccountOwnedByAnotherUser):
            if boundary == 'overlay':
                await gateway.apply_overlay(737, opened.overlay)
            elif boundary == 'restore':
                await gateway.restore_snapshot(737, opened.panel_before, opened.overlay)
            else:
                await gateway.apply_billing_state(opened.billing_before, expected_overlay=opened.overlay)
    api.update_user.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()


async def test_masked_update_refreshes_changed_target_under_lock_and_preserves_expiry(sessions, monkeypatch):
    api, opened = await seed(sessions, monkeypatch, foreign=False)
    original_lock = grace.lock_grace_sensitive_panel_updates

    async def replace_then_lock(db, ids):
        async with sessions() as other:
            await other.execute(update(User).where(User.id == 1).values(remnawave_id=999))
            await other.execute(update(Subscription).where(Subscription.id == 11).values(remnawave_id=999))
            await other.commit()
        return await original_lock(db, ids)

    monkeypatch.setattr(grace, 'lock_grace_sensitive_panel_updates', replace_then_lock)
    api.update_user.return_value.id = 999
    async with sessions() as db:
        sub = await db.get(Subscription, 11)
        result = await SubscriptionService().update_remnawave_user(db, sub, reset_traffic=True)
        assert result.id == 999
    api.update_user.assert_awaited_once()
    sent = api.update_user.call_args.kwargs
    assert sent['user_id'] == 999
    assert not {'expire_at', 'status', 'traffic_limit_bytes', 'active_internal_squads'} & sent.keys()
    assert result.expire_at == opened.grace_until
    api.reset_user_traffic.assert_not_awaited()
