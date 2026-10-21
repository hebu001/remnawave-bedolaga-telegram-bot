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
    from app.cabinet.routes import balance, telegram_checkout as routes

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
        SimpleNamespace(id=method, min_amount_kopeks=100, max_amount_kopeks=100000, is_available=True, options=None)
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
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', True)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://checkout.example')
    monkeypatch.setattr(m.maintenance_service, 'is_maintenance_active', lambda: False)
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
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
    ['expired', 'cart', 'balance', 'restricted', 'blocked', 'deleted', 'maintenance', 'limits', 'disabled', 'option'],
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
    if reason == 'option':
        setup.providers[0].options = [{'id': 'sbp'}]
    with pytest.raises(m.CheckoutError):
        await m.complete_checkout(token, AsyncMock())
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


@pytest.mark.parametrize('reason', ['token', 'extra', 'type', 'json', 'content_type', 'oversized_stream', 'rate_limit'])
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
        elif reason == 'rate_limit':
            routes.RateLimitCache.is_ip_rate_limited.return_value = True
            expected = 429
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
    routes.RateLimitCache.is_ip_rate_limited.assert_awaited_once_with(
        '127.0.0.1', 'telegram_checkout', limit=30, window=60, fail_closed=True
    )


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
    monkeypatch.setattr(purchase, 'checkout_keyboard', prepared)
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
