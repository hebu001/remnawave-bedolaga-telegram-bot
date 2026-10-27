"""Actual PostgreSQL settlement; provider reads and notification delivery are isolated."""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.crud import cardlink as crud
from app.database.crud.transaction import get_income_total, income_payments_query
from app.database.models import CardlinkPayment, GuestPurchase, SubpageInvoice, Transaction, User
from app.services.cardlink_service import CardlinkAPIError, CardlinkService
from app.services.event_emitter import event_emitter
from app.services.payment_service import PaymentService
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = [
    CardlinkPayment.__table__,
    Transaction.__table__,
    User.__table__,
    GuestPurchase.__table__,
    SubpageInvoice.__table__,
]
AMOUNT = 15001
INITIAL = 700


@pytest.fixture
def service(monkeypatch):
    svc = PaymentService(bot=None)
    svc.cardlink_service = Mock()
    svc.cardlink_service.is_available.return_value = True
    svc.cardlink_service.verify_callback.return_value = True
    svc.cardlink_service.get_bill = AsyncMock()
    svc.cardlink_service.create_payment_link = AsyncMock(
        return_value={'bill_id': 'bill-123', 'link_page_url': 'https://cardlink.link/bill/bill-123'}
    )
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', AsyncMock())
    return svc


async def seed(db, service, *, metadata=None, guest=False):
    user = User(telegram_id=905101, first_name='Cardlink fixture', language='ru', balance_kopeks=INITIAL)
    db.add(user)
    await db.commit()
    payment = await crud.create_cardlink_payment(
        db,
        user_id=None if guest else user.id,
        bill_id='bill-123',
        order_id='order-123',
        amount_kopeks=AMOUNT,
        metadata=metadata,
    )
    bill = {
        'id': payment.bill_id,
        'order_id': payment.order_id,
        'amount': '150.01',
        'currency_in': 'RUB',
        'type': 'NORMAL',
        'status': 'SUCCESS',
    }
    service.cardlink_service.get_bill.return_value = bill
    callback = {
        'InvId': payment.order_id,
        'TrsId': payment.bill_id,
        'CurrencyIn': 'RUB',
        'OutSum': '150.01',
        'Commission': '0.00',
        'Status': 'SUCCESS',
        'SignatureValue': 'verified-by-client-fixture',
    }
    return user.id, payment.id, callback


async def deliver(service, db, callback, *, poll=False, payment_id=None):
    try:
        if poll:
            return await service.get_cardlink_payment_status(db, payment_id)
        return await service.process_cardlink_webhook(db, deepcopy(callback))
    finally:
        await db.rollback()


async def assert_credit(db, user_id, payment_id, *, credited=True):
    db.expunge_all()
    user = await db.get(User, user_id)
    receipt = await db.get(CardlinkPayment, payment_id)
    assert user.balance_kopeks == INITIAL + (AMOUNT if credited else 0)
    assert receipt.is_paid is credited
    deposits = (await db.scalars(select(Transaction).where(Transaction.payment_method == 'cardlink'))).all()
    assert len(deposits) == int(credited)
    if credited:
        assert receipt.transaction_id == deposits[0].id
        assert deposits[0].external_id == receipt.bill_id
        assert deposits[0].amount_kopeks == AMOUNT
        assert receipt.paid_at is not None
    income = income_payments_query()
    assert await db.scalar(select(func.count()).select_from(income)) == int(credited)
    now = datetime.now(UTC)
    assert await get_income_total(db, now - timedelta(days=1), now + timedelta(days=1)) == (AMOUNT if credited else 0)
    await db.rollback()


@pytest.mark.parametrize('status', ['SUCCESS', 'OVERPAID'])
async def test_replay_and_stale_notification_credit_once(postgres_database, service, status):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        service.cardlink_service.get_bill.return_value['status'] = status
        callback['OutSum'] = '170.00'  # extra/commission never becomes an extra balance credit
        for _ in range(3):
            assert await deliver(service, db, callback)
        service.cardlink_service.get_bill.return_value['status'] = 'FAIL'
        callback['Status'] = 'FAIL'
        assert await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id)
        assert (await watcher.get(CardlinkPayment, payment_id)).status == status
        event_emitter.emit.assert_awaited_once()


