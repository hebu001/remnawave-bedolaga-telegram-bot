"""Lazy Telegram purchase checkout. Redis loss expires links; it is not a payment ledger.

A claimed attempt is never retried automatically. A new explicit checkout after
Redis eviction/expiry may create a new invoice, just as a new bot payment request.
"""

import asyncio
import hashlib
import json
import secrets
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardMarkup

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.services.maintenance_service import maintenance_service
from app.services.payment_service import PaymentService
from app.services.rbac_bootstrap_service import is_protected_from_blocking
from app.services.support_settings_service import SupportSettingsService
from app.services.user_cart_service import user_cart_service


TTL = 900
SUPPORTED_METHODS = frozenset({'yookassa', 'yookassa_sbp', 'wata'})
CART_FIELDS = (
    'cart_mode',
    'tariff_id',
    'subscription_id',
    'period_days',
    'device_limit',
    'traffic_limit_gb',
    'total_price',
    'discount_percent',
)
ISSUE = """
local existing = redis.call('GET', KEYS[1])
if existing and redis.call('EXISTS', ARGV[4] .. existing) == 1 then return existing end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return ARGV[1]
"""
CLAIM = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'expired' end
local data = cjson.decode(raw)
if data.status ~= 'ready' then return data.status end
data.status = 'creating'
redis.call('SET', KEYS[1], cjson.encode(data), 'KEEPTTL')
return 'claimed'
"""
COMPLETE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local data = cjson.decode(raw)
if data.status ~= 'creating' then return 0 end
data.status = 'complete'
data.url = ARGV[1]
redis.call('SET', KEYS[1], cjson.encode(data), 'KEEPTTL')
return 1
"""


class CheckoutError(Exception):
    """Safe text suitable for the checkout page; never includes credentials."""


def cart_fingerprint(cart: dict) -> str:
    return hashlib.sha256(json.dumps({k: cart.get(k) for k in CART_FIELDS}, sort_keys=True).encode()).hexdigest()


def checkout_origin() -> str:
    url = urlsplit(settings.WEBHOOK_URL or '')
    if url.scheme != 'https' or not url.netloc or url.username or url.password:
        raise CheckoutError('Оплата временно недоступна. Вернитесь в бот.')
    return f'https://{url.netloc}'


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


async def issue_token(user_id: int, cart: dict, method: str, amount: int) -> str:
    client = user_cart_service._get_redis_client()
    if client is None:
        raise CheckoutError('Оплата временно недоступна. Вернитесь в бот.')
    context = dict(
        user_id=user_id,
        cart=cart_fingerprint(cart),
        subscription_id=cart.get('subscription_id'),
        method=method,
        amount=amount,
    )
    digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    token = secrets.token_urlsafe(32)
    prefix = 'telegram_checkout:token:'
    async with asyncio.timeout(3):
        result = await client.eval(
            ISSUE,
            2,
            'telegram_checkout:context:' + digest,
            prefix + token,
            token,
            json.dumps(context | {'status': 'ready'}),
            TTL,
            prefix,
        )
    return _text(result)


async def checkout_keyboard(keyboard: InlineKeyboardMarkup, user_id: int, cart: dict) -> InlineKeyboardMarkup:
    """Create only credentials. Provider APIs are called only after a URL click."""
    result = keyboard.model_copy(deep=True)
    for row in result.inline_keyboard:
        for button in row:
            parts = (button.callback_data or '').split('|')
            if len(parts) != 3 or parts[0] != 'topup_amount' or parts[1] not in SUPPORTED_METHODS:
                continue
            try:
                origin = checkout_origin()
                token = await issue_token(user_id, cart, parts[1], int(parts[2]))
                button.url = f'{origin}/cabinet/telegram-checkout#{token}'
                button.callback_data = None
            except Exception:
                button.text = 'Повторить получение ссылки'
                button.callback_data = 'return_to_saved_cart'
    return result


