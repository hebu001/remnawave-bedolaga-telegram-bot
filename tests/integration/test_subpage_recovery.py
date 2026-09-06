"""PostgreSQL regressions for immutable quotes and resumable Subpage payments."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.cabinet.routes import subpage as routes
from app.database.crud import transaction as transaction_crud
from app.database.crud.user import lock_user_for_pricing
from app.database.models import (
    RenewalSyncTask,
    SubpageInvoice,
    Subscription,
    Tariff,
    Transaction,
    User,
    WataPayment,
    YooKassaPayment,
)
from app.services import renewal_sync_service as sync, subpage_payment_service as service
from app.services.pricing_engine import pricing_engine
from app.services.subscription_renewal_service import SubscriptionRenewalService
from tests.integration.test_purchase_atomicity import (
    context,
    seed_subscription,
    sessions,
    snapshot,
)


__all__ = ['context', 'sessions']  # Re-export shared pytest fixtures.


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


@pytest_asyncio.fixture
async def order_context(sessions, context, monkeypatch):
    sub_id, old_end = await seed_subscription(sessions, context)
    async with sessions() as db:
        sub = await db.get(Subscription, sub_id)
        sub.remnawave_short_uuid = 'TestShortUuid123'
        await db.commit()
    monkeypatch.setattr(service.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(service.cache, 'set', AsyncMock(side_effect=AssertionError('new invoices must not use Redis')))
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr(routes, '_ensure_enabled', lambda: None)
    monkeypatch.setattr(routes, '_rate_limit', AsyncMock())
    return SimpleNamespace(**vars(context), sub_id=sub_id, old_end=old_end)


async def make_invoice(sessions, ctx, method='wata'):
    async with sessions() as db:
        user = await lock_user_for_pricing(db, ctx.user_id)
        sub = await db.get(Subscription, ctx.sub_id)
        price = await pricing_engine.calculate_renewal_price(db, sub, 30, user=user)
        token = await service.create_invoice_record(
            db,
            short_uuid=sub.remnawave_short_uuid,
            user=user,
            subscription=sub,
            pricing=price,
            method=method,
        )
        external_id = 'test-' + token
        await service.attach_invoice_payment(db, token, local_payment_id=None, provider_payment_id=external_id)
    return {
        'token': token,
        'amount': price.final_total,
        'method': method,
        'external_id': external_id,
        'metadata': {'purpose': 'subpage_renewal', 'invoice_token': token, 'order_version': '1'},
    }


async def notify(sessions, ctx, invoice, **overrides):
    args = dict(
        metadata=invoice['metadata'],
        payment_amount_kopeks=invoice['amount'],
        provider_payment_id=invoice['external_id'],
        provider_name=invoice['method'],
        payment_user_id=ctx.user_id,
    )
    args.update(overrides)
    async with sessions() as db:
        return await service.try_fulfill_subpage_renewal(db, **args)


async def order_state(sessions, token):
    async with sessions() as db:
        order = await db.get(SubpageInvoice, token)
        return (order.status, order.reason, order.deposit_transaction_id, order.renewal_transaction_id)


@pytest.mark.parametrize('method', ['wata', 'yookassa'])
async def test_parallel_webhook_replays_credit_and_extend_once(sessions, order_context, method):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx, method)
    assert all(await asyncio.gather(*(notify(sessions, ctx, invoice) for _ in range(4))))
    state = await snapshot(sessions)
    assert state['users'][0][1] == 50000
    assert state['subs'][0][1] == ctx.old_end + timedelta(days=30)
    assert [row[1] for row in state['ledger']] == [10000, -10000]
    status, reason, deposit, renewal = await order_state(sessions, invoice['token'])
    assert status == 'succeeded' and reason is None and deposit and renewal


@pytest.mark.parametrize(
    'change', ['devices', 'tariff_price_up', 'tariff_price_down', 'squads', 'external_squad', 'offer']
)
async def test_changed_quote_only_credits_money(sessions, order_context, change):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    async with sessions() as db:
        sub = await db.get(Subscription, ctx.sub_id)
        tariff = await db.get(Tariff, ctx.tariff_id)
        if change == 'devices':
            sub.device_limit = 8
        elif change == 'squads':
            sub.connected_squads = ['another-squad']
        elif change == 'external_squad':
            tariff.external_squad_uuid = 'another-external-squad'
        elif change == 'offer':
            user = await db.get(User, ctx.user_id)
            user.promo_offer_discount_percent = 20
            user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        else:
            tariff.period_prices = {'30': 20000 if change.endswith('up') else 5000}
        await db.commit()
    await notify(sessions, ctx, invoice)
    state = await snapshot(sessions)
    assert state['users'][0][1] == 60000
    assert state['subs'][0][1] == ctx.old_end
    assert len(state['ledger']) == 1 and state['ledger'][0][1] == 10000
    assert (await order_state(sessions, invoice['token']))[0] == 'credited_only'


async def test_one_time_discount_cannot_be_reused_by_two_invoices(sessions, order_context):
    ctx = order_context
    async with sessions() as db:
        user = await db.get(User, ctx.user_id)
        user.promo_offer_discount_percent = 10
        user.promo_offer_discount_source = 'one-time'
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
    first, second = await make_invoice(sessions, ctx), await make_invoice(sessions, ctx)
    assert first['amount'] == second['amount'] == 9000
    await asyncio.gather(notify(sessions, ctx, first), notify(sessions, ctx, second))
    state = await snapshot(sessions)
    assert state['users'][0][1] == 59000
    assert state['users'][0][3] == 0
    assert state['subs'][0][1] == ctx.old_end + timedelta(days=30)
    assert sorted(row[1] for row in state['ledger']) == [-9000, 9000, 9000]


async def test_crash_after_capture_is_resumed_without_webhook(sessions, order_context, monkeypatch):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    original = service.fulfill_paid_invoice
    monkeypatch.setattr(service, 'fulfill_paid_invoice', AsyncMock(side_effect=RuntimeError('process stopped')))
    with pytest.raises(RuntimeError, match='process stopped'):
        await notify(sessions, ctx, invoice)
    assert (await order_state(sessions, invoice['token']))[0] == 'paid'
    assert (await snapshot(sessions))['ledger'] == []
    monkeypatch.setattr(service, 'fulfill_paid_invoice', original)
    await service.process_pending_subpage_orders(session_factory=sessions)
    assert (await order_state(sessions, invoice['token']))[0] == 'succeeded'
    assert len((await snapshot(sessions))['ledger']) == 2


@pytest.mark.parametrize('failure', ['credit', 'renewal', 'commit', 'cancel'])
async def test_failed_fulfillment_is_not_acknowledged_or_partially_committed(
    sessions,
    order_context,
    monkeypatch,
    failure,
):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    before = await snapshot(sessions)
    if failure in ('credit', 'renewal'):
        original = transaction_crud.create_transaction

        async def failing_transaction(db, *args, **kwargs):
            if kwargs['type'].value == ('deposit' if failure == 'credit' else 'subscription_payment'):
                await db.execute(text('SELECT 1/0'))
            return await original(db, *args, **kwargs)

        if failure == 'credit':
            monkeypatch.setattr(transaction_crud, 'create_transaction', failing_transaction)
        else:
            from app.services import subscription_renewal_service

            monkeypatch.setattr(subscription_renewal_service, 'create_transaction', failing_transaction)
    elif failure == 'cancel':
        monkeypatch.setattr(SubscriptionRenewalService, 'finalize', AsyncMock(side_effect=asyncio.CancelledError))
    else:
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
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else DBAPIError):
            await notify(sessions, ctx, invoice)
        assert await snapshot(sessions) == before
        assert (await order_state(sessions, invoice['token']))[0] == 'paid'
        ctx.panel.update_remnawave_user.assert_not_awaited()
    finally:
        if failure == 'commit':
            async with sessions() as db:
                await db.execute(text('ALTER TABLE transactions DROP CONSTRAINT test_deferred_failure'))
                await db.execute(text('DROP TABLE commit_guard'))
                await db.commit()


async def test_existing_deposit_does_not_skip_unfinished_order(sessions, order_context):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    async with sessions() as db:
        user = await db.get(User, ctx.user_id)
        user.balance_kopeks += invoice['amount']
        tx = Transaction(
            user_id=ctx.user_id,
            type='deposit',
            amount_kopeks=invoice['amount'],
            external_id=invoice['external_id'],
            payment_method=invoice['method'],
            is_completed=True,
        )
        db.add(tx)
        await db.flush()
        order = await db.get(SubpageInvoice, invoice['token'])
        order.deposit_transaction_id = tx.id
        await db.commit()
    await notify(sessions, ctx, invoice)
    state = await snapshot(sessions)
    assert state['users'][0][1] == 50000
    assert state['subs'][0][1] == ctx.old_end + timedelta(days=30)
    assert len(state['ledger']) == 2


async def test_lost_response_after_commit_cannot_repeat_renewal(sessions, order_context, monkeypatch):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    async with sessions() as db:
        original_commit = db.commit
        commits = 0

        async def commit_then_crash():
            nonlocal commits
            await original_commit()
            commits += 1
            if commits == 2:
                raise RuntimeError('lost commit response')

        monkeypatch.setattr(db, 'commit', commit_then_crash)
        with pytest.raises(RuntimeError, match='lost commit response'):
            await service.try_fulfill_subpage_renewal(
                db,
                metadata=invoice['metadata'],
                payment_amount_kopeks=invoice['amount'],
                provider_payment_id=invoice['external_id'],
                provider_name=invoice['method'],
                payment_user_id=ctx.user_id,
            )
    state = await snapshot(sessions)
    await notify(sessions, ctx, invoice)
    assert await snapshot(sessions) == state
    assert (await order_state(sessions, invoice['token']))[0] == 'succeeded'
    await sync.process_pending_renewal_syncs(session_factory=sessions)
    async with sessions() as db:
        assert (await db.get(RenewalSyncTask, ctx.sub_id)).status == 'done'


async def test_invoice_ownership_survives_cache_loss_and_late_payment(sessions, order_context):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    async with sessions() as db:
        order = await db.get(SubpageInvoice, invoice['token'])
        order.created_at = datetime.now(UTC) - timedelta(days=4)
        await db.commit()
    request = SimpleNamespace(state=SimpleNamespace(subpage_short_uuid='TestShortUuid123'))
    async with sessions() as db:
        assert (await routes.get_subpage_invoice_status(invoice['token'], request, db)).status == 'pending'
    await notify(sessions, ctx, invoice)
    async with sessions() as db:
        response = await routes.get_subpage_invoice_status(invoice['token'], request, db)
        assert response.status == 'succeeded' and response.newExpiresAt
        request.state.subpage_short_uuid = 'WrongShortUuid'
        with pytest.raises(HTTPException) as caught:
            await routes.get_subpage_invoice_status(invoice['token'], request, db)
        assert caught.value.status_code == 404


@pytest.mark.parametrize('mismatch', ['owner', 'provider', 'payment_id'])
async def test_callback_binding_cannot_be_changed(sessions, order_context, mismatch):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    changes = {
        'owner': {'payment_user_id': ctx.user_id + 1},
        'provider': {'provider_name': 'yookassa'},
        'payment_id': {'provider_payment_id': 'another-payment'},
    }
    before = await snapshot(sessions)
    with pytest.raises(ValueError):
        await notify(sessions, ctx, invoice, **changes[mismatch])
    assert await snapshot(sessions) == before
    assert (await order_state(sessions, invoice['token']))[0] == 'pending'


async def test_amount_mismatch_credits_actual_local_invoice_amount_only(sessions, order_context):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    await notify(sessions, ctx, invoice, payment_amount_kopeks=9000)
    state = await snapshot(sessions)
    assert state['users'][0][1] == 59000 and state['subs'][0][1] == ctx.old_end
    assert (await order_state(sessions, invoice['token']))[:2] == ('credited_only', 'amount_mismatch')


async def test_foreign_currency_is_held_for_review(sessions, order_context):
    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    before = await snapshot(sessions)
    await notify(sessions, ctx, invoice, currency='USD')
    assert await snapshot(sessions) == before
    assert (await order_state(sessions, invoice['token']))[:2] == ('review', 'currency_mismatch')


@pytest.mark.parametrize('already_credited', [False, True])
async def test_legacy_unknown_quote_never_buys_changed_configuration(sessions, order_context, already_credited):
    ctx = order_context
    token = uuid4().hex
    invoice = {
        'token': token,
        'amount': 10000,
        'method': 'wata',
        'external_id': 'legacy-' + token,
        'metadata': {
            'purpose': 'subpage_renewal',
            'invoice_token': token,
            'subscription_id': str(ctx.sub_id),
            'period_days': '30',
            'expected_amount_kopeks': '10000',
        },
    }
    if already_credited:
        async with sessions() as db:
            db.add(
                Transaction(
                    user_id=ctx.user_id,
                    type='deposit',
                    amount_kopeks=10000,
                    external_id=invoice['external_id'],
                    payment_method='wata',
                    is_completed=True,
                )
            )
            await db.commit()
    await notify(sessions, ctx, invoice)
    state = await snapshot(sessions)
    assert state['subs'][0][1] == ctx.old_end
    assert len(state['ledger']) == 1
    assert state['users'][0][1] == (50000 if already_credited else 60000)
    assert (await order_state(sessions, token))[0] == ('review' if already_credited else 'credited_only')


@pytest.mark.parametrize('method', ['wata', 'yookassa'])
async def test_provider_routes_linked_payment_to_unfinished_subpage_order(sessions, order_context, method):
    from app.services.payment.wata import WataPaymentMixin
    from app.services.payment.yookassa import YooKassaPaymentMixin

    ctx = order_context
    invoice = await make_invoice(sessions, ctx, method)
    async with sessions() as db:
        user = await db.get(User, ctx.user_id)
        user.balance_kopeks += invoice['amount']
        deposit = Transaction(
            user_id=ctx.user_id,
            type='deposit',
            amount_kopeks=invoice['amount'],
            external_id=invoice['external_id'],
            payment_method=method,
            is_completed=True,
        )
        db.add(deposit)
        await db.flush()
        kwargs = dict(
            user_id=ctx.user_id,
            amount_kopeks=invoice['amount'],
            currency='RUB',
            metadata_json=invoice['metadata'],
            transaction_id=deposit.id,
            is_paid=True,
        )
        if method == 'wata':
            payment = WataPayment(
                payment_link_id=invoice['external_id'], order_id=invoice['token'], status='Paid', **kwargs
            )
        else:
            payment = YooKassaPayment(yookassa_payment_id=invoice['external_id'], status='succeeded', **kwargs)
        db.add(payment)
        await db.commit()
        if method == 'wata':
            result = await WataPaymentMixin().process_wata_webhook(
                db,
                {
                    'orderId': invoice['token'],
                    'paymentLinkId': invoice['external_id'],
                    'transactionStatus': 'Paid',
                },
            )
        else:
            result = await YooKassaPaymentMixin()._process_successful_yookassa_payment(db, payment)
        assert result is True
    state = await snapshot(sessions)
    assert state['users'][0][1] == 50000
    assert state['subs'][0][1] == ctx.old_end + timedelta(days=30)
    assert len(state['ledger']) == 2
    assert (await order_state(sessions, invoice['token']))[0] == 'succeeded'


@pytest.mark.parametrize('method', ['wata', 'yookassa'])
async def test_provider_does_not_acknowledge_database_error(sessions, order_context, monkeypatch, method):
    from app.services.payment.wata import WataPaymentMixin
    from app.services.payment.yookassa import YooKassaPaymentMixin

    ctx = order_context
    invoice = await make_invoice(sessions, ctx, method)

    async def fail(db, *args, **kwargs):
        await db.execute(text('SELECT 1/0'))

    monkeypatch.setattr(transaction_crud, 'create_transaction', fail)
    async with sessions() as db:
        kwargs = dict(
            user_id=ctx.user_id, amount_kopeks=invoice['amount'], currency='RUB', metadata_json=invoice['metadata']
        )
        if method == 'wata':
            payment = WataPayment(payment_link_id=invoice['external_id'], order_id=invoice['token'], **kwargs)
        else:
            payment = YooKassaPayment(
                yookassa_payment_id=invoice['external_id'], status='succeeded', is_paid=True, **kwargs
            )
        db.add(payment)
        await db.commit()
        if method == 'wata':
            with pytest.raises(DBAPIError):
                await WataPaymentMixin().process_wata_webhook(
                    db,
                    {
                        'orderId': invoice['token'],
                        'paymentLinkId': invoice['external_id'],
                        'transactionStatus': 'Paid',
                    },
                )
        else:
            assert await YooKassaPaymentMixin()._process_successful_yookassa_payment(db, payment) is False
    assert (await order_state(sessions, invoice['token']))[0] == 'paid'
    assert (await snapshot(sessions))['ledger'] == []


@pytest.mark.parametrize('method', ['wata', 'yookassa'])
async def test_quote_is_durable_before_provider_and_early_callback_is_not_overwritten(
    sessions,
    order_context,
    monkeypatch,
    method,
):
    ctx = order_context
    monkeypatch.setattr(
        routes, '_available_methods', AsyncMock(return_value=[routes.SubpageMethod(id=method, name=method)])
    )
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))

    async def provider(**kwargs):
        metadata = kwargs['metadata']
        token = metadata['invoice_token']
        async with sessions() as observer:
            row = await observer.get(SubpageInvoice, token)
            assert row.status == 'pending' and row.configuration and row.pricing
        invoice = {
            'token': token,
            'metadata': metadata,
            'amount': kwargs['amount_kopeks'],
            'external_id': 'early-' + token,
            'method': method,
        }
        await notify(sessions, ctx, invoice)
        return {
            'local_payment_id': None,
            'payment_link_id': invoice['external_id'],
            'yookassa_payment_id': invoice['external_id'],
            'confirmation_url': 'https://example.test/pay',
            'payment_url': 'https://example.test/pay',
        }

    fake_provider = SimpleNamespace(create_wata_payment=provider, create_yookassa_payment=provider)
    monkeypatch.setattr('app.services.payment_service.PaymentService', lambda: fake_provider)
    async with sessions() as db:
        result = await routes.create_subpage_invoice(
            'TestShortUuid123',
            routes.SubpageInvoiceRequest(periodDays=30, method=method),
            SimpleNamespace(),
            db,
        )
    assert (await order_state(sessions, result.invoiceToken))[0] == 'succeeded'
    assert len((await snapshot(sessions))['ledger']) == 2


async def test_wata_stores_subpage_metadata_in_initial_payment_insert(sessions, order_context):
    from app.services.payment.wata import WataPaymentMixin

    ctx = order_context
    service_instance = WataPaymentMixin()
    service_instance.wata_service = SimpleNamespace(
        create_payment_link=AsyncMock(
            return_value={'id': str(uuid4()), 'url': 'https://example.test/pay'},
        )
    )
    metadata = {'purpose': 'subpage_renewal', 'invoice_token': uuid4().hex, 'order_version': '1'}
    async with sessions() as db:
        result = await service_instance.create_wata_payment(
            db,
            ctx.user_id,
            10000,
            'test order',
            metadata=metadata,
        )
    async with sessions() as observer:
        payment = await observer.scalar(select(WataPayment).where(WataPayment.id == result['local_payment_id']))
        assert all(payment.metadata_json[key] == value for key, value in metadata.items())


async def test_confirmed_legacy_success_is_adopted_without_changing_money(sessions, order_context, monkeypatch):
    ctx = order_context
    token = uuid4().hex
    invoice = {
        'token': token,
        'amount': 10000,
        'method': 'wata',
        'external_id': 'legacy-' + token,
        'metadata': {
            'purpose': 'subpage_renewal',
            'invoice_token': token,
            'subscription_id': str(ctx.sub_id),
            'period_days': '30',
            'expected_amount_kopeks': '10000',
        },
    }
    async with sessions() as db:
        db.add(
            Transaction(
                user_id=ctx.user_id,
                type='deposit',
                amount_kopeks=10000,
                external_id=invoice['external_id'],
                payment_method='wata',
                is_completed=True,
            )
        )
        await db.commit()
    monkeypatch.setattr(
        service.cache,
        'get',
        AsyncMock(
            return_value={
                'user_id': ctx.user_id,
                'short_uuid': 'TestShortUuid123',
                'status': 'succeeded',
                'new_expires_at': ctx.old_end.isoformat(),
            }
        ),
    )
    before = await snapshot(sessions)
    await notify(sessions, ctx, invoice)
    assert await snapshot(sessions) == before
    assert (await order_state(sessions, token))[0] == 'succeeded'


async def test_recovery_loop_drains_durable_work_with_empty_legacy_queue(sessions, order_context, monkeypatch):
    from app.services import subscription_renewal_service as renewal
    from app.services.remnawave_retry_queue import RemnaWaveRetryQueue

    ctx = order_context
    invoice = await make_invoice(sessions, ctx)
    fulfill = service.fulfill_paid_invoice
    monkeypatch.setattr(service, 'fulfill_paid_invoice', AsyncMock(side_effect=RuntimeError('process interrupted')))
    with pytest.raises(RuntimeError, match='process interrupted'):
        await notify(sessions, ctx, invoice)
    monkeypatch.setattr(service, 'fulfill_paid_invoice', fulfill)
    monkeypatch.setattr(renewal, 'process_renewal_sync', AsyncMock(return_value=False))
    orders_worker, panel_worker = service.process_pending_subpage_orders, sync.process_pending_renewal_syncs
    monkeypatch.setattr(service, 'process_pending_subpage_orders', lambda: orders_worker(session_factory=sessions))
    monkeypatch.setattr(sync, 'process_pending_renewal_syncs', lambda: panel_worker(session_factory=sessions))
    queue = RemnaWaveRetryQueue()
    assert queue.pending_count == 0
    await queue.process_pending()
    assert (await order_state(sessions, invoice['token']))[0] == 'succeeded'
    async with sessions() as db:
        assert (await db.get(RenewalSyncTask, ctx.sub_id)).status == 'done'
    assert len((await snapshot(sessions))['ledger']) == 2