@pytest.mark.parametrize('second_is_poll', [False, True])
async def test_competing_callback_and_poll_credit_once(postgres_database, service, monkeypatch, second_is_poll):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user_id, payment_id, callback = await seed(first, service)
        original = crud.get_cardlink_payment_by_id_for_update
        locked = asyncio.Event()
        entries = 0
        saw_waiter = False

        async def observe(db, local_id):
            nonlocal entries, saw_waiter
            entries += 1
            if entries == 1:
                result = await original(db, local_id)
                locked.set()
                saw_waiter = await lock_waiter_appeared(watcher)
                return result
            await locked.wait()
            return await original(db, local_id)

        monkeypatch.setattr(crud, 'get_cardlink_payment_by_id_for_update', observe)
        async with asyncio.timeout(15):
            outcomes = await asyncio.gather(
                deliver(service, first, callback),
                deliver(service, second, callback, poll=second_is_poll, payment_id=payment_id),
            )
        assert all(outcomes)
        assert entries == 2 and saw_waiter
        await assert_credit(watcher, user_id, payment_id)
        event_emitter.emit.assert_awaited_once()


@pytest.mark.parametrize(
    ('field', 'bad'),
    [
        ('id', 'another-bill'),
        ('order_id', 'another-order'),
        ('currency_in', 'USD'),
        ('currency_in', None),
        ('type', 'MULTI'),
        ('type', None),
        ('amount', '150.00'),
        ('amount', '150.02'),
        ('amount', '150.001'),
        ('amount', 'NaN'),
        ('amount', True),
        ('amount', None),
        ('status', None),
    ],
)
@pytest.mark.parametrize('entrypoint', ['webhook', 'poll'])
async def test_mismatched_api_proof_never_credits(postgres_database, service, field, bad, entrypoint):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        service.cardlink_service.get_bill.return_value[field] = bad
        if entrypoint == 'webhook':
            assert await service.process_cardlink_webhook(db, callback) is False
        else:
            assert await service.get_cardlink_payment_status(db, payment_id) is None
        # A normal False/None response does not imply that the request's DB
        # dependency rolls back. Rejected evidence must survive an explicit commit.
        await db.commit()
        await assert_credit(watcher, user_id, payment_id, credited=False)
        receipt = await watcher.get(CardlinkPayment, payment_id)
        assert receipt.status == 'NEW'
        assert receipt.paid_at is None
        assert receipt.transaction_id is None
        assert receipt.callback_payload is None
        assert 'verified_bill' not in (receipt.metadata_json or {})