async def complete_checkout(token: str, db) -> str:
    client = user_cart_service._get_redis_client()
    if client is None:
        raise CheckoutError('Ссылка недоступна. Вернитесь в бот.')
    key = 'telegram_checkout:token:' + token
    async with asyncio.timeout(3):
        raw = await client.get(key)
    if not raw:
        raise CheckoutError('Ссылка истекла. Вернитесь в бот и откройте оплату заново.')
    intent = json.loads(raw)
    user = await get_user_by_id(db, intent['user_id'])
    if (
        user is None
        or user.restriction_topup
        or (user.status != 'active' and not (user.status == 'blocked' and is_protected_from_blocking(user)))
    ):
        raise CheckoutError('Пополнение недоступно. Обратитесь в поддержку.')
    if maintenance_service.is_maintenance_active() and not (
        settings.is_admin(user.telegram_id) or SupportSettingsService.is_moderator(user.telegram_id)
    ):
        raise CheckoutError('Сейчас проводятся технические работы. Попробуйте позже.')
    cart = (
        await user_cart_service.get_subscription_cart(user.id, intent['subscription_id'])
        if intent['subscription_id']
        else await user_cart_service.get_user_cart(user.id)
    )
    if not cart or cart_fingerprint(cart) != intent['cart'] or cart.get('cart_mode') != 'tariff_purchase':
        raise CheckoutError('Покупка изменилась или истекла. Вернитесь в бот.')
    amount = intent['amount']
    if amount <= 0 or max(0, cart['total_price'] - (user.balance_kopeks or 0)) != amount:
        raise CheckoutError('Сумма изменилась. Вернитесь в бот и обновите покупку.')
    method = intent['method']
    if method not in SUPPORTED_METHODS:
        raise CheckoutError('Способ оплаты недоступен.')
    # Telegram payment availability follows the existing bot handlers, not the
    # cabinet-only PaymentMethodConfig filters and sub-options.
    if method == 'wata':
        enabled = settings.is_wata_enabled()
        minimum = settings.WATA_MIN_AMOUNT_KOPEKS
        maximum = settings.WATA_MAX_AMOUNT_KOPEKS
    else:
        enabled = settings.is_yookassa_enabled() and (method != 'yookassa_sbp' or settings.YOOKASSA_SBP_ENABLED)
        minimum = settings.YOOKASSA_MIN_AMOUNT_KOPEKS
        maximum = settings.YOOKASSA_MAX_AMOUNT_KOPEKS
    if not enabled or not minimum <= amount <= maximum:
        raise CheckoutError('Способ или сумма оплаты недоступны. Вернитесь в бот.')
    if intent['status'] == 'complete':
        return intent['url']
    async with asyncio.timeout(3):
        claimed = _text(await client.eval(CLAIM, 1, key))
    if claimed != 'claimed':
        # No automatic reissue after an ambiguous provider response or concurrent POST.
        raise CheckoutError(
            'Платёж уже обрабатывается. Повторно откройте эту ссылку позже. Если ошибка сохраняется, обратитесь в поддержку.'
        )
    service = PaymentService()
    description = settings.get_balance_payment_description(amount, telegram_user_id=user.telegram_id)
    try:
        async with asyncio.timeout(35):
            if method == 'wata':
                payment = await service.create_wata_payment(
                    db=db, user_id=user.id, amount_kopeks=amount, description=description, language=user.language
                )
                url = payment.get('payment_url') if payment else None
            else:
                create = (
                    service.create_yookassa_sbp_payment if method == 'yookassa_sbp' else service.create_yookassa_payment
                )
                payment = await create(
                    db=db,
                    user_id=user.id,
                    amount_kopeks=amount,
                    description=description,
                    receipt_email=None,
                    receipt_phone=None,
                    metadata={
                        'user_telegram_id': str(user.telegram_id),
                        'user_username': user.username or '',
                        'purpose': 'balance_topup_sbp' if method == 'yookassa_sbp' else 'balance_topup',
                    },
                )
                url = payment.get('confirmation_url') if payment else None
        parsed = urlsplit(url or '')
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise CheckoutError('Не удалось получить ссылку. Не создавайте повторный платёж; обратитесь в поддержку.')
        async with asyncio.timeout(3):
            stored = await client.eval(COMPLETE, 1, key, url)
        if not stored:
            raise CheckoutError('Не удалось сохранить ссылку. Обратитесь в поддержку.')
        return url
    except CheckoutError:
        raise
    except Exception:
        raise CheckoutError(
            'Результат запроса пока неизвестен. Не создавайте повторный платёж; обратитесь в поддержку.'
        ) from None
