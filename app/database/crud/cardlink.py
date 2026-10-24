"""Cardlink receipt persistence. Lock and link helpers never commit."""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CardlinkPayment


async def create_cardlink_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    bill_id: str,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    status: str = 'NEW',
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> CardlinkPayment:
    payment = CardlinkPayment(
        user_id=user_id,
        bill_id=bill_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        url=url,
        metadata_json=metadata or {},
        expires_at=expires_at,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def get_cardlink_payment_by_id(db: AsyncSession, payment_id: int) -> CardlinkPayment | None:
    return await db.scalar(select(CardlinkPayment).where(CardlinkPayment.id == payment_id))


get_cardlink_payment_by_local_id = get_cardlink_payment_by_id


async def get_cardlink_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> CardlinkPayment | None:
    return await db.scalar(
        select(CardlinkPayment)
        .where(CardlinkPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_cardlink_payment_by_bill_id(db: AsyncSession, bill_id: str) -> CardlinkPayment | None:
    return await db.scalar(select(CardlinkPayment).where(CardlinkPayment.bill_id == bill_id))


async def get_cardlink_payment_by_order_id(db: AsyncSession, order_id: str) -> CardlinkPayment | None:
    return await db.scalar(select(CardlinkPayment).where(CardlinkPayment.order_id == order_id))


async def link_cardlink_payment_to_transaction(
    db: AsyncSession, payment: CardlinkPayment, transaction_id: int
) -> CardlinkPayment:
    payment.transaction_id = transaction_id
    await db.flush()
    return payment
