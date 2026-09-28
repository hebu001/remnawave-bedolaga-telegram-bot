"""Telegram custom/daily purchases use real PostgreSQL pricing, debits and rows.

Only external panel/Telegram/notification delivery is replaced. Each module run
owns a disposable schema on an explicitly configured loopback PostgreSQL.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


@pytest_asyncio.fixture(scope='module')
async def sessions():
    url = make_url(os.environ['TEST_POSTGRES_URL'])
    if url.host not in {'127.0.0.1', 'localhost', '::1'}:
        pytest.fail('TEST_POSTGRES_URL must point to a disposable loopback database')
    schema = 'test_tariff_' + uuid4().hex
    admin = create_async_engine(url, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={'server_settings': {'search_path': schema, 'statement_timeout': '10000'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def delivery(sessions, monkeypatch):
    async with sessions() as db:
        await db.execute(text('TRUNCATE users, tariffs RESTART IDENTITY CASCADE'))
        await db.commit()
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda _: True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(tariff_purchase, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(tariff_purchase, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(
        tariff_purchase,
        'AdminNotificationService',
        lambda _bot: SimpleNamespace(send_subscription_purchase_notification=AsyncMock()),
    )
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'delete_subscription_cart', AsyncMock())
    monkeypatch.setattr('app.services.event_emitter.event_emitter.emit', AsyncMock())
    monkeypatch.setattr('app.services.promo_group_assignment.maybe_assign_promo_group_by_total_spent', AsyncMock())
    monkeypatch.setattr(
        'app.services.referral_contest_service.referral_contest_service.on_subscription_payment', AsyncMock()
    )
    monkeypatch.setattr('app.services.yandex_offline_conv_service.spawn_bg', lambda coro: coro.close())
    return panel


async def seed(sessions, *, daily, same_tariff):
    async with sessions() as db:
        target = Tariff(
            name='Target paid tariff',
            period_prices={'30': 10000},
            is_daily=daily,
            daily_price_kopeks=1000 if daily else 0,
            device_limit=2,
            device_price_kopeks=200,
            allowed_squads=['target-squad'],
            traffic_limit_gb=100,
        )
        old = target if same_tariff else Tariff(name='Previous tariff', period_prices={'30': 15000}, device_limit=2)
        user = User(telegram_id=101, balance_kopeks=50000, remnawave_id=101)
        db.add_all([target, old, user])
        await db.flush()
        subscription = Subscription(
            user_id=user.id,
            tariff_id=old.id,
            status='active',
            is_trial=False,
            device_limit=5,
            traffic_limit_gb=100,
            connected_squads=['old-squad'],
            end_date=datetime.now(UTC) + timedelta(days=5),
            remnawave_short_id=uuid4().hex[:16],
        )
        db.add(subscription)
        await db.commit()
        return SimpleNamespace(
            user_id=user.id,
            tariff_id=target.id,
            old_tariff_id=old.id,
            subscription_id=subscription.id,
            old_end=subscription.end_date,
        )


async def confirm(sessions, context, *, daily):
    # Deliberately retain the old preview on A -> B: it must not price B with A's extras.
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'selected_tariff_id': context.old_tariff_id,
                'target_subscription_id': context.subscription_id,
                'device_limit': 5,
                'custom_days': 30,
                'custom_traffic_gb': 100,
            }
        ),
        clear=AsyncMock(),
    )
    callback = SimpleNamespace(
        data=f'confirm:{context.tariff_id}',
        bot=object(),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    handler = tariff_purchase.confirm_daily_tariff_purchase if daily else tariff_purchase.handle_custom_confirm
    async with sessions() as db:
        await handler.__wrapped__(callback, await db.get(User, context.user_id), db, state)
    state.clear.assert_awaited_once()


@pytest.mark.parametrize('daily', [False, True], ids=['custom', 'daily'])
@pytest.mark.parametrize('multi', [False, True], ids=['single', 'multi'])
async def test_switch_updates_primary_only_in_single_mode_and_drops_old_extras(
    sessions, delivery, monkeypatch, daily, multi
):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: multi)
    context = await seed(sessions, daily=daily, same_tariff=False)
    await confirm(sessions, context, daily=daily)
    expected_charge = 1000 if daily else 10000
    async with sessions() as db:
        subscriptions = (await db.scalars(select(Subscription).order_by(Subscription.id))).all()
        assert len(subscriptions) == (2 if multi else 1)
        purchased = next(sub for sub in subscriptions if sub.tariff_id == context.tariff_id)
        assert (purchased.id == context.subscription_id) is (not multi)
        assert purchased.device_limit == 2
        assert purchased.status == 'active' and purchased.is_trial is False
        if multi:
            old = next(sub for sub in subscriptions if sub.id == context.subscription_id)
            assert (old.tariff_id, old.device_limit, old.end_date) == (context.old_tariff_id, 5, context.old_end)
        elif not daily:
            assert abs((purchased.end_date - context.old_end - timedelta(days=30)).total_seconds()) < 1
        assert await db.scalar(select(User.balance_kopeks).where(User.id == context.user_id)) == 50000 - expected_charge
        assert (await db.scalars(select(Transaction.amount_kopeks))).all() == [-expected_charge]
    assert delivery.update_remnawave_user.await_args.args[1].id == purchased.id


@pytest.mark.parametrize('daily', [False, True], ids=['custom', 'daily'])
@pytest.mark.parametrize('multi', [False, True], ids=['single', 'multi'])
async def test_same_tariff_delivers_every_extra_device_in_the_real_charge(
    sessions, delivery, monkeypatch, daily, multi
):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: multi)
    context = await seed(sessions, daily=daily, same_tariff=True)
    await confirm(sessions, context, daily=daily)
    expected_charge = (1000 if daily else 10000) + 3 * 200
    async with sessions() as db:
        subscriptions = (await db.scalars(select(Subscription))).all()
        assert len(subscriptions) == 1
        purchased = subscriptions[0]
        assert purchased.id == context.subscription_id
        assert purchased.tariff_id == context.tariff_id
        assert purchased.device_limit == 5
        assert await db.scalar(select(User.balance_kopeks).where(User.id == context.user_id)) == 50000 - expected_charge
        assert (await db.scalars(select(Transaction.amount_kopeks))).all() == [-expected_charge]
        if daily:
            assert purchased.last_daily_charge_at is not None
            assert purchased.is_daily_paused is False
            assert timedelta(hours=23) < purchased.end_date - datetime.now(UTC) <= timedelta(days=1)
        else:
            assert purchased.end_date == context.old_end + timedelta(days=30)
    assert delivery.update_remnawave_user.await_args.args[1].device_limit == 5
