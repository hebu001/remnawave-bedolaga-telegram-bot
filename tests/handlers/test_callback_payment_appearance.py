from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from app.handlers.subscription import tariff_purchase as m
from app.keyboards.inline import get_payment_methods_keyboard
from app.services import telegram_checkout
from app.services.pricing_engine import pricing_engine
from app.utils.rich_buttons import render_keyboard_as_rich_html


@pytest.fixture(autouse=True)
def configured_payment_methods(monkeypatch):
    monkeypatch.setattr(
        telegram_checkout,
        '_cabinet_methods',
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    id='telegram_stars',
                    name='Telegram Stars',
                    is_available=True,
                    min_amount_kopeks=100,
                    max_amount_kopeks=1000000,
                    options=None,
                )
            ]
        ),
    )


def _price():
    return SimpleNamespace(original_total=15000, final_total=15000, promo_group_discount=0, promo_offer_discount=0)


def _tariff():
    return SimpleNamespace(
        id=7,
        name='Plan',
        is_active=True,
        is_daily=False,
        device_limit=2,
        traffic_limit_gb=300,
        allowed_squads=[],
        period_prices={'30': 15000},
    )


@pytest.mark.parametrize(
    'factory,args,callback',
    [
        (m.get_tariff_confirm_keyboard, (7, 30, 'ru'), 'tariff_confirm:7:30'),
        (m.get_daily_tariff_confirm_keyboard, (7, 'ru'), 'daily_tariff_confirm:7'),
        (m.get_tariff_extend_confirm_keyboard, (11, 7, 30, 'ru'), 'tariff_ext_confirm:11:7:30'),
    ],
)
def test_balance_payment_button_preserves_callback_in_both_payloads(factory, args, callback):
    keyboard = factory(*args)
    button = keyboard.inline_keyboard[0][0]
    payload = button.model_dump(exclude_none=True)
    assert payload['text'] == 'Оплатить балансом'
    assert payload['callback_data'] == callback
    assert payload['style'] == 'primary'
    assert payload['icon_custom_emoji_id'] == '5375112143144263548'
    rich = render_keyboard_as_rich_html(keyboard)
    assert f'data="{callback}" style="primary"' in rich
    assert '<tg-emoji emoji-id="5375112143144263548">' in rich


@pytest.mark.parametrize('with_traffic', [False, True])
async def test_period_buttons_are_green_and_back_is_unchanged(monkeypatch, with_traffic):
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=_price()))
    factory = m.get_tariff_periods_keyboard_with_traffic if with_traffic else m.get_tariff_periods_keyboard
    keyboard = await factory(_tariff(), 'ru')
    assert keyboard.inline_keyboard[0][0].style == 'success'
    assert 'style="success"' in render_keyboard_as_rich_html(keyboard)
    assert keyboard.inline_keyboard[-1][0].style is None


@pytest.mark.parametrize('amount', [0, 15000])
@pytest.mark.parametrize('stars', [False, True])
def test_payment_icons_follow_visible_order_after_disabled_methods(monkeypatch, amount, stars):
    for name in dir(Settings):
        if name.startswith('is_') and name.endswith('_enabled') and callable(getattr(Settings, name)):
            monkeypatch.setattr(Settings, name, lambda self: False)
    monkeypatch.setattr(Settings, 'is_yookassa_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', stars)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', False)
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', True)
    keyboard = get_payment_methods_keyboard(amount, 'ru')
    buttons = [b for row in keyboard.inline_keyboard for b in row if (b.callback_data or '').startswith('topup_')]
    expected = (['stars'] if stars else []) + ['yookassa_sbp', 'yookassa']
    assert [b.callback_data for b in buttons] == [
        f'topup_amount|{method}|{amount}' if amount else f'topup_{method}' for method in expected
    ]
    assert [b.icon_custom_emoji_id for b in buttons[:2]] == ['5192678313415434135', '5192803468762441581']
    assert all(b.style == 'primary' for b in buttons)
    assert keyboard.inline_keyboard[-1][0].style is None
    rich = render_keyboard_as_rich_html(keyboard)
    assert rich.count('style="primary"') == len(buttons)
    assert '<tg-emoji emoji-id="5192678313415434135">' in rich