@pytest.mark.parametrize('scenario', ['underpaid', 'smaller-api-amount', 'foreign-api-currency', 'api-unavailable'])
async def test_valid_signature_cannot_override_api_money_or_status(postgres_database, service, monkeypatch, scenario):
    from app.services.guest_purchase_service import _find_succeeded_provider_payment

    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service, metadata={'purchase_token': 'money-proof'})
        original_bill = deepcopy(service.cardlink_service.get_bill.return_value)
        bill = deepcopy(original_bill)
        if scenario == 'underpaid':
            bill['status'] = 'UNDERPAID'
        elif scenario == 'smaller-api-amount':
            bill['amount'] = '1.00'
        elif scenario == 'foreign-api-currency':
            bill['currency_in'] = 'USD'
        api = AsyncMock(return_value=bill)
        if scenario == 'api-unavailable':
            api.side_effect = CardlinkAPIError('unavailable')
        real_client = CardlinkService(
            base_url='https://cardlink.link', api_token='fixture-money-secret', shop_id='fixture-shop'
        )
        monkeypatch.setattr(real_client, 'get_bill', api)
        service.cardlink_service = real_client

        # A provider-signed partial payment is replayed with unsigned Status
        # replaced by SUCCESS. The signature is valid; only API proof may settle.
        callback['OutSum'] = '1.00'
        callback['Status'] = 'SUCCESS'
        signed = f'{callback["OutSum"]}:{callback["InvId"]}:fixture-money-secret'
        callback['SignatureValue'] = hashlib.md5(signed.encode(), usedforsecurity=False).hexdigest().upper()
        assert real_client.verify_callback(callback)
        result = await service.process_cardlink_webhook(db, callback)
        assert result is (scenario == 'underpaid')
        await db.commit()
        await assert_credit(watcher, user_id, payment_id, credited=False)
        assert await _find_succeeded_provider_payment(watcher, 'cardlink', 'money-proof') is None
        await watcher.rollback()

        # A later fully verified payment still succeeds exactly once.
        api.side_effect = None
        api.return_value = original_bill
        callback['OutSum'] = '150.01'
        signed = f'{callback["OutSum"]}:{callback["InvId"]}:fixture-money-secret'
        callback['SignatureValue'] = hashlib.md5(signed.encode(), usedforsecurity=False).hexdigest().upper()
        for _ in range(2):
            assert await service.process_cardlink_webhook(db, callback)
        await assert_credit(watcher, user_id, payment_id)


@pytest.mark.parametrize('status', ['NEW', 'PROCESS', 'UNDERPAID', 'FAIL', 'CANCEL', 'unknown'])
async def test_unsigned_success_never_overrides_api_nonpaid(postgres_database, service, status):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        service.cardlink_service.get_bill.return_value['status'] = status
        result = await deliver(service, db, callback)
        assert result is (status in {'NEW', 'PROCESS', 'UNDERPAID', 'FAIL'})
        await assert_credit(watcher, user_id, payment_id, credited=False)


@pytest.mark.parametrize('purpose', ['balance', 'guest_purchase', 'subpage_renewal'])
@pytest.mark.parametrize('field,bad', [('amount', '1.00'), ('currency_in', 'USD'), ('status', 'UNDERPAID')])
async def test_finalizer_rejects_invalid_evidence_before_durable_capture(
    postgres_database, service, purpose, field, bad
):
    from app.services.guest_purchase_service import _find_succeeded_provider_payment

    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, _ = await seed(
            db, service, metadata={'purpose': purpose, 'purchase_token': 'finalizer-proof'}
        )
        payment = await crud.get_cardlink_payment_by_id_for_update(db, payment_id)
        bill = {**service.cardlink_service.get_bill.return_value, field: bad}
        with pytest.raises(ValueError, match='cardlink_payment_evidence_mismatch'):
            await service._finalize_cardlink_payment(db, payment, bill)
        await db.commit()
        await assert_credit(watcher, user_id, payment_id, credited=False)
        receipt = await watcher.get(CardlinkPayment, payment_id)
        assert receipt.status == 'NEW' and receipt.paid_at is None
        assert await _find_succeeded_provider_payment(watcher, 'cardlink', 'finalizer-proof') is None


@pytest.mark.parametrize('invalid', ['signature', 'order', 'bill', 'currency', 'unavailable'])
async def test_callback_and_api_failure_never_credits(postgres_database, service, invalid):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        if invalid == 'signature':
            service.cardlink_service.verify_callback.return_value = False
        elif invalid == 'order':
            callback['InvId'] = 'different'
        elif invalid == 'bill':
            callback['TrsId'] = 'different'
        elif invalid == 'currency':
            callback['CurrencyIn'] = 'USD'
        else:
            service.cardlink_service.get_bill.side_effect = CardlinkAPIError('unavailable')
        assert await deliver(service, db, callback) is False
        await assert_credit(watcher, user_id, payment_id, credited=False)
        if invalid != 'unavailable':
            service.cardlink_service.get_bill.assert_not_awaited()


