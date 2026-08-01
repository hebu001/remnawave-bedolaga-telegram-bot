"""Public payment endpoints for the Remnawave subscription page (sub page).

No authentication: the shortUuid from the subscription link identifies the
target subscription. The surface is strictly "money in": price options and
renewal payment only — no personal data, no balance spending, no mutations
beyond a webhook-confirmed renewal. Response field names are camelCase — the
sub-page widget consumes them as-is.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import YooKassaPayment
from app.services.subpage_payment_service import (
    create_invoice_record,
    format_period_label,
    get_invoice_record,
    get_renewal_periods,
    is_subscription_renewable,
    resolve_subscription_by_short_uuid,
)
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db
from ..ip_utils import get_client_ip


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/subpage', tags=['Subpage Payments'])


class SubpageMethod(BaseModel):
    id: str
    name: str


class SubpageRenewalOption(BaseModel):
    periodDays: int
    priceKopeks: int
    basePriceKopeks: int = 0
    devicesPriceKopeks: int = 0
    extraDevices: int = 0
    label: str | None = None


class SubpageRenewalOptionsResponse(BaseModel):
    enabled: bool
    currency: str = 'RUB'
    expiresAt: str | None = None
    tariffName: str | None = None
    trafficLimitGb: int | None = None
    deviceLimit: int | None = None
    cabinetUrl: str | None = None
    options: list[SubpageRenewalOption] = []
    methods: list[SubpageMethod] = []


class SubpageInvoiceRequest(BaseModel):
    periodDays: int = Field(gt=0, le=3650)
    method: str = Field(min_length=1, max_length=32)


class SubpageInvoiceResponse(BaseModel):
    invoiceToken: str
    paymentUrl: str
    amountKopeks: int


class SubpageInvoiceStatusResponse(BaseModel):
    status: str
    newExpiresAt: str | None = None


def _ensure_enabled() -> None:
    if not settings.is_subpage_payment_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')


async def _rate_limit(request: Request, action: str, limit: int, window: int) -> None:
    client_ip = get_client_ip(request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=limit, window=window, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
        )


def _available_methods() -> list[SubpageMethod]:
    methods: list[SubpageMethod] = []
    if settings.is_yookassa_enabled():
        methods.append(SubpageMethod(id='yookassa', name='Карта / СБП (ЮKassa)'))
    return methods


@router.get('/{short_uuid}/renewal-options', response_model=SubpageRenewalOptionsResponse)
async def get_subpage_renewal_options(
    short_uuid: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    _ensure_enabled()
    await _rate_limit(raw_request, 'subpage_options', limit=20, window=60)

    resolved = await resolve_subscription_by_short_uuid(db, short_uuid)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')

    user, subscription = resolved

    methods = _available_methods()
    if not methods or not is_subscription_renewable(subscription):
        return SubpageRenewalOptionsResponse(enabled=False)

    from app.services.pricing_engine import pricing_engine

    options: list[SubpageRenewalOption] = []
    for period in get_renewal_periods(subscription):
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period, user=user)
        if pricing.final_total <= 0:
            continue
        breakdown = pricing.breakdown or {}
        options.append(
            SubpageRenewalOption(
                periodDays=period,
                priceKopeks=pricing.final_total,
                basePriceKopeks=pricing.base_price,
                devicesPriceKopeks=pricing.devices_price,
                extraDevices=int(breakdown.get('extra_devices', 0) or 0),
                label=format_period_label(period),
            )
        )

    if not options:
        return SubpageRenewalOptionsResponse(enabled=False)

    tariff = subscription.tariff if subscription.tariff_id else None

    end_date = subscription.end_date
    return SubpageRenewalOptionsResponse(
        enabled=True,
        expiresAt=end_date.isoformat() if end_date else None,
        tariffName=tariff.name if tariff else None,
        trafficLimitGb=subscription.traffic_limit_gb,
        deviceLimit=subscription.device_limit,
        cabinetUrl=settings.CABINET_URL or None,
        options=options,
        methods=methods,
    )


@router.post('/{short_uuid}/invoice', response_model=SubpageInvoiceResponse)
async def create_subpage_invoice(
    short_uuid: str,
    body: SubpageInvoiceRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    _ensure_enabled()
    await _rate_limit(raw_request, 'subpage_invoice', limit=5, window=60)

    # Per-subscription cap protects a victim's link from invoice spam
    # regardless of the attacker's IP pool.
    if await RateLimitCache.is_ip_rate_limited(
        short_uuid, 'subpage_invoice_sub', limit=10, window=300, fail_closed=True
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    resolved = await resolve_subscription_by_short_uuid(db, short_uuid)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')

    user, subscription = resolved

    if not is_subscription_renewable(subscription):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Subscription is not renewable')

    if body.method != 'yookassa' or not settings.is_yookassa_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported payment method')

    if body.periodDays not in get_renewal_periods(subscription):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported period')

    from app.services.pricing_engine import pricing_engine

    pricing = await pricing_engine.calculate_renewal_price(db, subscription, body.periodDays, user=user)
    amount_kopeks = pricing.final_total
    if amount_kopeks <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported period')

    if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS or amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Amount out of provider limits')

    # Token is minted before the provider call so it can ride along in the
    # payment metadata; the Redis record is written only after the provider
    # accepts the payment.
    import secrets as _secrets

    from app.services.payment_service import PaymentService

    invoice_token = _secrets.token_urlsafe(24)

    subpage_base = (settings.SUBPAGE_URL or '').rstrip('/')
    return_url = f'{subpage_base}/{short_uuid}?invoice={invoice_token}'

    metadata = {
        'purpose': 'subpage_renewal',
        'type': 'subpage_renewal',
        'invoice_token': invoice_token,
        'subscription_id': str(subscription.id),
        'period_days': str(body.periodDays),
        'expected_amount_kopeks': str(amount_kopeks),
        'source': 'subpage',
    }

    payment_service = PaymentService()
    result = await payment_service.create_yookassa_payment(
        db=db,
        user_id=user.id,
        amount_kopeks=amount_kopeks,
        description=f'Продление подписки на {body.periodDays} дней',
        metadata=metadata,
        return_url=return_url,
    )

    if not result or not result.get('confirmation_url'):
        logger.error('Subpage: YooKassa payment creation failed', short_uuid=short_uuid)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='Payment provider error')

    await create_invoice_record(
        short_uuid=short_uuid,
        subscription_id=subscription.id,
        user_id=user.id,
        period_days=body.periodDays,
        amount_kopeks=amount_kopeks,
        local_payment_id=result.get('local_payment_id'),
        provider_payment_id=str(result.get('yookassa_payment_id')),
        token=invoice_token,
    )

    logger.info(
        'Subpage invoice created',
        subscription_id=subscription.id,
        period_days=body.periodDays,
        amount_kopeks=amount_kopeks,
    )

    return SubpageInvoiceResponse(
        invoiceToken=invoice_token,
        paymentUrl=result['confirmation_url'],
        amountKopeks=amount_kopeks,
    )


@router.get('/invoice/{invoice_token}', response_model=SubpageInvoiceStatusResponse)
async def get_subpage_invoice_status(
    invoice_token: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    _ensure_enabled()
    await _rate_limit(raw_request, 'subpage_status', limit=60, window=60)

    record = await get_invoice_record(invoice_token)
    if record is None:
        return SubpageInvoiceStatusResponse(status='expired')

    invoice_status = record.get('status', 'pending')

    if invoice_status == 'pending' and record.get('local_payment_id'):
        payment = await db.get(YooKassaPayment, record['local_payment_id'])
        if payment is not None and payment.status in ('canceled', 'cancelled'):
            invoice_status = 'failed'

    return SubpageInvoiceStatusResponse(
        status=invoice_status,
        newExpiresAt=record.get('new_expires_at'),
    )
