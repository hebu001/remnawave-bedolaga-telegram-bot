"""Payment endpoints for the Remnawave subscription-page backend.

Every request is authenticated with a short-lived, replay-protected HMAC from
the BFF. The shortUuid identifies the target subscription but is never accepted
as the sole credential. Response field names are camelCase for the widget.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.subpage_bff import verify_subpage_bff_request
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


logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix='/subpage',
    tags=['Subpage Payments'],
    dependencies=[Depends(verify_subpage_bff_request)],
)


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
    client_ip = getattr(request.state, 'subpage_client_ip', '')
    if not client_ip:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=limit, window=window, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
        )


# Providers wired into the subpage flow (invoice creation + webhook hook)
SUBPAGE_SUPPORTED_METHODS = ('yookassa', 'wata')


async def _available_methods(db: AsyncSession, user) -> list[SubpageMethod]:
    """Enabled methods in the bot's admin ranking order (PaymentMethodConfig),
    filtered to providers the subpage flow supports. The widget offers the
    first-ranked one."""
    try:
        from app.services.payment_method_config_service import get_enabled_methods_for_user

        ranked = await get_enabled_methods_for_user(db, user=user, is_first_topup=False)
        return [
            SubpageMethod(id=m['id'], name=str(m.get('name') or m['id']))
            for m in ranked
            if m.get('id') in SUBPAGE_SUPPORTED_METHODS
        ]
    except Exception as error:
        logger.error('Subpage: method ranking failed', error=str(error))
        return []


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

    methods = await _available_methods(db, user)
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

    available = await _available_methods(db, user)
    if body.method not in {m.id for m in available}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported payment method')

    if body.periodDays not in get_renewal_periods(subscription):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported period')

    from app.services.pricing_engine import pricing_engine

    pricing = await pricing_engine.calculate_renewal_price(db, subscription, body.periodDays, user=user)
    amount_kopeks = pricing.final_total
    if amount_kopeks <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported period')

    if body.method == 'yookassa':
        min_amount, max_amount = settings.YOOKASSA_MIN_AMOUNT_KOPEKS, settings.YOOKASSA_MAX_AMOUNT_KOPEKS
    else:
        min_amount, max_amount = settings.WATA_MIN_AMOUNT_KOPEKS, settings.WATA_MAX_AMOUNT_KOPEKS
    if amount_kopeks < min_amount or amount_kopeks > max_amount:
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
    description = f'Продление подписки на {body.periodDays} дней'

    if body.method == 'yookassa':
        result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            metadata=metadata,
            return_url=return_url,
        )
        payment_url = result.get('confirmation_url') if result else None
        provider_payment_id = str(result.get('yookassa_payment_id')) if result else ''
    else:
        result = await payment_service.create_wata_payment(
            db=db,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            language=getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE,
            return_url=return_url,
            failed_url=return_url,
        )
        payment_url = result.get('payment_url') if result else None
        provider_payment_id = str(result.get('payment_link_id')) if result else ''
        # WATA creation has no metadata param — patch the local record afterwards
        # (same pattern as the guest-purchase flow).
        if result:
            from app.database.crud.wata import get_wata_payment_by_id

            wata_record = await get_wata_payment_by_id(db, result['local_payment_id'])
            if wata_record is not None:
                merged = dict(getattr(wata_record, 'metadata_json', None) or {})
                merged.update(metadata)
                wata_record.metadata_json = merged
                await db.commit()

    if not result or not payment_url:
        logger.error('Subpage: payment creation failed', short_uuid=short_uuid, method=body.method)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='Payment provider error')

    await create_invoice_record(
        short_uuid=short_uuid,
        subscription_id=subscription.id,
        user_id=user.id,
        period_days=body.periodDays,
        amount_kopeks=amount_kopeks,
        local_payment_id=result.get('local_payment_id'),
        provider_payment_id=provider_payment_id,
        method=body.method,
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
        paymentUrl=payment_url,
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

    signed_short_uuid = getattr(raw_request.state, 'subpage_short_uuid', '')
    if not signed_short_uuid or record.get('short_uuid') != signed_short_uuid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')

    invoice_status = record.get('status', 'pending')

    if (
        invoice_status == 'pending'
        and record.get('local_payment_id')
        and record.get('method', 'yookassa') == 'yookassa'
    ):
        payment = await db.get(YooKassaPayment, record['local_payment_id'])
        if payment is not None and payment.status in ('canceled', 'cancelled'):
            invoice_status = 'failed'

    return SubpageInvoiceStatusResponse(
        status=invoice_status,
        newExpiresAt=record.get('new_expires_at'),
    )