async def test_partial_ledger_failure_rolls_back_receipt_and_balance(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        original = crud.link_cardlink_payment_to_transaction

        async def fail_after_link(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('injected before atomic commit')

        monkeypatch.setattr(crud, 'link_cardlink_payment_to_transaction', fail_after_link)
        with pytest.raises(RuntimeError, match='injected'):
            await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id, credited=False)
        monkeypatch.setattr(crud, 'link_cardlink_payment_to_transaction', original)
        assert await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id)


async def test_guest_creation_has_metadata_in_first_committed_record(postgres_database, service):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        result = await service.create_guest_payment(
            db,
            amount_kopeks=AMOUNT,
            payment_method='cardlink',
            description='Gift fixture',
            purchase_token='gift-token',
            return_url='https://example.test/return',
        )
        payment = await crud.get_cardlink_payment_by_bill_id(watcher, result['payment_id'])
        assert payment.user_id is None
        assert payment.metadata_json['purpose'] == 'guest_purchase'
        assert payment.metadata_json['purchase_token'] == 'gift-token'
        assert result['provider'] == 'cardlink'


async def test_paid_gift_capture_and_recovery_have_one_receipt_no_balance_credit(
    postgres_database, service, monkeypatch
):
    monkeypatch.setattr('app.services.guest_purchase_service.notify_gift_claim_available', AsyncMock())
    from app.services.guest_purchase_service import _find_succeeded_provider_payment

    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        metadata = {'purpose': 'guest_purchase', 'purchase_token': 'gift-token'}
        user_id, payment_id, callback = await seed(db, service, metadata=metadata, guest=True)
        purchase = GuestPurchase(
            token='gift-token',
            contact_type='email',
            contact_value='fixture@example.test',
            is_gift=True,
            period_days=30,
            amount_kopeks=AMOUNT,
            payment_method='cardlink',
        )
        db.add(purchase)
        await db.commit()
        for _ in range(2):
            assert await deliver(service, db, callback)
        found = await watcher.scalar(select(GuestPurchase).where(GuestPurchase.token == 'gift-token'))
        assert found.status == 'paid' and found.paid_at is not None and found.payment_id == 'bill-123'
        assert (await watcher.get(User, user_id)).balance_kopeks == INITIAL
        assert await watcher.scalar(select(func.count()).select_from(Transaction)) == 0
        assert await _find_succeeded_provider_payment(watcher, 'cardlink', 'gift-token') == ('bill-123', AMOUNT)
        assert await watcher.scalar(select(func.sum(income_payments_query().c.amount_kopeks))) == AMOUNT


async def test_subpage_capture_is_durable_and_replay_resumes_fulfillment(postgres_database, service, monkeypatch):
    fulfill = AsyncMock()
    monkeypatch.setattr('app.services.subpage_payment_service.fulfill_paid_invoice', fulfill)
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        metadata = {'purpose': 'subpage_renewal', 'invoice_token': 'subpage-token', 'order_version': '1'}
        user_id, payment_id, callback = await seed(db, service, metadata=metadata)
        db.add(
            SubpageInvoice(
                token='subpage-token',
                user_id=user_id,
                short_uuid='fixture123',
                period_days=30,
                amount_kopeks=AMOUNT,
                method='cardlink',
                provider_payment_id='bill-123',
            )
        )
        await db.commit()
        for _ in range(2):
            assert await deliver(service, db, callback)
        invoice = await watcher.get(SubpageInvoice, 'subpage-token')
        receipt = await watcher.get(CardlinkPayment, payment_id)
        assert invoice.status == 'paid' and invoice.paid_amount_kopeks == AMOUNT
        assert receipt.is_paid
        assert (await watcher.get(User, user_id)).balance_kopeks == INITIAL
        assert await watcher.scalar(select(func.count()).select_from(Transaction)) == 0
        assert fulfill.await_count == 2


async def test_disabled_creation_still_verifies_existing_bill(postgres_database, service):
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        service.cardlink_service.is_available.return_value = False
        assert await service.create_cardlink_payment(db, user_id, AMOUNT, 'disabled') is None
        assert await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id)