async def test_insufficient_balance_still_saves_cart_without_cart_notice(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(
        m, 'get_subscription_by_user_id', AsyncMock(return_value=SimpleNamespace(id=11, tariff_id=7, device_limit=2))
    )
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=_price()))
    save = AsyncMock()
    monkeypatch.setattr(m.user_cart_service, 'save_user_cart', save)
    callback = SimpleNamespace(
        data='tariff_period:7:30', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_state=AsyncMock())
    user = SimpleNamespace(id=123, language='ru', balance_kopeks=10000)
    await m.select_tariff_period.__wrapped__(callback, user, AsyncMock(), state)
    cart = save.await_args.args[1]
    assert (cart['total_price'], cart['missing_amount'], cart['subscription_id']) == (15000, 5000, 11)
    assert cart['saved_cart'] is True
    assert 'Корзина сохранена' not in callback.message.edit_text.await_args.args[0]
    assert 'автоматически' not in callback.message.edit_text.await_args.args[0]


def test_recurring_choices_are_styled_after_prefilled_methods(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(Settings, 'is_platega_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(Settings, 'is_lava_recurrent_enabled', lambda self: True)
    keyboard = m.get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=5000)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    choices = [
        button
        for button in buttons
        if (button.callback_data or '').startswith(('topup_amount|', 'tariff_sbp:', 'tariff_lava:'))
    ]
    assert choices[0].callback_data == 'topup_amount|stars|5000'
    assert [button.icon_custom_emoji_id for button in choices[:2]] == ['5192678313415434135', '5192803468762441581']
    assert all(button.style == 'primary' for button in choices)
    assert choices[0].text == 'Telegram Stars'
    assert buttons[-1].callback_data == 'tariff_select:7'


def test_renewal_period_preserves_target_and_has_green_payload():
    keyboard = m.get_tariff_extend_keyboard(_tariff(), 'ru', subscription_id=11)
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == 'tariff_extend:11:7:30'
    assert button.style == 'success'
    assert 'style="success"' in render_keyboard_as_rich_html(keyboard)


@pytest.mark.parametrize('restore', [False, True])
@pytest.mark.parametrize(
    'cost,balance',
    [
        (15000, 0),
        (15000, 10000),
        (15000, 6501),
        (15000, 15000),
        (15000, 20000),
        (12000, 0),
        (12000, 10000),
        (12000, 12000),
        (12000, 20000),
        (0, 0),
    ],
)
async def test_purchase_preview_keeps_extra_devices_and_new_fields(monkeypatch, restore, cost, balance):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(
        m, 'get_subscription_by_user_id', AsyncMock(return_value=SimpleNamespace(id=11, tariff_id=7, device_limit=5))
    )
    price = SimpleNamespace(
        original_total=15000, final_total=cost, promo_group_discount=15000 - cost, promo_offer_discount=0
    )
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=price))
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(m.user_cart_service, 'save_user_cart', AsyncMock())
    callback = SimpleNamespace(
        data='tariff_period:7:30', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_state=AsyncMock())
    user = SimpleNamespace(id=123, language='ru', balance_kopeks=balance)
    if restore:
        await m.return_to_saved_tariff_cart(
            callback,
            state,
            user,
            AsyncMock(),
            dict(
                cart_mode='tariff_purchase',
                tariff_id=7,
                subscription_id=11,
                period_days=30,
                device_limit=5,
                total_price=cost,
                discount_percent=round((1 - cost / 15000) * 100),
            ),
        )
    else:
        await m.select_tariff_period.__wrapped__(callback, user, AsyncMock(), state)
    text = callback.message.edit_text.await_args.args[0]
    assert '• Тариф: <b>Plan</b>\n• Трафик: 300 ГБ\n• Период: 30 дней\n• Устройств: 5' in text
    assert 'После оплаты' not in text
    assert 'Стоимость:' not in text
    assert ('Ваш баланс:' in text) == (balance != 0)
    assert '\n\n\n' not in text
    assert 'Не хватает' not in text
    assert 'Недостаточно' not in text
    assert text.startswith('<b>Подтверждение покупки</b>\n\n• Тариф:')
    from app.utils.formatting import format_price_kopeks

    emoji = '<tg-emoji emoji-id="5278613311858959074">🛒</tg-emoji>'
    display_total = cost - balance if 0 < balance < cost else cost
    assert emoji + '<b>Итого: ' + format_price_kopeks(display_total) + '</b>' in text
    if (cost, balance) == (15000, 6501):
        assert '<b>Итого: 84.99 ₽</b>' in text
    if cost < 15000:
        assert 'Скидка:' in text
    buttons = [
        button for row in callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard for button in row
    ]
    confirmations = [button for button in buttons if button.callback_data == 'tariff_confirm:7:30']
    assert bool(confirmations) == (balance >= cost)
    if confirmations:
        assert confirmations[0].text == 'Оплатить балансом'
        assert confirmations[0].style == 'primary'
        assert confirmations[0].icon_custom_emoji_id == '5375112143144263548'
    else:
        assert any(button.callback_data == f'topup_amount|stars|{cost - balance}' for button in buttons)


