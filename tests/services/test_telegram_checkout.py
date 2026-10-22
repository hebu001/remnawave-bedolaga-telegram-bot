import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings, settings
from app.services import telegram_checkout as m


_REAL_OWNER_LIMITER = m.RateLimitCache.is_rate_limited


class Redis:
    """Atomic in-memory model; production Lua also needs an actual Redis smoke."""

    def __init__(self):
        self.data = {}
        self.fail_complete = False

    async def get(self, key):
        return self.data.get(key)

    async def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        if script == m.ISSUE:
            old = self.data.get(keys[0])
            if old and argv[3] + old in self.data:
                return old
            self.data[keys[1]], self.data[keys[0]] = argv[1], argv[0]
            return argv[0]
        raw = self.data.get(keys[0])
        if script == m.CLAIM:
            if not raw:
                return 'expired'
            data = json.loads(raw)
            if data['status'] != 'ready':
                return data['status']
            data['status'] = 'creating'
            self.data[keys[0]] = json.dumps(data)
            return 'claimed'
        assert script == m.COMPLETE
        if self.fail_complete:
            raise ConnectionError('offline')
        if not raw:
            return 0
        data = json.loads(raw)
        if data['status'] != 'creating':
            return 0
        data.update(status='complete', url=argv[0])
        self.data[keys[0]] = json.dumps(data)
        return 1


@pytest.fixture
def setup(monkeypatch):
    from app.cabinet.routes import balance

    redis = Redis()
    cart = dict(
        cart_mode='tariff_purchase',
        tariff_id=7,
        subscription_id=11,
        period_days=30,
        device_limit=5,
        traffic_limit_gb=300,
        total_price=15000,
    )
    user = SimpleNamespace(
        id=1,
        telegram_id=123,
        username='sample',
        language='ru',
        balance_kopeks=10000,
        restriction_topup=False,
        status='active',
        email=None,
        email_verified=False,
    )
    create = AsyncMock(
        return_value={'confirmation_url': 'https://pay.example/test', 'payment_url': 'https://pay.example/test'}
    )
    service = SimpleNamespace(
        create_yookassa_payment=create, create_yookassa_sbp_payment=create, create_wata_payment=create
    )
    providers = [
        SimpleNamespace(
            id=method,
            name=method,
            min_amount_kopeks=100,
            max_amount_kopeks=100000,
            is_available=True,
            options=[{'id': 'card', 'name': '💳 Карта'}, {'id': 'sbp', 'name': '🏦 СБП'}]
            if method == 'yookassa'
            else None,
        )
        for method in ('yookassa', 'wata')
    ]
    monkeypatch.setattr(m.user_cart_service, '_get_redis_client', lambda: redis)
    monkeypatch.setattr(m.user_cart_service, 'get_subscription_cart', AsyncMock(return_value=cart))
    monkeypatch.setattr(m.user_cart_service, 'get_user_cart', AsyncMock(return_value=cart))
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(m, 'PaymentService', lambda: service)
    monkeypatch.setattr(balance, 'get_payment_methods', AsyncMock(return_value=providers))
    monkeypatch.setattr(Settings, 'is_yookassa_enabled', lambda self: True)
    monkeypatch.setattr(Settings, 'is_wata_enabled', lambda self: True)
    for provider in ('YOOKASSA', 'WATA'):
        monkeypatch.setattr(settings, provider + '_MIN_AMOUNT_KOPEKS', 100)
        monkeypatch.setattr(settings, provider + '_MAX_AMOUNT_KOPEKS', 100000)
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', True)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://checkout.example')
    monkeypatch.setattr(m.maintenance_service, 'is_maintenance_active', lambda: False)
    monkeypatch.setattr(m.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    return SimpleNamespace(redis=redis, cart=cart, user=user, create=create, service=service, providers=providers)


async def test_keyboard_only_issues_tokens_and_reuses_context(setup):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='Карта',
                    callback_data='topup_amount|yookassa|5000',
                    style='primary',
                    icon_custom_emoji_id='123',
                )
            ],
            [InlineKeyboardButton(text='WATA', callback_data='topup_amount|wata|5000')],
            [InlineKeyboardButton(text='Stars', callback_data='topup_amount|stars|5000')],
        ]
    )
    first = await m.checkout_keyboard(keyboard, 1, setup.cart)
    second = await m.checkout_keyboard(keyboard, 1, setup.cart)
    setup.create.assert_not_awaited()
    assert first == second
    button = first.inline_keyboard[0][0]
    assert button.url.startswith('https://checkout.example/cabinet/telegram-checkout#')
    assert button.callback_data is None and button.style == 'primary' and button.icon_custom_emoji_id == '123'
    assert keyboard.inline_keyboard[0][0].callback_data is not None
    assert first.inline_keyboard[2][0].callback_data == 'topup_amount|stars|5000'


