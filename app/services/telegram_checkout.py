"""Telegram purchase checkout with cached provider URLs; Redis is not a payment ledger.

A claimed attempt is never retried automatically. A new explicit checkout after
Redis eviction/expiry may create a new invoice, just as a new bot payment request.
"""

import asyncio
import hashlib
import json
import secrets
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.services.payment_service import PaymentService
from app.services.rbac_bootstrap_service import is_protected_from_blocking
from app.services.support_settings_service import SupportSettingsService
from app.services.user_cart_service import user_cart_service
from app.utils.cache import RateLimitCache


TTL = 900
SUPPORTED_METHODS = frozenset({'yookassa', 'yookassa_sbp', 'wata', 'cardlink'})
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


class CheckoutRateLimited(CheckoutError):
    """The server-side checkout owner exceeded the request budget."""


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


async def checkout_keyboard(keyboard: InlineKeyboardMarkup, user_id: int, cart: dict, db) -> InlineKeyboardMarkup:
    """Prepare provider URLs before rendering; never expose a backend redirect URL.

    Each displayed supported method gets a pending invoice. Re-rendering the same
    cart reuses its cached URL; an ambiguous creation is never retried automatically.
    Keep creation sequential because callers share a single SQLAlchemy session.
    """
    result = keyboard.model_copy(deep=True)
    for row in result.inline_keyboard:
        for button in row:
            parts = (button.callback_data or '').split('|')
            if len(parts) != 3 or parts[0] != 'topup_amount' or parts[1] not in SUPPORTED_METHODS:
                continue
            try:
                token = await issue_token(user_id, cart, parts[1], int(parts[2]))
                button.url = await complete_checkout(token, db)
                button.callback_data = None
            except Exception:
                button.text = 'Повторить получение ссылки'
                button.callback_data = 'return_to_saved_cart'
    return result


async def _cabinet_methods(db, user):
    # Reuse the cabinet endpoint's read-only selector, including first-topup and
    # promo-group filters, configured names, ordering, limits and sub-options.
    from app.cabinet.routes.balance import get_payment_methods

    async with asyncio.timeout(3):
        return await get_payment_methods(user=user, db=db)


def _cabinet_choices(methods):
    """Match the cabinet picker; providers with their own chooser keep that flow."""
    for method in methods:
        if not method.is_available:
            continue
        options = method.options
        if method.id == 'yookassa' and options:
            # TopUpPanel puts SBP first within each provider.
            for option in sorted(options, key=lambda item: item['id'] != 'sbp'):
                if option['id'] == 'sbp':
                    if not settings.YOOKASSA_SBP_ENABLED:
                        continue
                    callback_method = 'yookassa_sbp'
                elif option['id'] == 'card':
                    callback_method = 'yookassa'
                else:
                    continue
                yield method, callback_method, option['name']
        else:
            callback_method = 'stars' if method.id == 'telegram_stars' else method.id
            yield method, callback_method, method.name


async def cabinet_checkout_keyboard(keyboard: InlineKeyboardMarkup, user, cart: dict, db) -> InlineKeyboardMarkup:
    """Use the cabinet's payment list for the new and restored purchase screen."""
    amount = max(0, cart['total_price'] - (user.balance_kopeks or 0))
    if not settings.is_auto_purchase_after_topup_enabled() or amount <= 0:
        return keyboard

    from app.utils.miniapp_buttons import strip_leading_emoji

    texts = get_texts(user.language)
    methods = await _cabinet_methods(db, user)
    rows = []
    icons = ('5192678313415434135', '5192803468762441581')
    for method, callback_method, name in _cabinet_choices(methods):
        if not method.min_amount_kopeks <= amount <= method.max_amount_kopeks:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=strip_leading_emoji(name),
                    callback_data=f'topup_amount|{callback_method}|{amount}',
                    style='primary',
                    icon_custom_emoji_id=icons[len(rows)] if len(rows) < len(icons) else None,
                )
            ]
        )
    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENTS_TEMPORARILY_UNAVAILABLE', '⚠️ Способы оплаты временно недоступны'),
                    callback_data='payment_methods_unavailable',
                )
            ]
        )
    # Preserve Back and separate recurring-subscription actions. Never reinsert
    # providers from the older ENV-only keyboard when the cabinet list is empty.
    rows.extend(
        [button.model_copy(deep=True) for button in row]
        for row in keyboard.inline_keyboard
        if row
        and all(
            not (button.callback_data or '').startswith('topup_')
            and button.callback_data not in ('balance_topup', 'payment_methods_unavailable')
            for button in row
        )
    )
    return await checkout_keyboard(InlineKeyboardMarkup(inline_keyboard=rows), user.id, cart, db)


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
    async with asyncio.timeout(3):
        limited = await RateLimitCache.is_rate_limited(
            intent['user_id'], 'telegram_checkout', limit=30, window=60, fail_closed=True
        )
    if limited:
        raise CheckoutRateLimited('Слишком много запросов. Повторно откройте ссылку позже.')
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
    # Recheck the same cabinet policy on click: a disabled method or option must
    # not remain usable through a previously rendered purchase link.
    methods = await _cabinet_methods(db, user)
    configured = next((item for item, code, _ in _cabinet_choices(methods) if code == method), None)
    if not configured or not configured.min_amount_kopeks <= amount <= configured.max_amount_kopeks:
        raise CheckoutError('Способ или сумма оплаты недоступны. Вернитесь в бот.')
    if method == 'cardlink':
        enabled = settings.is_cardlink_enabled()
    elif method == 'wata':
        enabled = settings.is_wata_enabled()
    else:
        enabled = settings.is_yookassa_enabled() and (method != 'yookassa_sbp' or settings.YOOKASSA_SBP_ENABLED)
    if not enabled:
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
            if method in {'wata', 'cardlink'}:
                create = service.create_cardlink_payment if method == 'cardlink' else service.create_wata_payment
                payment = await create(
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