async def test_guest_receipt_survives_fulfillment_failure_and_replay_recovers(postgres_database, service, monkeypatch):
    from app.services.payment import common

    real_fulfill = common.try_fulfill_guest_purchase
    monkeypatch.setattr('app.services.guest_purchase_service.notify_gift_claim_available', AsyncMock())
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        metadata = {'purpose': 'guest_purchase', 'purchase_token': 'gift-retry-token'}
        user_id, payment_id, callback = await seed(db, service, metadata=metadata, guest=True)
        db.add(
            GuestPurchase(
                token='gift-retry-token',
                contact_type='email',
                contact_value='fixture@example.test',
                is_gift=True,
                period_days=30,
                amount_kopeks=AMOUNT,
                payment_method='cardlink',
            )
        )
        await db.commit()
        monkeypatch.setattr(
            common, 'try_fulfill_guest_purchase', AsyncMock(side_effect=RuntimeError('fulfillment unavailable'))
        )
        with pytest.raises(RuntimeError, match='fulfillment unavailable'):
            await deliver(service, db, callback)
        assert (await watcher.get(CardlinkPayment, payment_id)).is_paid
        assert (await watcher.get(User, user_id)).balance_kopeks == INITIAL
        assert (await watcher.scalar(select(GuestPurchase))).status == 'pending'
        await watcher.rollback()
        monkeypatch.setattr(common, 'try_fulfill_guest_purchase', real_fulfill)
        assert await deliver(service, db, callback)
        watcher.expunge_all()
        assert (await watcher.scalar(select(GuestPurchase))).status == 'paid'
        assert await watcher.scalar(select(func.sum(income_payments_query().c.amount_kopeks))) == AMOUNT


async def test_subpage_receipt_survives_fulfillment_failure_and_replay_resumes(postgres_database, service, monkeypatch):
    fail = AsyncMock(side_effect=RuntimeError('renewal unavailable'))
    monkeypatch.setattr('app.services.subpage_payment_service.fulfill_paid_invoice', fail)
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        metadata = {'purpose': 'subpage_renewal', 'invoice_token': 'subpage-retry-token', 'order_version': '1'}
        user_id, payment_id, callback = await seed(db, service, metadata=metadata)
        db.add(
            SubpageInvoice(
                token='subpage-retry-token',
                user_id=user_id,
                short_uuid='fixture123',
                period_days=30,
                amount_kopeks=AMOUNT,
                method='cardlink',
                provider_payment_id='bill-123',
            )
        )
        await db.commit()
        with pytest.raises(RuntimeError, match='renewal unavailable'):
            await deliver(service, db, callback)
        assert (await watcher.get(CardlinkPayment, payment_id)).is_paid
        invoice = await watcher.get(SubpageInvoice, 'subpage-retry-token')
        assert invoice.status == 'paid' and invoice.paid_amount_kopeks == AMOUNT
        await watcher.rollback()
        fail.side_effect = None
        assert await deliver(service, db, callback)
        assert fail.await_count == 2
        assert await watcher.scalar(select(func.sum(income_payments_query().c.amount_kopeks))) == AMOUNT


async def test_post_commit_notification_failure_cannot_duplicate_credit(postgres_database, service, monkeypatch):
    from app.database.crud import transaction as transaction_crud

    emit = AsyncMock(side_effect=RuntimeError('notification unavailable'))
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', emit)
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        user_id, payment_id, callback = await seed(db, service)
        with pytest.raises(RuntimeError, match='notification unavailable'):
            await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id)
        assert await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id)
        emit.assert_awaited_once()


