"""Real persisted payment state must stay unpaid after unverified YooKassa money.

Only provider I/O and outbound notifications are mocked. Balance, transaction,
receipt and guest recovery queries execute against the disposable PostgreSQL.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.crud.yookassa import create_yookassa_payment
from app.database.models import Transaction, User, YooKassaPayment
from app.services.event_emitter import event_emitter
from app.services.guest_purchase_service import _find_succeeded_provider_payment
from app.services.payment_service import PaymentService
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = [YooKassaPayment.__table__, Transaction.__table__, User.__table__]
AMOUNT_KOPEKS = 15000
INITIAL_BALANCE = 700
PAYMENT_ID = 'yk-money-validation'
PURCHASE_TOKEN = 'money-validation-purchase'


def _remote(**overrides):
    return {
        'id': PAYMENT_ID,
        'status': 'succeeded',
        'paid': True,
        'amount_value': '150.00',
        'amount_currency': 'RUB',
        'test_mode': False,
        **overrides,
    }


def _event(**money):
    return {
        'event': 'payment.succeeded',
        'object': {
            'id': PAYMENT_ID,
            'status': 'succeeded',
            'paid': True,
            'amount': {'value': '150.00', 'currency': 'RUB', **money},
        },
    }


async def _create_order(db):
    user = User(telegram_id=3090501, first_name='Money validation', balance_kopeks=INITIAL_BALANCE)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    payment = await create_yookassa_payment(
        db,
        user_id=user.id,
        yookassa_payment_id=PAYMENT_ID,
        amount_kopeks=AMOUNT_KOPEKS,
        currency='RUB',
        description='Balance top-up',
        status='pending',
        metadata_json={'purchase_token': PURCHASE_TOKEN},
    )
    assert payment is not None
    return user.id, payment.id


@pytest.mark.parametrize('entrypoint', ['webhook', 'poll'])
@pytest.mark.parametrize(
    'remote',
    [
        _remote(amount_value='149.99'),
        _remote(amount_value='150.01'),
        _remote(amount_currency='USD'),
        _remote(amount_currency=None),
        _remote(amount_value='150.000000000000001'),
        _remote(id='another-provider-payment'),
        None,
    ],
    ids=['underpaid', 'overpaid', 'foreign-currency', 'missing-currency', 'fractional-kopeks', 'wrong-id', 'api-down'],
)
async def test_rejected_provider_money_cannot_become_recoverable_paid_order(
    postgres_database, monkeypatch, entrypoint, remote
):
    monkeypatch.setattr(settings, 'YOOKASSA_TEST_MODE', False)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    service.yookassa_service = SimpleNamespace(get_payment_info=AsyncMock(return_value=remote))

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, observer):
        user_id, payment_id = await _create_order(db)
        if entrypoint == 'webhook':
            result = await service.process_yookassa_webhook(db, _event())
            assert result is False
        else:
            assert await service.get_yookassa_payment_status(db, payment_id) is None
        # FastAPI's dependency may commit even when the callback returns False.
        await db.commit()

        payment = await observer.get(YooKassaPayment, payment_id)
        user = await observer.get(User, user_id)
        assert payment.status == 'pending'
        assert payment.is_paid is False
        assert payment.is_captured is False
        assert payment.transaction_id is None
        assert payment.amount_kopeks == AMOUNT_KOPEKS and payment.currency == 'RUB'
        assert user.balance_kopeks == INITIAL_BALANCE
        assert await observer.scalar(select(func.count()).select_from(Transaction)) == 0
        assert await _find_succeeded_provider_payment(observer, 'yookassa', PURCHASE_TOKEN) is None


async def test_rejected_callback_then_matching_replays_credit_once(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'YOOKASSA_TEST_MODE', False)
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', False)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    service.yookassa_service = SimpleNamespace(get_payment_info=AsyncMock(return_value=_remote()))

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, observer):
        user_id, payment_id = await _create_order(db)
        assert await service.process_yookassa_webhook(db, _event(value='1.00')) is False
        await db.commit()
        assert await _find_succeeded_provider_payment(observer, 'yookassa', PURCHASE_TOKEN) is None
        await observer.rollback()

        for _ in range(3):
            assert await service.process_yookassa_webhook(db, _event()) is True
            status = await service.get_yookassa_payment_status(db, payment_id)
            assert status['is_paid'] is True
        await db.commit()

        user = await observer.get(User, user_id)
        payment = await observer.get(YooKassaPayment, payment_id)
        transactions = (await observer.scalars(select(Transaction))).all()
        assert user.balance_kopeks == INITIAL_BALANCE + AMOUNT_KOPEKS
        assert payment.status == 'succeeded' and payment.is_paid
        assert len(transactions) == 1
        assert transactions[0].id == payment.transaction_id
        assert transactions[0].amount_kopeks == AMOUNT_KOPEKS
        assert transactions[0].external_id == PAYMENT_ID


async def test_restore_race_does_not_mark_conflicting_existing_order_paid(postgres_database, monkeypatch):
    """An idempotent create may return a row inserted after the initial lookup."""
    from app.services import payment_service as payment_module

    monkeypatch.setattr(settings, 'YOOKASSA_TEST_MODE', False)
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    original_lookup = payment_module.get_yookassa_payment_by_id
    lookups = 0

    async def miss_initial_lookup(db, provider_id):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return await original_lookup(db, provider_id)

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, observer):
        user_id, payment_id = await _create_order(db)
        service.yookassa_service = SimpleNamespace(
            get_payment_info=AsyncMock(return_value=_remote(amount_value='1.00', metadata={'user_id': str(user_id)}))
        )
        monkeypatch.setattr(payment_module, 'get_yookassa_payment_by_id', miss_initial_lookup)
        assert await service.process_yookassa_webhook(db, _event(value='1.00')) is False
        await db.commit()

        payment = await observer.get(YooKassaPayment, payment_id)
        assert payment.status == 'pending'
        assert payment.is_paid is False
        assert payment.transaction_id is None
        assert payment.amount_kopeks == AMOUNT_KOPEKS
        assert await observer.scalar(select(User.balance_kopeks).where(User.id == user_id)) == INITIAL_BALANCE
        assert await observer.scalar(select(func.count()).select_from(Transaction)) == 0
        assert await _find_succeeded_provider_payment(observer, 'yookassa', PURCHASE_TOKEN) is None