def _old_purchase_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Extra SBP', callback_data='topup_amount|yookassa_sbp|5000')],
            [InlineKeyboardButton(text='Old card', callback_data='topup_amount|yookassa|5000')],
            [InlineKeyboardButton(text='Old Wata', callback_data='topup_amount|wata|5000')],
            [InlineKeyboardButton(text='Crypto', callback_data='topup_amount|cryptobot|5000')],
            [InlineKeyboardButton(text='Назад', callback_data='tariff_select:7')],
        ]
    )


async def test_purchase_matches_cabinet_names_order_options_and_urls(setup, monkeypatch):
    from app.cabinet.routes import balance

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    card, wata = setup.providers
    wata.name = 'СБП'
    card.options = [{'id': 'card', 'name': '💳 Карта'}]
    # Same configuration as the cabinet: WATA first, YooKassa card second,
    # CryptoBot and YooKassa SBP omitted even though enabled in the bot ENV.
    setup.providers[:] = [wata, card]
    original = _old_purchase_keyboard()
    db = AsyncMock()
    keyboard = await m.cabinet_checkout_keyboard(original, setup.user, setup.cart, db)
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.text for button in buttons] == ['СБП', 'Карта', 'Назад']
    assert [button.icon_custom_emoji_id for button in buttons[:2]] == ['5192678313415434135', '5192803468762441581']
    assert all(button.style == 'primary' and button.callback_data is None for button in buttons[:2])
    intents = [json.loads(setup.redis.data['telegram_checkout:token:' + b.url.split('#')[1]]) for b in buttons[:2]]
    assert [(item['method'], item['amount']) for item in intents] == [('wata', 5000), ('yookassa', 5000)]
    assert buttons[-1].callback_data == 'tariff_select:7'
    assert original == _old_purchase_keyboard()
    balance.get_payment_methods.assert_awaited_once_with(user=setup.user, db=db)
    setup.create.assert_not_awaited()


async def test_new_render_reads_updated_names_and_only_enabled_sbp_option(setup, monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    setup.providers[0].options = [{'id': 'sbp', 'name': '🏦 СБП'}]
    setup.providers[1].name = 'Банковская карта'
    keyboard = await m.cabinet_checkout_keyboard(_old_purchase_keyboard(), setup.user, setup.cart, AsyncMock())
    assert [row[0].text for row in keyboard.inline_keyboard] == ['СБП', 'Банковская карта', 'Назад']
    intent = json.loads(setup.redis.data['telegram_checkout:token:' + keyboard.inline_keyboard[0][0].url.split('#')[1]])
    assert intent['method'] == 'yookassa_sbp'
    setup.providers[1].name = 'Новое название'
    keyboard = await m.cabinet_checkout_keyboard(_old_purchase_keyboard(), setup.user, setup.cart, AsyncMock())
    assert keyboard.inline_keyboard[1][0].text == 'Новое название'


@pytest.mark.parametrize('reason', ['disabled', 'limit', 'empty'])
async def test_no_env_fallback_when_cabinet_has_no_payable_method(setup, monkeypatch, reason):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    if reason == 'empty':
        setup.providers.clear()
    for method in setup.providers:
        if reason == 'disabled':
            method.is_available = False
        else:
            method.min_amount_kopeks = 5001
    keyboard = await m.cabinet_checkout_keyboard(_old_purchase_keyboard(), setup.user, setup.cart, AsyncMock())
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        'payment_methods_unavailable',
        'tariff_select:7',
    ]
    assert not setup.redis.data
    setup.create.assert_not_awaited()