async def test_distinct_receipts_for_same_user_lock_before_ledger_insert(postgres_database, service, monkeypatch):
    from app.database.crud import user as user_crud

    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user_id, first_id, callback = await seed(first, service)
        bill_one = dict(service.cardlink_service.get_bill.return_value)
        bill_two = {**bill_one, 'id': 'bill-456', 'order_id': 'order-456'}
        second_receipt = await crud.create_cardlink_payment(
            first, user_id=user_id, bill_id='bill-456', order_id='order-456', amount_kopeks=AMOUNT
        )
        second_id = second_receipt.id
        service.cardlink_service.get_bill.side_effect = lambda bill_id: dict(
            bill_one if bill_id == 'bill-123' else bill_two
        )
        second_callback = {**callback, 'InvId': 'order-456', 'TrsId': 'bill-456'}
        original = user_crud.lock_user_for_update
        holder_ready = asyncio.Event()
        entries = 0
        saw_waiter = False
        initial_locks = set()

        async def observe(db, user):
            nonlocal entries, saw_waiter
            if db in initial_locks:
                # Post-commit promo-group checks can lock the user again.
                return await original(db, user)
            initial_locks.add(db)
            entries += 1
            entry = entries
            # A ledger FK takes KEY SHARE on users; inserting first can cause
            # concurrent UPDATE-lock upgrades to deadlock. Assert the ledger
            # is still absent for this receipt at the user-lock boundary.
            current_bill = 'bill-123' if db is first else 'bill-456'
            assert (
                await db.scalar(
                    select(func.count()).select_from(Transaction).where(Transaction.external_id == current_bill)
                )
                == 0
            )
            if entry == 1:
                result = await original(db, user)
                holder_ready.set()
                saw_waiter = await lock_waiter_appeared(watcher)
                return result
            await holder_ready.wait()
            return await original(db, user)

        monkeypatch.setattr(user_crud, 'lock_user_for_update', observe)
        async with asyncio.timeout(15):
            assert await asyncio.gather(
                deliver(service, first, callback), deliver(service, second, second_callback)
            ) == [True, True]
        assert entries == 2 and saw_waiter
        watcher.expunge_all()
        assert (await watcher.get(User, user_id)).balance_kopeks == INITIAL + 2 * AMOUNT
        receipts = (
            await watcher.scalars(select(CardlinkPayment).where(CardlinkPayment.id.in_([first_id, second_id])))
        ).all()
        assert len(receipts) == 2 and all(payment.is_paid and payment.transaction_id for payment in receipts)
        assert await watcher.scalar(select(func.count()).select_from(Transaction)) == 2
        assert await watcher.scalar(select(func.sum(income_payments_query().c.amount_kopeks))) == 2 * AMOUNT


async def test_creation_uses_merchant_return_urls_not_generic_spa_urls(postgres_database, service, monkeypatch):
    monkeypatch.setattr(settings, 'CARDLINK_SUCCESS_REDIRECT_URL', 'https://merchant.example.test/cardlink/return')
    monkeypatch.setattr(settings, 'CARDLINK_FAIL_REDIRECT_URL', None)
    async with postgres_sessions(postgres_database, TABLES) as (db, _watcher):
        assert await service.create_cardlink_payment(
            db,
            None,
            AMOUNT,
            'Fixture',
            return_url='https://cabinet.example.test/balance',
            failed_url='https://subpage.example.test/failed',
        )
    called = service.cardlink_service.create_payment_link.await_args.kwargs
    assert called['success_url'] == 'https://merchant.example.test/cardlink/return'
    assert called['fail_url'] is None


async def test_unrecognized_subpage_metadata_does_not_acknowledge_payment(postgres_database, service, monkeypatch):
    monkeypatch.setattr(
        'app.services.subpage_payment_service.try_fulfill_subpage_renewal', AsyncMock(return_value=None)
    )
    async with postgres_sessions(postgres_database, TABLES) as (db, watcher):
        metadata = {'purpose': 'subpage_renewal', 'invoice_token': 'subpage-token'}
        user_id, payment_id, callback = await seed(db, service, metadata=metadata)
        with pytest.raises(ValueError, match='cardlink_subpage_metadata_invalid'):
            await deliver(service, db, callback)
        await assert_credit(watcher, user_id, payment_id, credited=False)
