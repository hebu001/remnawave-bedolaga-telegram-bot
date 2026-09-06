"""C12: activation runs real money writes, locks and rollback on PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.config import settings
from app.database.crud import subscription as subscription_crud, transaction as transaction_crud
from app.database.models import RenewalSyncTask, ServerSquad, Subscription, Transaction, User
from app.handlers.menu import handle_activate_button
from app.services import renewal_sync_service
from app.services.pricing_engine import pricing_engine
from app.services.subscription_renewal_service import SubscriptionRenewalService
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


@pytest_asyncio.fixture
async def activation(sessions, monkeypatch):
    async with sessions() as db:
        await db.execute(text('TRUNCATE users, tariffs, server_squads RESTART IDENTITY CASCADE'))
        user = User(
            telegram_id=987654321,
            balance_kopeks=50000,
            language='ru',
            promo_offer_discount_percent=10,
            promo_offer_discount_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        server = ServerSquad(display_name='Activation test', squad_uuid='activation-squad', price_kopeks=0)
        db.add_all([user, server])
        await db.commit()
        user_id = user.id

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: False)
    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda _: [30])
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(
        pricing_engine,
        'calculate_classic_new_subscription_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10000)),
    )
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())

    # Provider calls must observe the entire durable purchase, never a partial debit.
    async def sync(subscription_id, **_kwargs):
        async with sessions() as observer:
            assert await observer.scalar(select(User.balance_kopeks).where(User.id == user_id)) == 40000
            assert await observer.scalar(select(func.count()).select_from(Transaction)) == 1
            assert await observer.get(Subscription, subscription_id) is not None
            assert await observer.get(RenewalSyncTask, subscription_id) is not None
        return False  # Panel unavailable: the durable intent remains pending.

    inline_sync = AsyncMock(side_effect=sync)
    monkeypatch.setattr(renewal_sync_service, 'process_renewal_sync', inline_sync)
    return SimpleNamespace(user_id=user_id, inline_sync=inline_sync)


async def activate(sessions, user_id, barrier=None):
    callback = SimpleNamespace(answer=AsyncMock())
    async with sessions() as db:
        user = await db.get(User, user_id)
        if barrier:
            await barrier.wait()
        await handle_activate_button(callback, user, db)
    return callback


async def test_new_activation_commits_before_panel_and_does_not_charge_again(sessions, activation):
    first = await activate(sessions, activation.user_id)
    assert 'Повторная оплата не нужна' in first.answer.await_args.args[0]
    second = await activate(sessions, activation.user_id)
    assert 'уже активна' in second.answer.await_args.args[0]
    async with sessions() as db:
        user = await db.get(User, activation.user_id)
        assert user.balance_kopeks == 40000
        assert user.promo_offer_discount_percent == 0
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await db.scalar(select(Transaction.amount_kopeks)) == -10000
        assert await db.scalar(select(Subscription.status)) == 'active'
        assert await db.scalar(select(RenewalSyncTask.status)) == 'pending'
        assert await db.scalar(select(ServerSquad.current_users)) == 1
    activation.inline_sync.assert_awaited_once()


async def test_concurrent_first_activation_only_debits_once(sessions, activation):
    barrier = asyncio.Barrier(2)
    callbacks = await asyncio.gather(*(activate(sessions, activation.user_id, barrier) for _ in range(2)))
    messages = [callback.answer.await_args.args[0] for callback in callbacks]
    assert sum('уже активна' in message for message in messages) == 1
    async with sessions() as db:
        assert await db.scalar(select(User.balance_kopeks)) == 40000
        assert await db.scalar(select(func.count()).select_from(Subscription)) == 1
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await db.scalar(select(func.count()).select_from(RenewalSyncTask)) == 1


@pytest.mark.parametrize('failure_stage', ['subscription', 'transaction', 'sync_intent', 'cancelled'])
async def test_activation_failure_rolls_back_money_subscription_promo_and_counters(
    sessions, activation, monkeypatch, failure_stage
):
    module, method = {
        'subscription': (subscription_crud, 'create_paid_subscription'),
        'transaction': (transaction_crud, 'create_transaction'),
        'sync_intent': (renewal_sync_service, 'schedule_renewal_sync'),
        'cancelled': (renewal_sync_service, 'schedule_renewal_sync'),
    }[failure_stage]
    original = getattr(module, method)

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        if failure_stage == 'cancelled':
            raise asyncio.CancelledError
        raise RuntimeError('injected local failure')

    monkeypatch.setattr(module, method, fail_after_write)
    if failure_stage == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            await activate(sessions, activation.user_id)
    else:
        callback = await activate(sessions, activation.user_id)
        assert 'Ошибка активации' in callback.answer.await_args.args[0]
    async with sessions() as db:
        user = await db.get(User, activation.user_id)
        assert user.balance_kopeks == 50000
        assert user.promo_offer_discount_percent == 10
        for model in (Subscription, Transaction, RenewalSyncTask):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert await db.scalar(select(ServerSquad.current_users)) == 0
    activation.inline_sync.assert_not_awaited()


async def test_empty_balance_does_not_create_subscription_or_call_panel(sessions, activation):
    async with sessions() as db:
        user = await db.get(User, activation.user_id)
        user.balance_kopeks = 0
        await db.commit()
    callback = await activate(sessions, activation.user_id)
    assert 'Недостаточно средств' in callback.answer.await_args.args[0]
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Subscription)) == 0
    activation.inline_sync.assert_not_awaited()


async def test_real_deferred_commit_failure_rolls_back_complete_activation(sessions, activation):
    async with sessions() as db:
        await db.execute(text('CREATE TABLE activation_commit_guard (id integer PRIMARY KEY)'))
        await db.execute(
            text(
                'ALTER TABLE transactions ADD CONSTRAINT activation_commit_failure '
                'FOREIGN KEY (user_id) REFERENCES activation_commit_guard(id) DEFERRABLE INITIALLY DEFERRED'
            )
        )
        await db.commit()
    try:
        callback = await activate(sessions, activation.user_id)
        assert 'Ошибка активации' in callback.answer.await_args.args[0]
        async with sessions() as db:
            user = await db.get(User, activation.user_id)
            assert user.balance_kopeks == 50000
            assert user.promo_offer_discount_percent == 10
            for model in (Subscription, Transaction, RenewalSyncTask):
                assert await db.scalar(select(func.count()).select_from(model)) == 0
            assert await db.scalar(select(ServerSquad.current_users)) == 0
        activation.inline_sync.assert_not_awaited()
    finally:
        async with sessions() as db:
            await db.execute(text('ALTER TABLE transactions DROP CONSTRAINT activation_commit_failure'))
            await db.execute(text('DROP TABLE activation_commit_guard'))
            await db.commit()


@pytest.mark.parametrize('late_failure', ['after_commit', 'telegram_answer'])
async def test_renewal_remains_paid_after_side_effect_failure_and_repeat_is_free(
    sessions, activation, monkeypatch, late_failure
):
    async with sessions() as db:
        db.add(
            Subscription(
                user_id=activation.user_id,
                remnawave_short_id='existing-sub',
                status='active',
                is_trial=False,
                end_date=datetime.now(UTC) - timedelta(days=1),
                connected_squads=['activation-squad'],
            )
        )
        await db.commit()
    monkeypatch.setattr(
        pricing_engine,
        'calculate_renewal_price',
        AsyncMock(
            return_value=SimpleNamespace(
                final_total=10000,
                period_days=30,
                promo_offer_discount=1000,
                breakdown={},
            )
        ),
    )

    async def post_commit(_self, _db, _user, result, **_kwargs):
        async with sessions() as observer:
            assert await observer.scalar(select(User.balance_kopeks)) == 40000
            assert await observer.get(RenewalSyncTask, result.subscription.id) is not None
            assert await observer.scalar(select(func.count()).select_from(Transaction)) == 1
        if late_failure == 'after_commit':
            raise RuntimeError('after-commit notification failed')

    monkeypatch.setattr(SubscriptionRenewalService, 'after_commit', post_commit)
    callback = SimpleNamespace(
        answer=AsyncMock(
            side_effect=[RuntimeError('Telegram failed'), None] if late_failure == 'telegram_answer' else None
        )
    )
    async with sessions() as db:
        user = await db.get(User, activation.user_id)
        await handle_activate_button(callback, user, db)
    if late_failure == 'telegram_answer':
        assert 'Повторная оплата не нужна' in callback.answer.await_args.args[0]
    repeated = await activate(sessions, activation.user_id)
    assert 'уже активна' in repeated.answer.await_args.args[0]
    async with sessions() as db:
        assert await db.scalar(select(User.balance_kopeks)) == 40000
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await db.scalar(select(func.count()).select_from(Subscription)) == 1
