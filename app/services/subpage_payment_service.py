"""Payments initiated from the Remnawave subscription page (sub page).

Requests arrive only through the subscription-page BFF and carry a short-lived,
replay-protected HMAC. The flow can pay for the identified subscription but can
never read personal data or spend the owner's balance. Money flows in only.

Invoice quotes, payment capture and fulfillment live in PostgreSQL. Redis is
read only to recognize legacy invoices during rollout. Paid orders can be
recovered without redelivery from the provider.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    PaymentMethod,
    SubpageInvoice,
    Subscription,
    SubscriptionStatus,
    Transaction,
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


def quote_configuration(user: User, subscription: Subscription) -> dict[str, Any]:
    """Commercial configuration only; expiry/balance changes do not alter a quote."""
    tariff = subscription.tariff
    tariff_fields = (
        'traffic_limit_gb',
        'device_limit',
        'device_price_kopeks',
        'max_device_limit',
        'allowed_squads',
        'external_squad_uuid',
        'server_traffic_limits',
        'traffic_reset_mode',
        'period_prices',
        'is_daily',
        'daily_price_kopeks',
        'custom_days_enabled',
        'price_per_day_kopeks',
        'min_days',
        'max_days',
        'custom_traffic_enabled',
        'traffic_price_per_gb_kopeks',
        'min_traffic_gb',
        'max_traffic_gb',
    )
    expires = user.promo_offer_discount_expires_at
    return {
        'tariff_id': subscription.tariff_id,
        'device_limit': subscription.device_limit,
        'traffic_limit_gb': subscription.traffic_limit_gb,
        'purchased_traffic_gb': subscription.purchased_traffic_gb,
        'connected_squads': sorted(subscription.connected_squads or []),
        'tariff': {key: getattr(tariff, key) for key in tariff_fields} if tariff else None,
        'offer': {
            'percent': user.promo_offer_discount_percent,
            'source': user.promo_offer_discount_source,
            'expires': expires.isoformat() if expires else None,
        },
    }


async def create_invoice_record(
    db: AsyncSession,
    *,
    short_uuid: str,
    user: User,
    subscription: Subscription,
    pricing,
    method: str,
    token: str | None = None,
) -> str:
    """Persist the immutable quote BEFORE sending a request to the provider."""
    token = token or secrets.token_urlsafe(24)
    db.add(
        SubpageInvoice(
            token=token,
            short_uuid=short_uuid,
            user_id=user.id,
            subscription_id=subscription.id,
            period_days=pricing.period_days,
            amount_kopeks=pricing.final_total,
            method=method,
            configuration=quote_configuration(user, subscription),
            pricing=asdict(pricing),
        )
    )
    await db.commit()
    return token


async def attach_invoice_payment(
    db: AsyncSession,
    token: str,
    *,
    local_payment_id: int | None,
    provider_payment_id: str,
) -> None:
    invoice = await db.scalar(select(SubpageInvoice).where(SubpageInvoice.token == token).with_for_update())
    if invoice is None or not provider_payment_id:
        raise ValueError('subpage_invoice_not_found')
    if invoice.provider_payment_id and invoice.provider_payment_id != provider_payment_id:
        raise ValueError('subpage_provider_payment_conflict')
    invoice.provider_payment_id = provider_payment_id
    invoice.local_payment_id = local_payment_id
    # A webhook may have already completed the order. Never reset its status.
    await db.commit()


async def get_invoice_record(db: AsyncSession, token: str) -> dict[str, Any] | None:
    if not token or len(token) > 128:
        return None
    invoice = await db.get(SubpageInvoice, token)
    if invoice is not None:
        public_status = {'paid': 'pending', 'credited_only': 'failed', 'review': 'failed'}.get(
            invoice.status, invoice.status
        )
        return {
            'status': public_status,
            'short_uuid': invoice.short_uuid,
            'method': invoice.method,
            'local_payment_id': invoice.local_payment_id,
            'new_expires_at': invoice.new_expires_at.isoformat() if invoice.new_expires_at else None,
            'balance_credited': invoice.status == 'credited_only',
            'reason': invoice.reason,
        }
    # Compatibility with invoices issued before migration 0103. New records are
    # never written to Redis, and a missing cache entry cannot erase a DB order.
    data = await cache.get(_invoice_key(token))
    return data if isinstance(data, dict) else None


def _extract_subpage_invoice_token(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict) or metadata.get('purpose') != 'subpage_renewal':
        return None
    token = metadata.get('invoice_token')
    if not isinstance(token, str) or not token or len(token) > 128:
        raise ValueError('subpage_invoice_token_invalid')
    return token


async def _adopt_legacy_invoice(
    db: AsyncSession,
    token: str,
    metadata: dict[str, Any],
    *,
    payment_user_id: int | None,
    provider_name: str,
    provider_payment_id: str,
) -> SubpageInvoice:
    subscription_id = int(metadata.get('subscription_id', 0))
    period_days = int(metadata.get('period_days', 0))
    amount = int(metadata.get('expected_amount_kopeks', 0))
    if subscription_id <= 0 or period_days <= 0 or amount <= 0:
        raise ValueError('legacy_subpage_metadata_invalid')
    sub = await db.get(Subscription, subscription_id)
    cached = await cache.get(_invoice_key(token))
    cached = cached if isinstance(cached, dict) else {}
    if sub is not None and sub.user_id != payment_user_id:
        raise ValueError('legacy_subpage_owner_mismatch')
    if cached.get('user_id') is not None and cached['user_id'] != payment_user_id:
        raise ValueError('legacy_subpage_owner_mismatch')
    short_uuid = cached.get('short_uuid') or (sub.remnawave_short_uuid if sub else '') or ''
    legacy_status = 'pending'
    legacy_end = None
    if cached.get('status') == 'succeeded':
        deposit = await db.scalar(
            select(Transaction).where(
                Transaction.external_id == provider_payment_id,
                Transaction.payment_method == provider_name,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.user_id == payment_user_id,
                Transaction.amount_kopeks == amount,
                Transaction.is_completed.is_(True),
            )
        )
        if deposit is not None:
            legacy_status = 'succeeded'
            if cached.get('new_expires_at'):
                legacy_end = datetime.fromisoformat(cached['new_expires_at'])
    # We cannot reconstruct the price/configuration at the time of a legacy
    # quote. Its payment is credited, but never used to buy today's configuration.
    await db.execute(
        insert(SubpageInvoice)
        .values(
            token=token,
            user_id=payment_user_id,
            subscription_id=sub.id if sub else None,
            short_uuid=short_uuid,
            period_days=period_days,
            amount_kopeks=amount,
            method=provider_name,
            provider_payment_id=provider_payment_id,
            status=legacy_status,
            new_expires_at=legacy_end,
            reason=None if legacy_status == 'succeeded' else 'legacy_quote_missing',
        )
        .on_conflict_do_nothing(index_elements=[SubpageInvoice.token])
    )
    return await db.scalar(select(SubpageInvoice).where(SubpageInvoice.token == token).with_for_update())


async def try_fulfill_subpage_renewal(
    db: AsyncSession,
    *,
    metadata: dict[str, Any] | None,
    payment_amount_kopeks: int,
    provider_payment_id: str,
    provider_name: str,
    payment_user_id: int | None,
    currency: str = 'RUB',
) -> bool | None:
    """Capture a verified payment durably, then resume the order.

    Transient failures propagate to the provider handler. Capture is committed
    first so a restart can resume a paid order even without another webhook.
    """
    token = _extract_subpage_invoice_token(metadata)
    if token is None:
        return None
    if provider_name not in ('wata', 'yookassa') or not provider_payment_id or payment_amount_kopeks <= 0:
        raise ValueError('subpage_payment_invalid')
    try:
        invoice = await db.scalar(select(SubpageInvoice).where(SubpageInvoice.token == token).with_for_update())
        if invoice is None:
            if metadata.get('order_version') == '1':
                raise ValueError('durable_subpage_order_missing')
            invoice = await _adopt_legacy_invoice(
                db,
                token,
                metadata,
                payment_user_id=payment_user_id,
                provider_name=provider_name,
                provider_payment_id=provider_payment_id,
            )
        if invoice.user_id != payment_user_id or invoice.method != provider_name:
            raise ValueError('subpage_payment_owner_or_method_mismatch')
        if invoice.provider_payment_id and invoice.provider_payment_id != provider_payment_id:
            raise ValueError('subpage_provider_payment_conflict')
        if invoice.paid_amount_kopeks is not None and invoice.paid_amount_kopeks != payment_amount_kopeks:
            raise ValueError('subpage_payment_amount_changed')
        invoice.provider_payment_id = provider_payment_id
        invoice.paid_amount_kopeks = payment_amount_kopeks
        if currency.upper() != invoice.currency:
            invoice.status, invoice.reason = 'review', 'currency_mismatch'
        elif invoice.status == 'pending':
            invoice.status = 'paid'
        invoice.updated_at = datetime.now(UTC)
        await db.commit()
        await fulfill_paid_invoice(db, token)
        return True
    except (Exception, asyncio.CancelledError):
        await db.rollback()
        raise


async def fulfill_paid_invoice(db: AsyncSession, token: str) -> None:
    from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
    from app.database.crud.user import lock_user_for_pricing
    from app.services.pricing_engine import pricing_engine
    from app.services.subscription_renewal_service import SubscriptionRenewalService

    invoice = await db.get(SubpageInvoice, token)
    if invoice is None or invoice.status != 'paid':
        return
    user_id = invoice.user_id
    if user_id is None:
        invoice.status, invoice.reason = 'review', 'owner_deleted'
        await db.commit()
        return
    try:
        # Same lock order as purchases: user -> invoice -> subscription.
        user = await lock_user_for_pricing(db, user_id)
        invoice = await db.scalar(
            select(SubpageInvoice)
            .where(SubpageInvoice.token == token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invoice.status != 'paid':
            await db.rollback()
            return
        method = PaymentMethod(invoice.method)
        amount = invoice.paid_amount_kopeks
        external_id = invoice.provider_payment_id
        deposit = await db.scalar(
            select(Transaction).where(
                Transaction.external_id == external_id,
                Transaction.payment_method == method.value,
            )
        )
        new_deposit = deposit is None
        if deposit is not None:
            if (
                deposit.user_id != user_id
                or deposit.type != TransactionType.DEPOSIT.value
                or deposit.amount_kopeks != amount
                or not deposit.is_completed
            ):
                raise ValueError('subpage_existing_deposit_mismatch')
        else:
            user.balance_kopeks += amount
            deposit = await create_transaction(
                db,
                user_id=user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=amount,
                description=f'Пополнение через страницу подписки ({invoice.method})',
                payment_method=method,
                external_id=external_id,
                commit=False,
            )
        invoice.deposit_transaction_id = deposit.id
        sub = await db.scalar(
            select(Subscription)
            .options(*_subscription_load_options())
            .where(
                Subscription.id == invoice.subscription_id,
                Subscription.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # populate_existing on eager User must not overwrite the pending credit.
        # create_transaction(commit=False) flushed that credit before this query.
        reason = None
        pricing = None
        if not invoice.configuration or not invoice.pricing:
            reason = 'legacy_quote_missing' if new_deposit else 'legacy_payment_already_processed'
        elif sub is None or not is_subscription_renewable(sub):
            reason = 'subscription_unavailable'
        elif amount != invoice.amount_kopeks:
            reason = 'amount_mismatch'
        elif quote_configuration(user, sub) != invoice.configuration:
            reason = 'configuration_changed'
        else:
            pricing = await pricing_engine.calculate_renewal_price(db, sub, invoice.period_days, user=user)
            current = asdict(pricing)
            if any(current[key] != invoice.pricing.get(key) for key in current if key != 'breakdown'):
                reason = 'price_or_discount_changed'
            elif user.balance_kopeks < amount:
                reason = 'previous_credit_already_spent'

        result = None
        description = f'Продление подписки на {invoice.period_days} дней (страница подписки)'
        period_days = invoice.period_days
        if reason:
            invoice.status = (
                'review'
                if reason in ('legacy_payment_already_processed', 'previous_credit_already_spent')
                else 'credited_only'
            )
            invoice.reason = reason
        else:
            result = await SubscriptionRenewalService().finalize(
                db,
                user,
                sub,
                pricing,
                charge_balance_amount=amount,
                description=description,
                payment_method=method,
                commit=False,
            )
            invoice.status, invoice.reason = 'succeeded', None
            invoice.renewal_transaction_id = result.transaction.id
            invoice.new_expires_at = result.subscription.end_date
        invoice.updated_at = datetime.now(UTC)
        await db.commit()
    except (Exception, asyncio.CancelledError):
        await db.rollback()
        raise

    # The monetary result is final. A failure in optional events must never
    # repeat a renewal; the panel intent is already in the same committed DB.
    try:
        if new_deposit:
            await emit_transaction_side_effects(
                db,
                deposit,
                amount_kopeks=amount,
                user_id=user_id,
                type=TransactionType.DEPOSIT,
                payment_method=method,
                external_id=external_id,
            )
        if result:
            await SubscriptionRenewalService().after_commit(
                db,
                user,
                result,
                period_days=period_days,
                description=description,
                payment_method=method,
            )
    except Exception as error:
        await db.rollback()
        logger.warning('Subpage post-commit work failed', token_prefix=token[:6], error_type=type(error).__name__)


async def process_pending_subpage_orders(*, session_factory=None, limit: int = 20) -> None:
    from app.database.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    async with factory() as db:
        tokens = list(
            (
                await db.scalars(
                    select(SubpageInvoice.token)
                    .where(
                        SubpageInvoice.status == 'paid',
                        SubpageInvoice.next_attempt_at <= datetime.now(UTC),
                    )
                    .order_by(SubpageInvoice.next_attempt_at)
                    .limit(limit)
                )
            ).all()
        )
    for token in tokens:
        async with factory() as db:
            try:
                await fulfill_paid_invoice(db, token)
            except Exception as error:
                await db.rollback()
                invoice = await db.scalar(select(SubpageInvoice).where(SubpageInvoice.token == token).with_for_update())
                if invoice is not None and invoice.status == 'paid':
                    invoice.attempts += 1
                    invoice.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(3600, 30 * 2 ** min(invoice.attempts, 7))
                    )
                    await db.commit()
                logger.error('Subpage fulfillment deferred', token_prefix=token[:6], error_type=type(error).__name__)