@pytest.mark.parametrize('language', ['en', 'fa', 'ua', 'zh'])
@pytest.mark.parametrize('restore', [False, True])
async def test_non_russian_insufficient_purchase_keeps_existing_locale(monkeypatch, language, restore):
    from app.localization.texts import get_texts
    from app.utils.formatting import format_period, format_price_kopeks

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(m, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=_price()))
    monkeypatch.setattr(m.user_cart_service, 'save_user_cart', AsyncMock())
    callback = SimpleNamespace(
        data='tariff_period:7:30', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_state=AsyncMock())
    user = SimpleNamespace(id=123, language=language, balance_kopeks=10000)
    if restore:
        await m.return_to_saved_tariff_cart(
            callback,
            state,
            user,
            AsyncMock(),
            dict(cart_mode='tariff_purchase', tariff_id=7, period_days=30, total_price=15000),
        )
    else:
        await m.select_tariff_period.__wrapped__(callback, user, AsyncMock(), state)
    key = 'TARIFF_PURCHASE_STILL_INSUFFICIENT' if restore else 'TARIFF_PURCHASE_INSUFFICIENT'
    expected = (
        get_texts(language)
        .t(key)
        .format(
            name='Plan',
            period=format_period(30),
            price=format_price_kopeks(15000),
            balance=format_price_kopeks(10000),
            missing=format_price_kopeks(5000),
        )
    )
    assert callback.message.edit_text.await_args.args[0] == expected


@pytest.mark.parametrize('stored,expected', [({}, 2), ({'device_limit': 0, 'traffic_limit_gb': 600}, 0)])
@pytest.mark.parametrize('balance', [10000, 20000])
async def test_saved_cart_devices_fallback_preserves_explicit_zero(monkeypatch, stored, expected, balance):
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())
    user = SimpleNamespace(id=123, language='ru', balance_kopeks=balance)
    cart = dict(cart_mode='tariff_purchase', tariff_id=7, period_days=30, total_price=15000, **stored)
    await m.return_to_saved_tariff_cart(callback, state, user, AsyncMock(), cart)
    assert f'• Устройств: {expected}\n' in callback.message.edit_text.await_args.args[0]
    assert f'• Трафик: {stored.get("traffic_limit_gb", 300)} ГБ\n' in callback.message.edit_text.await_args.args[0]
