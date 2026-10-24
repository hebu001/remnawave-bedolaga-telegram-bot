from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings, settings
from app.services import payment_method_config_service as methods, telegram_checkout as checkout
from app.services.system_settings_service import BotConfigurationService
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available
from tests.services.test_telegram_checkout import setup  # noqa: F401


def test_cardlink_configuration_and_secret_mask():
    config = Settings(_env_file=None, CARDLINK_ENABLED=True, CARDLINK_API_TOKEN=' ', CARDLINK_SHOP_ID='shop')
    assert not config.is_cardlink_configured()
    config.CARDLINK_API_TOKEN = 'secret'
    assert config.is_cardlink_configured() and config.is_cardlink_enabled()
    config.CARDLINK_ENABLED = False
    assert config.is_cardlink_configured() and not config.is_cardlink_enabled()
    assert (
        BotConfigurationService.mask_secret_value('CARDLINK_API_TOKEN', 'secret') == BotConfigurationService.SECRET_MASK
    )


def test_cardlink_selector_name_limits_and_disable(monkeypatch):
    monkeypatch.setattr(settings, 'CARDLINK_ENABLED', True)
    monkeypatch.setattr(settings, 'CARDLINK_API_TOKEN', 'secret')
    monkeypatch.setattr(settings, 'CARDLINK_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CARDLINK_DISPLAY_NAME', 'Карта Cardlink')
    defaults = methods._get_method_defaults()['cardlink']
    assert defaults['default_display_name'] == 'Карта Cardlink'
    assert defaults['is_configured']
    assert defaults['default_min'] == settings.CARDLINK_MIN_AMOUNT_KOPEKS
    assert defaults['default_max'] == settings.CARDLINK_MAX_AMOUNT_KOPEKS
    assert 'cardlink' in methods.DEFAULT_METHOD_ORDER
    assert is_payment_method_available('cardlink')
    assert any(item['id'] == 'cardlink' for item in get_available_payment_methods())
    monkeypatch.setattr(settings, 'CARDLINK_ENABLED', False)
    assert not is_payment_method_available('cardlink')
    assert not any(item['id'] == 'cardlink' for item in get_available_payment_methods())


async def test_cardlink_checkout_returns_cached_direct_provider_url(setup, monkeypatch):
    monkeypatch.setattr(Settings, 'is_cardlink_enabled', lambda self: True)
    setup.service.create_cardlink_payment = AsyncMock(return_value={'payment_url': 'https://cardlink.link/bill/123'})
    setup.providers[:] = [
        SimpleNamespace(
            id='cardlink',
            name='Моя карта',
            min_amount_kopeks=100,
            max_amount_kopeks=100000,
            is_available=True,
            options=None,
        )
    ]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Моя карта', callback_data='topup_amount|cardlink|5000')]]
    )
    first = await checkout.checkout_keyboard(keyboard, setup.user.id, setup.cart, AsyncMock())
    second = await checkout.checkout_keyboard(keyboard, setup.user.id, setup.cart, AsyncMock())
    assert first == second
    assert first.inline_keyboard[0][0].url == 'https://cardlink.link/bill/123'
    assert first.inline_keyboard[0][0].callback_data is None
    setup.service.create_cardlink_payment.assert_awaited_once()
    monkeypatch.setattr(Settings, 'is_cardlink_enabled', lambda self: False)
    refused = await checkout.checkout_keyboard(keyboard, setup.user.id, setup.cart, AsyncMock())
    assert refused.inline_keyboard[0][0].url is None
    assert refused.inline_keyboard[0][0].callback_data == 'return_to_saved_cart'


async def test_subpage_accepts_cardlink_in_configured_order(monkeypatch):
    from app.cabinet.routes.subpage import _available_methods

    monkeypatch.setattr(
        methods,
        'get_enabled_methods_for_user',
        AsyncMock(
            return_value=[
                {'id': 'cardlink', 'name': 'Моя карта'},
                {'id': 'cryptobot', 'name': 'Crypto'},
                {'id': 'wata', 'name': 'СБП'},
            ]
        ),
    )
    result = await _available_methods(AsyncMock(), SimpleNamespace())
    assert [(item.id, item.name) for item in result] == [('cardlink', 'Моя карта'), ('wata', 'СБП')]


async def test_legacy_status_does_not_refresh_another_users_payment(monkeypatch):
    from app.database.crud import cardlink
    from app.webapi.routes.miniapp import _resolve_cardlink_payment_status
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    monkeypatch.setattr(
        cardlink, 'get_cardlink_payment_by_local_id', AsyncMock(return_value=SimpleNamespace(id=22, user_id=999))
    )
    service = SimpleNamespace(get_cardlink_payment_status=AsyncMock())
    result = await _resolve_cardlink_payment_status(
        service, AsyncMock(), SimpleNamespace(id=1), MiniAppPaymentStatusQuery(method='cardlink', localPaymentId=22)
    )
    assert result.message == 'Payment not found'
    service.get_cardlink_payment_status.assert_not_awaited()


async def test_legacy_status_returns_owned_paid_record_while_creation_disabled(monkeypatch):
    from app.database.crud import cardlink
    from app.webapi.routes.miniapp import _resolve_cardlink_payment_status
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    monkeypatch.setattr(settings, 'CARDLINK_ENABLED', False)
    payment = SimpleNamespace(
        id=22,
        user_id=1,
        is_paid=True,
        status='SUCCESS',
        amount_kopeks=12345,
        currency='RUB',
        paid_at=None,
        transaction_id=42,
        bill_id='bill-1',
        order_id='order-1',
    )
    monkeypatch.setattr(cardlink, 'get_cardlink_payment_by_local_id', AsyncMock(return_value=payment))
    service = SimpleNamespace(get_cardlink_payment_status=AsyncMock(return_value={'payment': payment}))
    result = await _resolve_cardlink_payment_status(
        service, AsyncMock(), SimpleNamespace(id=1), MiniAppPaymentStatusQuery(method='cardlink', localPaymentId=22)
    )
    assert result.status == 'paid' and result.is_paid
    assert result.external_id == 'bill-1' and result.amount_kopeks == 12345
    service.get_cardlink_payment_status.assert_awaited_once()
