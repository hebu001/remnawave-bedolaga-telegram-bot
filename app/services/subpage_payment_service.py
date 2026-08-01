"""Payments initiated from the Remnawave subscription page (sub page).

Unauthenticated flow: anyone holding a subscription link (shortUuid) may PAY FOR
that subscription's renewal, but can never read personal data, spend the
owner's balance or mutate anything else. Money flows in only.

Invoice records live in Redis under ``subpage_invoice:{token}``; fulfillment is
driven by the payment-provider webhook via :func:`try_fulfill_subpage_renewal`,
mirroring the guest-purchase flow in ``app/services/payment/common.py``.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    TransactionType,
    User,
    UserPromoGroup,
)
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)


def _subscription_load_options():
    # pricing_engine resolves the user's promo group lazily; without these
    # eager loads any relationship access raises MissingGreenlet in async.
    return (
        selectinload(Subscription.user).selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
        selectinload(Subscription.user).selectinload(User.promo_group),
        selectinload(Subscription.tariff),
    )


SUBPAGE_INVOICE_PREFIX = 'subpage_invoice'
SUBPAGE_INVOICE_TTL = 3600  # pending invoice lifetime
SUBPAGE_INVOICE_DONE_TTL = 86400  # keep terminal statuses around for the result page

_SHORT_UUID_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


def _invoice_key(token: str) -> str:
    return cache_key(SUBPAGE_INVOICE_PREFIX, token)


async def resolve_subscription_by_short_uuid(
    db: AsyncSession,
    short_uuid: str,
) -> tuple[User, Subscription] | None:
    """Resolve a panel shortUuid to the local (user, subscription) pair.

    Local DB first (Subscription.remnawave_short_uuid), then the panel API
    (user-by-short-uuid -> panel uuid -> local user). Returns None when the
    shortUuid is unknown — callers must answer with a uniform 404.
    """
    if not _SHORT_UUID_RE.match(short_uuid):
        return None

    result = await db.execute(
        select(Subscription)
        .options(*_subscription_load_options())
        .where(Subscription.remnawave_short_uuid == short_uuid)
        .limit(1)
    )
    subscription = result.scalars().first()
    if subscription is not None and subscription.user is not None:
        return subscription.user, subscription

    # Fallback: ask the panel. Needed for subscriptions created before
    # remnawave_short_uuid was populated locally.
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    if not service.is_configured:
        return None

    try:
        async with service.get_api_client() as api:
            panel_user = await api.get_user_by_short_uuid(short_uuid)
    except Exception as error:
        logger.warning('Subpage: panel lookup by shortUuid failed', error=str(error))
        return None

    if panel_user is None or not panel_user.uuid:
        return None

    from app.database.crud.user import get_user_by_remnawave_uuid

    user = await get_user_by_remnawave_uuid(db, panel_user.uuid)
    if user is None:
        return None

    subscription = None
    if settings.is_multi_tariff_enabled():
        result = await db.execute(
            select(Subscription)
            .options(*_subscription_load_options())
            .where(Subscription.remnawave_uuid == panel_user.uuid)
            .limit(1)
        )
        subscription = result.scalars().first()

    if subscription is None:
        from app.database.crud.subscription import get_subscription_by_user_id

        subscription = await get_subscription_by_user_id(db, user.id)

    if subscription is None:
        return None

    return user, subscription


def is_subscription_renewable(subscription: Subscription) -> bool:
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        return False

    non_renewable = {SubscriptionStatus.DISABLED.value, SubscriptionStatus.PENDING.value}
    actual_status = getattr(subscription, 'actual_status', subscription.status)
    return actual_status not in non_renewable


def get_renewal_periods(subscription: Subscription) -> list[int]:
    if (
        subscription.tariff_id
        and subscription.tariff
        and subscription.tariff.is_active
        and subscription.tariff.period_prices
    ):
        return sorted(int(k) for k in subscription.tariff.period_prices.keys())
    return settings.get_available_renewal_periods()


def format_period_label(period_days: int) -> str:
    months, remainder = divmod(period_days, 30)
    if remainder == 0 and months > 0:
        if months == 1:
            return '1 месяц'
        if months in (2, 3, 4):
            return f'{months} месяца'
        return f'{months} месяцев'
    return f'{period_days} дней'


async def create_invoice_record(
    *,
    token: str | None = None,
    short_uuid: str,
    subscription_id: int,
    user_id: int,
    period_days: int,
    amount_kopeks: int,
    local_payment_id: int | None,
    provider_payment_id: str,
    method: str = 'yookassa',
) -> str:
    token = token or secrets.token_urlsafe(24)
    record = {
        'status': 'pending',
        'method': method,
        'short_uuid': short_uuid,
        'subscription_id': subscription_id,
        'user_id': user_id,
        'period_days': period_days,
        'amount_kopeks': amount_kopeks,
        'local_payment_id': local_payment_id,
        'provider_payment_id': provider_payment_id,
        'created_at': datetime.now(UTC).isoformat(),
        'new_expires_at': None,
    }
    stored = await cache.set(_invoice_key(token), record, expire=SUBPAGE_INVOICE_TTL)
    if not stored:
        raise RuntimeError('Failed to store subpage invoice in Redis')
    return token


async def get_invoice_record(token: str) -> dict[str, Any] | None:
    if not token or len(token) > 128:
        return None
    data = await cache.get(_invoice_key(token))
    return data if isinstance(data, dict) else None


async def mark_invoice(token: str, status: str, new_expires_at: str | None = None) -> None:
    record = await get_invoice_record(token)
    if record is None:
        record = {'status': status}
    record['status'] = status
    record['new_expires_at'] = new_expires_at
    await cache.set(_invoice_key(token), record, expire=SUBPAGE_INVOICE_DONE_TTL)


def _extract_subpage_invoice_token(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    if metadata.get('purpose') != 'subpage_renewal':
        return None
    return metadata.get('invoice_token') or None


async def try_fulfill_subpage_renewal(
    db: AsyncSession,
    *,
    metadata: dict[str, Any] | None,
    payment_amount_kopeks: int,
    provider_payment_id: str,
    provider_name: str,
) -> bool | None:
    """Fulfill a subscription renewal paid from the sub page.

    Returns ``True`` when the payment was consumed (fulfilled or terminally
    failed), ``None`` when the payment is not a subpage renewal (caller
    proceeds with its normal flow).

    Money-safety invariant: the paid amount is first credited to the owner's
    balance as a DEPOSIT transaction carrying ``external_id`` (idempotency
    anchor — unique per provider payment), then the renewal charges it back.
    Any failure after the credit leaves the money on the balance.
    """
    token = _extract_subpage_invoice_token(metadata)
    if token is None:
        return None

    invoice = await get_invoice_record(token)
    if invoice is not None and invoice.get('status') == 'succeeded':
        logger.info('Subpage renewal already fulfilled, skipping', token_prefix=token[:6])
        return True

    try:
        subscription_id = int(metadata.get('subscription_id', 0))
        period_days = int(metadata.get('period_days', 0))
        expected_amount = int(metadata.get('expected_amount_kopeks', 0))
    except (TypeError, ValueError):
        subscription_id = period_days = expected_amount = 0

    if subscription_id <= 0 or period_days <= 0 or expected_amount <= 0:
        logger.error(
            'Subpage renewal: malformed metadata',
            provider=provider_name,
            provider_payment_id=provider_payment_id,
        )
        await mark_invoice(token, 'failed')
        return True

    if payment_amount_kopeks != expected_amount:
        logger.error(
            'Subpage renewal: webhook amount does not match invoice amount',
            webhook_kopeks=payment_amount_kopeks,
            expected_kopeks=expected_amount,
            provider=provider_name,
            provider_payment_id=provider_payment_id,
        )
        await mark_invoice(token, 'failed')
        return True

    result = await db.execute(
        select(Subscription).options(*_subscription_load_options()).where(Subscription.id == subscription_id).limit(1)
    )
    subscription = result.scalars().first()
    if subscription is None or subscription.user is None:
        logger.error(
            'Subpage renewal: subscription not found',
            subscription_id=subscription_id,
            provider_payment_id=provider_payment_id,
        )
        await mark_invoice(token, 'failed')
        return True

    user = subscription.user

    from app.database.crud.transaction import create_transaction, get_transaction_by_external_id
    from app.database.crud.user import add_user_balance

    payment_method = {
        'yookassa': PaymentMethod.YOOKASSA,
        'wata': PaymentMethod.WATA,
    }.get(provider_name)

    if payment_method is not None:
        existing = await get_transaction_by_external_id(db, provider_payment_id, payment_method)
        if existing is not None:
            logger.info(
                'Subpage renewal: provider payment already has a transaction, skipping',
                provider_payment_id=provider_payment_id,
            )
            return True

    credited = await add_user_balance(
        db,
        user,
        payment_amount_kopeks,
        f'Пополнение через страницу подписки ({provider_name})',
        create_transaction=False,
        commit=False,
    )
    if not credited:
        logger.critical(
            'Subpage renewal: failed to credit balance',
            user_id=user.id,
            provider_payment_id=provider_payment_id,
        )
        await mark_invoice(token, 'failed')
        return True

    try:
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment_amount_kopeks,
            description=f'Пополнение через страницу подписки ({provider_name})',
            payment_method=payment_method,
            external_id=provider_payment_id,
            commit=True,
        )
    except Exception as error:
        # Unique (external_id, method) violation => concurrent webhook already
        # credited this payment. Roll back our balance mutation and bail out.
        await db.rollback()
        logger.warning(
            'Subpage renewal: deposit transaction already exists (replay), skipping',
            provider_payment_id=provider_payment_id,
            error=str(error),
        )
        return True

    from app.services.pricing_engine import pricing_engine
    from app.services.subscription_renewal_service import (
        SubscriptionRenewalChargeError,
        SubscriptionRenewalService,
    )

    try:
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period_days, user=user)
        renewal_service = SubscriptionRenewalService()
        renewal_result = await renewal_service.finalize(
            db,
            user,
            subscription,
            pricing,
            charge_balance_amount=payment_amount_kopeks,
            description=f'Продление подписки на {period_days} дней (страница подписки)',
            payment_method=payment_method,
        )
    except SubscriptionRenewalChargeError:
        logger.critical(
            'Subpage renewal: balance charge failed after credit — money left on balance',
            user_id=user.id,
            provider_payment_id=provider_payment_id,
        )
        await mark_invoice(token, 'failed')
        return True
    except Exception as error:
        logger.critical(
            'Subpage renewal: extension failed — money left on balance',
            user_id=user.id,
            subscription_id=subscription_id,
            provider_payment_id=provider_payment_id,
            error=str(error),
        )
        await mark_invoice(token, 'failed')
        return True

    new_end = renewal_result.subscription.end_date
    await mark_invoice(token, 'succeeded', new_end.isoformat() if new_end else None)

    logger.info(
        'Subpage renewal fulfilled',
        user_id=user.id,
        subscription_id=subscription_id,
        period_days=period_days,
        amount_kopeks=payment_amount_kopeks,
        provider_payment_id=provider_payment_id,
    )
    return True
