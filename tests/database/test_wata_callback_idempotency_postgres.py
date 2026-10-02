"""Real WATA Paid callbacks preserve one credit under replay and concurrency.

Signature verification happens before these callbacks. These tests cover the
unchanged financial boundary reached after an accepted signature: actual
PostgreSQL payment/user locks, deposit creation, receipt and income reporting.
Only external event notifications are replaced; provider calls are prohibited
by the test runner's network guard. This is an ordinary balance top-up, not a
guest purchase or subscription-page renewal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.database.crud.wata as wata_crud
from app.database.crud.transaction import get_income_total, income_payments_query
from app.database.models import PaymentMethod, Transaction, TransactionType, User, WataPayment
from app.services.event_emitter import event_emitter
from app.services.payment_service import PaymentService
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres

AMOUNT_KOPEKS = 125000
INITIAL_BALANCE_KOPEKS = 700
TABLES = [WataPayment.__table__, Transaction.__table__, User.__table__]


async def _create_payment(db: Any, order_id: str) -> tuple[int, int, dict[str, Any]]:
    user = User(
        telegram_id=3090001,
        first_name='WATA replay test',
        language='ru',
        balance_kopeks=INITIAL_BALANCE_KOPEKS,
        has_made_first_topup=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    payment = await wata_crud.create_wata_payment(
        db,
        user_id=user.id,
        payment_link_id=f'{order_id}-link',
        order_id=order_id,
        amount_kopeks=AMOUNT_KOPEKS,
        currency='RUB',
        description='Пополнение баланса',
        status='Opened',
        type_='OneTime',
        url='https://pay.example.test/wata',
    )
    payload = {
        'id': f'{order_id}-transaction',
        'orderId': order_id,
        'paymentLinkId': payment.payment_link_id,
        'transactionStatus': 'Paid',
        'amount': '1250.00',
        'currency': 'RUB',
        'paymentTime': datetime.now(UTC).isoformat(),
    }
    return user.id, payment.id, payload


async def _deliver(service: PaymentService, db: Any, payload: dict[str, Any]) -> bool:
    try:
        return await service.process_wata_webhook(db, dict(payload))
    finally:
        # Mirrors closing the separate session after each webhook delivery.
        await db.rollback()


async def _assert_one_credit_and_receipt(db: Any, user_id: int, payment_id: int, payload: dict[str, Any]) -> None:
    db.expunge_all()
    user = await db.get(User, user_id)
    assert user is not None
    assert user.balance_kopeks == INITIAL_BALANCE_KOPEKS + AMOUNT_KOPEKS
    assert user.has_made_first_topup is True

    deposits = (
        (
            await db.execute(
                select(Transaction).where(
                    Transaction.user_id == user_id,
                    Transaction.payment_method == PaymentMethod.WATA.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deposits) == 1
    deposit = deposits[0]
    assert deposit.type == TransactionType.DEPOSIT.value
    assert deposit.amount_kopeks == AMOUNT_KOPEKS
    assert deposit.is_completed is True
    assert deposit.external_id == payload['id']

    paid_receipts = (
        (await db.execute(select(WataPayment).where(WataPayment.user_id == user_id, WataPayment.is_paid.is_(True))))
        .scalars()
        .all()
    )
    assert len(paid_receipts) == 1
    receipt = paid_receipts[0]
    assert receipt.id == payment_id
    assert receipt.transaction_id == deposit.id
    assert receipt.status == 'Paid'
    assert receipt.amount_kopeks == AMOUNT_KOPEKS
    assert receipt.callback_payload == payload
    assert receipt.paid_at is not None

    # Fork income contracts count the confirmed WATA receipt once; adding its
    # linked deposit must not turn one provider payment into two receipts.
    income = income_payments_query()
    assert await db.scalar(select(func.count()).select_from(income)) == 1
    now = datetime.now(UTC)
    assert await get_income_total(db, now - timedelta(days=1), now + timedelta(days=1)) == AMOUNT_KOPEKS


async def test_repeated_paid_wata_callback_credits_and_counts_income_once(postgres_database, monkeypatch) -> None:
    notification = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', notification)
    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, watcher):
        user_id, payment_id, payload = await _create_payment(db, 'wata-replay')
        for _ in range(4):
            assert await _deliver(service, db, payload) is True

        await _assert_one_credit_and_receipt(watcher, user_id, payment_id, payload)
        notification.assert_awaited_once()


async def test_competing_paid_wata_callbacks_credit_and_count_income_once(postgres_database, monkeypatch) -> None:
    notification = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', notification)
    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user_id, payment_id, payload = await _create_payment(first, 'wata-concurrent')
        original_lock = wata_crud.get_wata_payment_by_id_for_update
        holder_has_the_row = asyncio.Event()
        entries = 0
        race_observed = False

        async def observed_lock(db: Any, target_id: int) -> WataPayment | None:
            nonlocal entries, race_observed
            entries += 1
            if entries == 1:
                row = await original_lock(db, target_id)
                holder_has_the_row.set()
                race_observed = await lock_waiter_appeared(watcher)
                return row
            await holder_has_the_row.wait()
            return await original_lock(db, target_id)

        # Observe the actual competing SELECT FOR UPDATE statements. The wrapper
        # neither returns a fake payment nor changes financial/lock semantics.
        monkeypatch.setattr(wata_crud, 'get_wata_payment_by_id_for_update', observed_lock)
        async with asyncio.timeout(20):
            outcomes = await asyncio.gather(_deliver(service, first, payload), _deliver(service, second, payload))

        await _assert_one_credit_and_receipt(watcher, user_id, payment_id, payload)
        assert outcomes == [True, True]
        assert entries == 2
        assert race_observed, 'The second actual callback never queued behind the payment row lock'
        notification.assert_awaited_once()
