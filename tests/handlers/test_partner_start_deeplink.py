from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram import types

from app.database.models import UserStatus
from app.handlers import start
from app.middlewares import channel_checker
from app.utils.start_parameters import PENDING_PARTNER_MENU_KEY, is_partner_menu_start_parameter


def test_partner_start_parameter_is_exact_and_reserved():
    assert is_partner_menu_start_parameter('partner') is True
    assert is_partner_menu_start_parameter('PARTNER') is False
    assert is_partner_menu_start_parameter('partner_extra') is False
    assert is_partner_menu_start_parameter(None) is False


async def test_partner_start_opens_callback_section_for_registered_user(monkeypatch):
    user = SimpleNamespace(
        id=1,
        telegram_id=12345,
        status=UserStatus.ACTIVE.value,
        referral_code='besw1fG5',
        language='ru',
    )
    message = MagicMock()
    message.text = '/start partner'
    message.from_user = SimpleNamespace(id=user.telegram_id)
    message.bot = MagicMock()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    show_partner = AsyncMock()

    monkeypatch.setattr(start, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr('app.handlers.referral.show_referral_info_message', show_partner)

    db = MagicMock()
    await start.cmd_start(message, state, db, db_user=user)

    show_partner.assert_awaited_once_with(message, user, db)
    state.clear.assert_awaited_once()


async def test_channel_gate_preserves_partner_menu_intent(monkeypatch):
    stored_state = {}
    state = MagicMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(stored_state))

    async def set_data(data):
        stored_state.clear()
        stored_state.update(data)

    state.set_data = AsyncMock(side_effect=set_data)
    save_redis = AsyncMock(return_value=True)
    monkeypatch.setattr(channel_checker, 'save_pending_payload_to_redis', save_redis)

    message = types.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=12345, type='private'),
        from_user=types.User(id=12345, is_bot=False, first_name='Test'),
        text='/start partner',
    )

    await channel_checker.ChannelCheckerMiddleware()._capture_start_payload(state, message)

    assert stored_state[PENDING_PARTNER_MENU_KEY] is True
    assert stored_state['pending_start_payload'] == 'partner'
    save_redis.assert_awaited_once_with(12345, 'partner')
