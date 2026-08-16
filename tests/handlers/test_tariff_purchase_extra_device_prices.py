from types import SimpleNamespace

from app.handlers.subscription.tariff_purchase import (
    format_tariffs_list_text,
    get_tariff_periods_keyboard,
)


def _tariff():
    return SimpleNamespace(
        id=4,
        name='Ежемесячный',
        description=None,
        traffic_limit_gb=100,
        device_limit=2,
        device_price_kopeks=5000,
        period_prices={'30': 15000, '90': 40000},
        is_daily=False,
        allowed_promo_groups=[],
        is_available_for_promo_group=lambda _promo_group_id: True,
    )


def _user():
    return SimpleNamespace(
        language='ru',
        promo_offer_discount_percent=0,
        promo_offer_discount_expires_at=None,
        get_primary_promo_group=lambda: None,
    )


async def test_tariff_catalog_price_includes_existing_extra_devices():
    text = await format_tariffs_list_text(
        [_tariff()],
        _user(),
        subscription_device_limits={4: 3},
    )

    assert '/ 3 📱' in text
    assert '200' in text  # 150 ₽ tariff + 50 ₽ for the third device
    assert '150' not in text


async def test_tariff_period_buttons_include_existing_extra_devices():
    keyboard = await get_tariff_periods_keyboard(
        _tariff(),
        'ru',
        db_user=_user(),
        device_limit=3,
    )

    first_period_button = keyboard.inline_keyboard[0][0].text
    assert '200' in first_period_button
    assert '150' not in first_period_button