async def test_method_lookup_failure_does_not_create_links_or_payment(setup, monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(m, '_cabinet_methods', AsyncMock(side_effect=RuntimeError('offline')))
    with pytest.raises(RuntimeError):
        await m.cabinet_checkout_keyboard(_old_purchase_keyboard(), setup.user, setup.cart, AsyncMock())
    assert not setup.redis.data
    setup.create.assert_not_awaited()


@pytest.mark.parametrize('method', ['yookassa', 'yookassa_sbp', 'wata'])
async def test_only_selected_method_created_and_reopening_reuses(setup, method):
    setup.service.create_yookassa_payment = AsyncMock(return_value={'confirmation_url': 'https://pay.example/card'})
    setup.service.create_yookassa_sbp_payment = AsyncMock(return_value={'confirmation_url': 'https://pay.example/sbp'})
    setup.service.create_wata_payment = AsyncMock(return_value={'payment_url': 'https://pay.example/wata'})
    tokens = {name: await m.issue_token(1, setup.cart, name, 5000) for name in m.SUPPORTED_METHODS}
    result = await m.complete_checkout(tokens[method], AsyncMock())
    assert await m.complete_checkout(tokens[method], AsyncMock()) == result
    creators = {
        'yookassa': setup.service.create_yookassa_payment,
        'yookassa_sbp': setup.service.create_yookassa_sbp_payment,
        'wata': setup.service.create_wata_payment,
    }
    for name, creator in creators.items():
        assert creator.await_count == (1 if name == method else 0)
    args = creators[method].await_args.kwargs
    assert args['user_id'] == 1 and args['amount_kopeks'] == 5000
    if method != 'wata':
        assert args['metadata']['purpose'] == ('balance_topup_sbp' if method.endswith('sbp') else 'balance_topup')


async def test_concurrent_posts_claim_once(setup):
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        entered.set()
        await release.wait()
        return {'confirmation_url': 'https://pay.example/card'}

    setup.create.side_effect = create
    token = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    task = asyncio.create_task(m.complete_checkout(token, AsyncMock()))
    await entered.wait()
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout(token, AsyncMock())
    release.set()
    assert await task == 'https://pay.example/card'
    assert setup.create.await_count == 1


@pytest.mark.parametrize('failure', ['timeout', 'empty', 'persist'])
async def test_unknown_outcome_never_recreates_on_retry(setup, failure):
    token = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    if failure == 'timeout':
        setup.create.side_effect = TimeoutError
    elif failure == 'empty':
        setup.create.return_value = None
    else:
        setup.redis.fail_complete = True
    for _ in range(2):
        with pytest.raises(m.CheckoutError):
            await m.complete_checkout(token, AsyncMock())
    assert setup.create.await_count == 1


@pytest.mark.parametrize(
    'reason',
    ['expired', 'cart', 'balance', 'restricted', 'blocked', 'deleted', 'maintenance', 'limits', 'disabled'],
)
async def test_fail_closed_before_provider(setup, reason, monkeypatch):
    token = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    if reason in {'blocked', 'deleted'}:
        setup.user.status = reason
    if reason == 'maintenance':
        monkeypatch.setattr(m.maintenance_service, 'is_maintenance_active', lambda: True)
    if reason == 'expired':
        setup.redis.data.clear()
    if reason == 'cart':
        setup.cart['period_days'] = 90
    if reason == 'balance':
        setup.user.balance_kopeks = 15000
    if reason == 'restricted':
        setup.user.restriction_topup = True
    if reason == 'limits':
        setup.providers[0].min_amount_kopeks = 6000
    if reason == 'disabled':
        monkeypatch.setattr(Settings, 'is_yookassa_enabled', lambda self: False)
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout(token, AsyncMock())
    setup.create.assert_not_awaited()


@pytest.mark.parametrize('method', ['yookassa', 'yookassa_sbp', 'wata'])
async def test_checkout_rechecks_cabinet_filters_before_creating_payment(setup, method):
    from app.cabinet.routes import balance

    token = await m.issue_token(1, setup.cart, method, 5000)
    if method == 'yookassa_sbp':
        setup.providers[0].options = [{'id': 'card', 'name': '💳 Карта'}]
    elif method == 'yookassa':
        setup.providers[0].min_amount_kopeks = 999999
    else:
        setup.providers[1].is_available = False
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout(token, AsyncMock())
    balance.get_payment_methods.assert_awaited_once()
    setup.create.assert_not_awaited()


@pytest.mark.parametrize('method', ['yookassa', 'yookassa_sbp', 'wata'])
@pytest.mark.parametrize('amount,allowed', [(4999, False), (5000, True), (6000, True), (6001, False)])
async def test_cabinet_provider_limits_are_inclusive(setup, method, amount, allowed):
    provider = setup.providers[1 if method == 'wata' else 0]
    provider.min_amount_kopeks = 5000
    provider.max_amount_kopeks = 6000
    setup.user.balance_kopeks = setup.cart['total_price'] - amount
    token = await m.issue_token(1, setup.cart, method, amount)
    if allowed:
        assert await m.complete_checkout(token, AsyncMock()) == 'https://pay.example/test'
        setup.create.assert_awaited_once()
    else:
        with pytest.raises(m.CheckoutError):
            await m.complete_checkout(token, AsyncMock())
        setup.create.assert_not_awaited()


@pytest.mark.parametrize('method', ['yookassa', 'yookassa_sbp', 'wata'])
async def test_telegram_provider_disabled_rejected(setup, monkeypatch, method):
    if method == 'yookassa_sbp':
        monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', False)
    else:
        monkeypatch.setattr(Settings, 'is_' + method + '_enabled', lambda self: False)
    token = await m.issue_token(1, setup.cart, method, 5000)
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout(token, AsyncMock())
    setup.create.assert_not_awaited()


async def test_owner_rate_budget_is_independent_behind_same_proxy(setup):
    counts = {}

    async def limit(user_id, action, *, limit, window, fail_closed):
        assert (action, limit, window, fail_closed) == ('telegram_checkout', 30, 60, True)
        counts[user_id] = counts.get(user_id, 0) + 1
        return counts[user_id] > limit

    m.RateLimitCache.is_rate_limited.side_effect = limit
    first = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    second = await m.issue_token(2, setup.cart, 'yookassa', 5000)
    for _ in range(30):
        await m.complete_checkout(first, AsyncMock())
    m.get_user_by_id.reset_mock()
    setup.create.reset_mock()
    with pytest.raises(m.CheckoutRateLimited):
        await m.complete_checkout(first, AsyncMock())
    m.get_user_by_id.assert_not_awaited()
    setup.create.assert_not_awaited()
    await m.complete_checkout(second, AsyncMock())
    assert counts == {1: 31, 2: 1}
    setup.create.assert_awaited_once()


async def test_unknown_token_never_allocates_owner_budget_or_loads_user(setup):
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout('x' * 43, AsyncMock())
    m.RateLimitCache.is_rate_limited.assert_not_awaited()
    m.get_user_by_id.assert_not_awaited()
    setup.create.assert_not_awaited()


async def test_unavailable_owner_limiter_fails_closed_before_user_lookup(setup, monkeypatch):
    from app.utils.cache import RateLimitCache, cache

    # Exercise the actual cache implementation, rather than its fixture stub.
    monkeypatch.setattr(cache, '_connected', False)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', _REAL_OWNER_LIMITER)
    token = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    with pytest.raises(m.CheckoutRateLimited):
        await m.complete_checkout(token, AsyncMock())
    m.get_user_by_id.assert_not_awaited()
    setup.create.assert_not_awaited()


async def test_static_page_and_origin_csrf_checks(setup):
    from app.cabinet.dependencies import get_cabinet_db
    from app.cabinet.routes import telegram_checkout as routes

    app = FastAPI()
    app.include_router(routes.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()
    token = await m.issue_token(1, setup.cart, 'yookassa', 5000)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://checkout.example') as client:
        page = await client.get('/cabinet/telegram-checkout')
        assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
        assert 'location.hash' in page.text and 'history.replaceState' in page.text
        assert token not in page.text
        assert (await client.head('/cabinet/telegram-checkout')).status_code == 405
        setup.create.assert_not_awaited()
        csrf = client.cookies.get('telegram_checkout_csrf')
        bad = await client.post(
            '/cabinet/telegram-checkout',
            json={'token': token, 'csrf': csrf},
            headers={'Origin': 'https://evil.example'},
        )
        assert bad.status_code == 403
        setup.create.assert_not_awaited()
        good = await client.post(
            '/cabinet/telegram-checkout',
            json={'token': token, 'csrf': csrf},
            headers={'Origin': 'https://checkout.example'},
        )
        assert good.status_code == 200 and good.json()['url'] == 'https://pay.example/test'
        m.RateLimitCache.is_rate_limited.side_effect = lambda user_id, *args, **kwargs: user_id == 1
        m.get_user_by_id.reset_mock()
        setup.create.reset_mock()
        limited = await client.post(
            '/cabinet/telegram-checkout',
            json={'token': token, 'csrf': csrf},
            headers={'Origin': 'https://checkout.example'},
        )
        assert limited.status_code == 429
        m.get_user_by_id.assert_not_awaited()
        setup.create.assert_not_awaited()
        other_token = await m.issue_token(2, setup.cart, 'yookassa', 5000)
        other = await client.post(
            '/cabinet/telegram-checkout',
            json={'token': other_token, 'csrf': csrf},
            headers={'Origin': 'https://checkout.example'},
        )
        assert other.status_code == 200  # Same client/proxy IP, independent intent owner.


@pytest.mark.parametrize('reason', ['token', 'extra', 'type', 'json', 'content_type', 'oversized_stream'])
async def test_ingress_rejects_before_checkout_without_echoing_credentials(setup, monkeypatch, reason):
    from app.cabinet.dependencies import get_cabinet_db
    from app.cabinet.routes import telegram_checkout as routes

    app = FastAPI()
    app.include_router(routes.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()
    complete = AsyncMock()
    monkeypatch.setattr(routes, 'complete_checkout', complete)
    token = 'S' * 43
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://checkout.example') as client:
        await client.get('/cabinet/telegram-checkout')
        body = {'token': token, 'csrf': client.cookies.get('telegram_checkout_csrf')}
        expected = 422
        if reason == 'token':
            body['token'] += '!'
        elif reason == 'extra':
            body['user_id'] = 999
        elif reason == 'type':
            body['csrf'] = 123
        content = json.dumps(body)
        content_type = 'application/json'
        if reason == 'json':
            content += ' broken'
        elif reason == 'content_type':
            content_type = 'text/plain'
            expected = 415
        elif reason == 'oversized_stream':
            first_chunk = (content + ' ' * 700).encode()

            async def chunks():
                yield first_chunk
                yield b' ' * 700

            content = chunks()
            expected = 413
        result = await client.post(
            '/cabinet/telegram-checkout',
            content=content,
            headers={'Origin': 'https://checkout.example', 'Content-Type': content_type},
        )
    assert result.status_code == expected
    assert token not in result.text and 'csrf' not in result.text
    complete.assert_not_awaited()
    setup.create.assert_not_awaited()
    m.RateLimitCache.is_rate_limited.assert_not_awaited()


@pytest.mark.parametrize('restore', [False, True])
@pytest.mark.parametrize('balance', [10000, 15000])
async def test_purchase_wiring_prepares_only_insufficient_links(monkeypatch, restore, balance):
    from app.handlers.subscription import tariff_purchase as purchase
    from app.services.pricing_engine import pricing_engine

    tariff = SimpleNamespace(
        id=7, name='Plan', is_active=True, is_daily=False, device_limit=2, traffic_limit_gb=300, allowed_squads=[]
    )
    user = SimpleNamespace(id=1, language='ru', balance_kopeks=balance)
    callback = SimpleNamespace(
        data='tariff_period:7:30', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_state=AsyncMock())
    prepared = AsyncMock(side_effect=lambda keyboard, *args: keyboard)
    monkeypatch.setattr(purchase, 'cabinet_checkout_keyboard', prepared)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(purchase, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(purchase.user_cart_service, 'save_user_cart', AsyncMock())
    monkeypatch.setattr(
        pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(
            return_value=SimpleNamespace(
                original_total=15000, final_total=15000, promo_group_discount=0, promo_offer_discount=0
            )
        ),
    )
    if restore:
        await purchase.return_to_saved_tariff_cart(
            callback,
            state,
            user,
            AsyncMock(),
            dict(cart_mode='tariff_purchase', tariff_id=7, period_days=30, total_price=15000),
        )
    else:
        await purchase.select_tariff_period.__wrapped__(callback, user, AsyncMock(), state)
    assert prepared.await_count == (1 if balance < 15000 else 0)
