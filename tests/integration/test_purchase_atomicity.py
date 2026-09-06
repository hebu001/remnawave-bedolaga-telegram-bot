"""Real PostgreSQL regression tests for C01/C02.

Opt in with TEST_POSTGRES_URL pointing at a disposable loopback PostgreSQL.
Each run owns an isolated schema. Payment providers and panel calls are mocked;
balance, ledger, subscriptions, row locks and rollbacks use the actual database.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database.crud import subscription as subscription_crud
from app.database.crud.user import lock_user_for_pricing
from app.database.models import Base, ServerSquad, Subscription, SubscriptionServer, Tariff, Transaction, User
from app.services import renewal_sync_service, subscription_renewal_service as renewal
from app.services.pricing_engine import pricing_engine
from app.services.user_cart_service import user_cart_service
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppTariffPurchaseRequest


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


@pytest_asyncio.fixture(scope='module')
async def sessions():
    url = make_url(os.environ['TEST_POSTGRES_URL'])
    if url.host not in {'127.0.0.1', 'localhost', '::1'}:
        pytest.fail('TEST_POSTGRES_URL must point to a disposable loopback database')
    schema = 'test_atomic_' + uuid4().hex
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
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def context(sessions, monkeypatch):
    async with sessions() as db:
        await db.execute(text('TRUNCATE users, tariffs, server_squads RESTART IDENTITY CASCADE'))
        tariff = Tariff(
            name='Atomicity test',
            period_prices={'30': 10000},
            traffic_limit_gb=100,
            device_limit=2,
            device_price_kopeks=1000,
            allowed_squads=['test-squad'],
        )
        user = User(telegram_id=1001, balance_kopeks=50000, remnawave_uuid='test-panel-user')
        db.add_all([tariff, user])
        await db.commit()
        user_id, tariff_id = user.id, tariff.id

    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda _: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    # Provider side effects must only observe a durable purchase from a new session.
    observed = []

    async def assert_committed(*args, **kwargs):
        async with sessions() as observer:
            balance = await observer.scalar(select(User.balance_kopeks).where(User.id == user_id))
            count = await observer.scalar(select(func.count()).select_from(Transaction))
            assert count > 0
            observed.append((balance, count))
        return {'synced': True}

    panel = SimpleNamespace(
        update_remnawave_user=AsyncMock(side_effect=assert_committed),
        create_remnawave_user=AsyncMock(side_effect=assert_committed),
    )
    monkeypatch.setattr(miniapp, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(renewal_sync_service, 'SubscriptionService', lambda: panel)
    effects = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(miniapp, 'emit_transaction_side_effects', effects)
    monkeypatch.setattr(renewal, 'emit_transaction_side_effects', effects)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(user_cart_service, 'save_user_cart', AsyncMock())

    async def authorize(_init_data, db):
        return await db.get(User, user_id)

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', authorize)
    return SimpleNamespace(user_id=user_id, tariff_id=tariff_id, panel=panel, effects=effects, observed=observed)


async def purchase(sessions, context, *, period_days=30):
    async with sessions() as db:
        return await miniapp.purchase_tariff_endpoint(
            MiniAppTariffPurchaseRequest(initData='test', tariffId=context.tariff_id, periodDays=period_days),
            db,
        )


async def seed_subscription(sessions, context, *, status='active', is_trial=False, device_limit=2, tariff_id=None):
    async with sessions() as db:
        sub = Subscription(
            user_id=context.user_id,
            tariff_id=tariff_id or context.tariff_id,
            status=status,
            is_trial=is_trial,
            end_date=datetime.now(UTC) + timedelta(days=-1 if status == 'expired' else 5),
            traffic_limit_gb=100,
            device_limit=device_limit,
            connected_squads=['test-squad'],
            remnawave_short_id=uuid4().hex[:16],
            remnawave_uuid='test-panel-sub',
        )
        db.add(sub)
        await db.commit()
        return sub.id, sub.end_date


async def snapshot(sessions):
    async with sessions() as db:
        return {
            'users': (
                await db.execute(
                    select(
                        User.id,
                        User.balance_kopeks,
                        User.has_had_paid_subscription,
                        User.promo_offer_discount_percent,
                        User.promo_offer_discount_source,
                        User.promo_offer_discount_expires_at,
                    ).order_by(User.id)
                )
            ).all(),
            'subs': (
                await db.execute(
                    select(
                        Subscription.id,
                        Subscription.end_date,
                        Subscription.tariff_id,
                        Subscription.is_trial,
                        Subscription.device_limit,
                        Subscription.last_daily_charge_at,
                    ).order_by(Subscription.id)
                )
            ).all(),
            'ledger': (
                await db.execute(
                    select(Transaction.user_id, Transaction.amount_kopeks, Transaction.type).order_by(Transaction.id)
                )
            ).all(),
        }


async def test_miniapp_new_purchase_is_durable_before_side_effects(sessions, context):
    response = await purchase(sessions, context)
    state = await snapshot(sessions)
    assert response.success and response.balance_kopeks == 40000
    assert state['users'][0][1:3] == (40000, True)
    assert len(state['subs']) == 1
    assert state['subs'][0][0] == response.subscription_id
    assert state['ledger'] == [(context.user_id, -10000, 'subscription_payment')]
    assert context.observed == [(40000, 1), (40000, 1)]


@pytest.mark.parametrize('status,is_trial', [('active', False), ('expired', False), ('trial', True)])
async def test_miniapp_reuses_existing_subscription(sessions, context, status, is_trial):
    sub_id, old_end = await seed_subscription(sessions, context, status=status, is_trial=is_trial)
    response = await purchase(sessions, context)
    state = await snapshot(sessions)
    assert response.subscription_id == sub_id
    assert len(state['subs']) == 1 and state['subs'][0][3] is False
    if status == 'active':
        assert response.new_end_date == old_end + timedelta(days=30)
    else:
        assert response.new_end_date > datetime.now(UTC) + timedelta(days=29)


async def test_miniapp_preserves_and_charges_extra_devices(sessions, context):
    await seed_subscription(sessions, context, device_limit=4)
    response = await purchase(sessions, context)
    state = await snapshot(sessions)
    assert state['subs'][0][4] == 4
    assert response.balance_kopeks == 38000
    assert state['ledger'][0][1] == -12000


async def test_miniapp_multi_tariff_does_not_replace_another_tariff(sessions, context, monkeypatch):
    async with sessions() as db:
        other = Tariff(name='Other tariff', period_prices={'30': 20000})
        db.add(other)
        await db.commit()
        other_id = other.id
    other_sub, old_end = await seed_subscription(sessions, context, tariff_id=other_id)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: True)
    response = await purchase(sessions, context)
    state = await snapshot(sessions)
    assert len(state['subs']) == 2
    assert response.subscription_id != other_sub
    assert state['subs'][0][1:3] == (old_end, other_id)


@pytest.mark.parametrize('period_days', [0, -1, 9999])
async def test_miniapp_rejects_unconfigured_period_without_charge(sessions, context, period_days):
    before = await snapshot(sessions)
    with pytest.raises(HTTPException) as caught:
        await purchase(sessions, context, period_days=period_days)
    assert caught.value.status_code == 400
    assert await snapshot(sessions) == before
    context.effects.assert_not_awaited()


async def test_miniapp_daily_purchase_records_first_charge(sessions, context):
    async with sessions() as db:
        tariff = await db.get(Tariff, context.tariff_id)
        tariff.is_daily = True
        tariff.daily_price_kopeks = 1000
        await db.commit()
    response = await purchase(sessions, context, period_days=365)
    state = await snapshot(sessions)
    assert response.balance_kopeks == 49000
    assert state['subs'][0][5] is not None
    assert datetime.now(UTC) + timedelta(hours=23) < response.new_end_date < datetime.now(UTC) + timedelta(days=1)


async def renew(sessions, context, sub_id):
    async with sessions() as db:
        user = await lock_user_for_pricing(db, context.user_id)
        sub = await db.get(Subscription, sub_id)
        pricing = await pricing_engine.calculate_renewal_price(db, sub, 30, user=user)
        return await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)


@pytest.mark.parametrize('flow', ['miniapp', 'renewal'])
@pytest.mark.parametrize('failure', ['extension', 'ledger', 'commit', 'cancel', 'trial_cleanup'])
async def test_purchase_failure_rolls_back_all_money_and_subscription_state(
    sessions,
    context,
    monkeypatch,
    flow,
    failure,
):
    sub_id, _ = await seed_subscription(sessions, context)
    async with sessions() as db:
        user = await db.get(User, context.user_id)
        user.promo_offer_discount_percent = 10
        user.promo_offer_discount_source = 'test-offer'
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
    before = await snapshot(sessions)
    module = miniapp if flow == 'miniapp' else renewal

    async def fail(db, *args, **kwargs):
        if failure == 'cancel':
            raise asyncio.CancelledError
        await db.execute(text('SELECT 1/0'))

    if failure in {'extension', 'cancel'}:
        original = module.extend_subscription

        async def fail_after_extension(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            await fail(db)

        monkeypatch.setattr(module, 'extend_subscription', fail_after_extension)
    elif failure == 'ledger':
        monkeypatch.setattr(module, 'create_transaction', fail)
    elif failure == 'trial_cleanup':
        monkeypatch.setattr(subscription_crud, 'deactivate_user_trial_subscriptions', fail)
    else:
        # A deferred constraint rejects the actual COMMIT, after all writes flushed.
        async with sessions() as db:
            await db.execute(text('CREATE TABLE commit_guard (id integer PRIMARY KEY)'))
            await db.execute(
                text(
                    'ALTER TABLE transactions ADD CONSTRAINT test_deferred_failure '
                    'FOREIGN KEY (user_id) REFERENCES commit_guard(id) DEFERRABLE INITIALLY DEFERRED'
                )
            )
            await db.commit()
    try:
        expected = asyncio.CancelledError if failure == 'cancel' else DBAPIError
        message = (
            None if failure == 'cancel' else ('test_deferred_failure' if failure == 'commit' else 'division by zero')
        )
        with pytest.raises(expected, match=message):
            if flow == 'miniapp':
                await purchase(sessions, context)
            else:
                await renew(sessions, context, sub_id)
        assert await snapshot(sessions) == before
        context.effects.assert_not_awaited()
        context.panel.update_remnawave_user.assert_not_awaited()
    finally:
        if failure == 'commit':
            async with sessions() as db:
                await db.execute(text('ALTER TABLE transactions DROP CONSTRAINT test_deferred_failure'))
                await db.execute(text('DROP TABLE commit_guard'))
                await db.commit()


async def test_renewal_lock_timeout_restores_uncommitted_charge(sessions, context):
    sub_id, _ = await seed_subscription(sessions, context)
    before = await snapshot(sessions)
    async with sessions() as blocker, sessions() as db:
        await blocker.execute(select(Subscription.id).where(Subscription.id == sub_id).with_for_update())
        await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        user = await lock_user_for_pricing(db, context.user_id)
        sub = await db.get(Subscription, sub_id)
        pricing = await pricing_engine.calculate_renewal_price(db, sub, 30, user=user)
        with pytest.raises(DBAPIError, match='lock timeout'):
            await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)
    assert await snapshot(sessions) == before
    context.effects.assert_not_awaited()


@pytest.mark.parametrize('flow', ['miniapp', 'renewal'])
async def test_concurrent_purchases_cannot_spend_the_same_balance(sessions, context, flow):
    sub_id, old_end = await seed_subscription(sessions, context)
    async with sessions() as db:
        user = await db.get(User, context.user_id)
        user.balance_kopeks = 10000
        await db.commit()

    async def attempt():
        try:
            return await (purchase(sessions, context) if flow == 'miniapp' else renew(sessions, context, sub_id))
        except (HTTPException, renewal.SubscriptionRenewalChargeError):
            return None

    results = await asyncio.gather(attempt(), attempt())
    assert sum(result is not None for result in results) == 1
    state = await snapshot(sessions)
    assert state['users'][0][1] == 0
    assert state['subs'][0][1] == old_end + timedelta(days=30)
    assert len(state['ledger']) == 1 and state['ledger'][0][1] == -10000


async def test_concurrent_renewals_preserve_both_paid_periods(sessions, context):
    sub_id, old_end = await seed_subscription(sessions, context)
    await asyncio.gather(renew(sessions, context, sub_id), renew(sessions, context, sub_id))
    state = await snapshot(sessions)
    assert state['users'][0][1] == 30000
    assert state['subs'][0][1] == old_end + timedelta(days=60)
    assert len(state['ledger']) == 2


async def test_new_subscription_creation_failure_restores_balance(sessions, context, monkeypatch):
    original = subscription_crud.create_paid_subscription

    async def fail_after_insert(db, *args, **kwargs):
        await original(db, *args, **kwargs)
        await db.execute(text('SELECT 1/0'))

    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', fail_after_insert)
    before = await snapshot(sessions)
    with pytest.raises(DBAPIError, match='division by zero'):
        await purchase(sessions, context)
    assert await snapshot(sessions) == before
    context.effects.assert_not_awaited()


async def test_full_discount_is_consumed_with_successful_purchase(sessions, context):
    async with sessions() as db:
        user = await db.get(User, context.user_id)
        user.promo_offer_discount_percent = 100
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
    response = await purchase(sessions, context)
    state = await snapshot(sessions)
    assert response.success and response.balance_kopeks == 50000
    assert state['users'][0][2:4] == (True, 0)
    assert len(state['subs']) == 1
    assert state['ledger'][0][1] == 0


@pytest.mark.parametrize('fail_ledger', [False, True])
async def test_server_prices_share_renewal_transaction(sessions, context, monkeypatch, fail_ledger):
    sub_id, old_end = await seed_subscription(sessions, context)
    async with sessions() as db:
        server = ServerSquad(squad_uuid='test-squad', display_name='Test', price_kopeks=1000)
        db.add(server)
        await db.commit()
        server_id = server.id
    pricing = renewal.SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=10000,
        discounted_total=10000,
        final_total=10000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=10000,
        server_ids=[server_id],
        details={'servers_individual_prices': [1000]},
    )
    before = await snapshot(sessions)

    async def bad_ledger(db, *args, **kwargs):
        await db.execute(text('SELECT 1/0'))

    if fail_ledger:
        monkeypatch.setattr(renewal, 'create_transaction', bad_ledger)
    async with sessions() as db:
        user = await lock_user_for_pricing(db, context.user_id)
        sub = await db.get(Subscription, sub_id)
        if fail_ledger:
            with pytest.raises(DBAPIError, match='division by zero'):
                await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)
        else:
            await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)
    state = await snapshot(sessions)
    async with sessions() as db:
        prices = (await db.execute(select(SubscriptionServer.paid_price_kopeks))).scalars().all()
    if fail_ledger:
        assert state == before
        assert prices == []
        context.effects.assert_not_awaited()
    else:
        assert state['users'][0][1] == 40000
        assert state['subs'][0][1] == old_end + timedelta(days=30)
        assert prices == [1000]


async def test_renewal_rejects_another_users_subscription(sessions, context):
    sub_id, _ = await seed_subscription(sessions, context)
    async with sessions() as db:
        other = User(telegram_id=1002, balance_kopeks=50000)
        db.add(other)
        await db.commit()
        other_id = other.id
    before = await snapshot(sessions)
    async with sessions() as db:
        user = await lock_user_for_pricing(db, other_id)
        sub = await db.get(Subscription, sub_id)
        await db.refresh(sub, ['user', 'tariff'])
        pricing = await pricing_engine.calculate_renewal_price(db, sub, 30, user=user)
        with pytest.raises(NoResultFound):
            await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)
    assert await snapshot(sessions) == before
    context.effects.assert_not_awaited()
