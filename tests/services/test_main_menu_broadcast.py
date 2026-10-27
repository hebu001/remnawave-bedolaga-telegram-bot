"""Personal menu broadcasts keep delivery accounting and /start side effects separate."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.handlers import menu, start
from app.services import main_menu_broadcast_service as sender
from app.utils import rich_menu
from tests.utils.test_rich_menu import DummyTexts, _make_subscription, _make_user


@pytest.fixture
def env(monkeypatch):
    rich_menu._reset_rich_menu_availability()
    for name, value in (
        ('MAIN_MENU_RICH_ENABLED', True),
        ('MAIN_MENU_RICH_EFFECT_ID', ''),
        ('MAIN_MENU_RICH_INLINE_BUTTONS', False),
        ('MAIN_MENU_RICH_LOGO_URL', ''),
        ('WEBHOOK_URL', None),
        ('CABINET_URL', 'https://cabinet.example.test'),
        ('HIDE_SUBSCRIPTION_LINK', False),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, telegram_id: telegram_id == 100)
    monkeypatch.setattr(type(settings), 'is_text_main_menu_mode', lambda self: True)
    monkeypatch.setattr(menu.SupportSettingsService, 'is_moderator', lambda telegram_id: False)
    monkeypatch.setattr(menu, 'has_subscription_checkout_draft', AsyncMock(return_value=False))
    monkeypatch.setattr(menu, 'should_offer_checkout_resume', lambda *_args: False)
    monkeypatch.setattr(menu.user_cart_service, 'has_user_cart', AsyncMock(return_value=False))

    state = SimpleNamespace(open_sessions=0, sessions=[], users={}, keyboard_contexts=[])
    for telegram_id in (100, 200):
        subscription = _make_subscription(datetime.now(UTC))
        subscription.is_active = True
        subscription.subscription_url = f'https://sub.example.test/u/{telegram_id}'
        user = _make_user(subscription)
        user.id = telegram_id
        user.telegram_id = telegram_id
        user.status = 'active'
        user.full_name = f'User {telegram_id}'
        user.has_had_paid_subscription = True
        user.last_activity = datetime(2026, 1, 1, tzinfo=UTC)
        state.users[telegram_id] = user

    @asynccontextmanager
    async def session():
        db = AsyncMock()
        state.sessions.append(db)
        state.open_sessions += 1
        try:
            yield db
        finally:
            state.open_sessions -= 1

    async def load_user(db, telegram_id):
        assert state.open_sessions == 1
        return state.users.get(telegram_id)

    async def build_keyboard(**kwargs):
        assert state.open_sessions == 1
        state.keyboard_contexts.append(kwargs)
        role = 'admin' if kwargs['is_admin'] else 'user'
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=role, callback_data=f'menu_{role}')]])

    async def fallback_text(user, texts, db):
        assert state.open_sessions == 1
        return f'Profile {user.telegram_id}'

    async def send(**kwargs):
        assert state.open_sessions == 0, 'Telegram I/O must not hold a database session'
        return SimpleNamespace(message_id=kwargs['chat_id'] + 10)

    monkeypatch.setattr(sender, 'AsyncSessionLocal', session)
    monkeypatch.setattr(sender, 'get_user_by_telegram_id', AsyncMock(side_effect=load_user))
    monkeypatch.setattr(sender, 'get_texts', lambda language: DummyTexts())
    monkeypatch.setattr(menu, 'get_main_menu_keyboard_async', AsyncMock(side_effect=build_keyboard))
    monkeypatch.setattr(menu, 'get_main_menu_text', AsyncMock(side_effect=fallback_text))
    monkeypatch.setattr(start, 'cmd_start', AsyncMock(side_effect=AssertionError('Must not run /start')))
    monkeypatch.setattr(rich_menu, 'remember_live_menu', AsyncMock())
    monkeypatch.setattr(rich_menu, 'get_start_video_file_id', AsyncMock(return_value=None))
    state.bot = SimpleNamespace(send_rich_message=AsyncMock(side_effect=send), send_message=AsyncMock(side_effect=send))
    yield state
    rich_menu._reset_rich_menu_availability()


async def test_personalized_menu_roles_links_and_live_tracking_without_start(env):
    old_activity = {key: user.last_activity for key, user in env.users.items()}
    await sender.send_current_main_menu(env.bot, 100)
    await sender.send_current_main_menu(env.bot, 200)

    first, second = env.bot.send_rich_message.await_args_list
    assert first.kwargs['chat_id'] == 100
    assert 'https://sub.example.test/u/100' in first.kwargs['rich_message'].html
    assert 'https://sub.example.test/u/200' not in first.kwargs['rich_message'].html
    assert second.kwargs['chat_id'] == 200
    assert 'https://sub.example.test/u/200' in second.kwargs['rich_message'].html
    assert 'https://sub.example.test/u/100' not in second.kwargs['rich_message'].html
    assert first.kwargs['reply_markup'].inline_keyboard[0][0].text == 'admin'
    assert second.kwargs['reply_markup'].inline_keyboard[0][0].text == 'user'
    assert all(context['has_active_subscription'] for context in env.keyboard_contexts)
    assert [call.args[1] for call in rich_menu.remember_live_menu.await_args_list] == [110, 210]
    assert rich_menu.remember_live_menu.await_args_list[0].args[2] is env.users[100]
    assert len(env.sessions) == 2
    assert env.open_sessions == 0
    for db in env.sessions:
        db.commit.assert_not_awaited()
        db.flush.assert_not_awaited()
    assert {key: user.last_activity for key, user in env.users.items()} == old_activity
    start.cmd_start.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('status', [None, 'deleted', 'blocked'])
async def test_missing_or_inactive_user_is_not_sent(env, status):
    if status is None:
        del env.users[100]
    else:
        env.users[100].status = status
    with pytest.raises(ValueError, match='missing or inactive'):
        await sender.send_current_main_menu(env.bot, 100)
    env.bot.send_rich_message.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()
    assert env.open_sessions == 0


async def test_disabled_rich_uses_personal_classic_menu_after_closing_session(env, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False)
    await sender.send_current_main_menu(env.bot, 200)
    env.bot.send_rich_message.assert_not_awaited()
    assert env.bot.send_message.await_args.kwargs['text'] == 'Profile 200'
    assert env.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text == 'user'
    rich_menu.remember_live_menu.assert_not_awaited()


@pytest.mark.parametrize(
    ('error', 'supported_after'),
    [
        (TelegramNotFound(method=None, message='Not Found'), False),
        (TelegramBadRequest(method=None, message='unknown method'), False),
        (TelegramBadRequest(method=None, message='invalid rich message'), True),
    ],
)
async def test_definitive_rejection_falls_back_once(env, error, supported_after):
    env.bot.send_rich_message.side_effect = error
    await sender.send_current_main_menu(env.bot, 100)
    assert env.bot.send_rich_message.await_count == 1
    assert env.bot.send_message.await_count == 1
    assert rich_menu.is_rich_menu_enabled() is supported_after
    rich_menu.remember_live_menu.assert_not_awaited()


@pytest.mark.parametrize(
    'error',
    [
        TelegramForbiddenError(method=None, message='blocked'),
        TelegramRetryAfter(method=None, message='retry', retry_after=5),
        TelegramNetworkError(method=None, message='timeout'),
        TelegramServerError(method=None, message='Internal Server Error'),
        ClientDecodeError('bad response', ValueError('unknown block'), {}),
    ],
)
async def test_delivery_errors_propagate_without_extra_send(env, error):
    env.bot.send_rich_message.side_effect = error
    with pytest.raises(type(error)) as raised:
        await sender.send_current_main_menu(env.bot, 100)
    assert raised.value is error
    assert env.bot.send_rich_message.await_count == 1
    env.bot.send_message.assert_not_awaited()
    rich_menu.remember_live_menu.assert_not_awaited()
    assert env.open_sessions == 0


@pytest.mark.parametrize('second_fails', [False, True])
async def test_logo_is_removed_for_one_retry_only(env, monkeypatch, second_fails):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.test/logo.png')
    logo_error = TelegramBadRequest(method=None, message='failed to get HTTP URL content')
    env.bot.send_rich_message.side_effect = [
        logo_error,
        logo_error if second_fails else SimpleNamespace(message_id=110),
    ]
    await sender.send_current_main_menu(env.bot, 100)
    first, second = env.bot.send_rich_message.await_args_list
    assert first.kwargs['rich_message'].html.startswith('<img ')
    assert '<img ' not in second.kwargs['rich_message'].html
    assert env.bot.send_rich_message.await_count == 2
    assert rich_menu._resolve_rich_logo_url() == ''
    assert env.bot.send_message.await_count == int(second_fails)
    assert rich_menu.remember_live_menu.await_count == int(not second_fails)
    assert len(env.sessions) == 1


async def test_broadcast_video_snapshot_is_loaded_before_session_closes(env, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.test/logo.png')

    async def load_video(db):
        assert env.open_sessions == 1
        assert db is env.sessions[-1]
        return 'saved-video'

    monkeypatch.setattr(rich_menu, 'get_start_video_file_id', AsyncMock(side_effect=load_video))
    await sender.send_current_main_menu(env.bot, 100)

    request = env.bot.send_rich_message.await_args.kwargs['rich_message']
    assert request.html.startswith('<video src="tg://video?id=start_video"></video>')
    assert '<img ' not in request.html
    assert 'User 100' in request.html
    assert request.media[0].media.media == 'saved-video'
    assert rich_menu.remember_live_menu.await_args.kwargs['payload'].video_file_id == 'saved-video'
    assert env.open_sessions == 0
    env.bot.send_rich_message.assert_awaited_once()
    env.bot.send_message.assert_not_awaited()


async def test_rejected_broadcast_video_falls_back_once_without_disabling_logo(env, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.test/logo.png')
    monkeypatch.setattr(rich_menu, 'get_start_video_file_id', AsyncMock(return_value='rejected-video'))
    env.bot.send_rich_message.side_effect = TelegramBadRequest(method=None, message='MEDIA_EMPTY')

    await sender.send_current_main_menu(env.bot, 100)

    env.bot.send_rich_message.assert_awaited_once()
    env.bot.send_message.assert_awaited_once()
    rich_menu.remember_live_menu.assert_not_awaited()
    assert rich_menu._resolve_rich_logo_url() == 'https://example.test/logo.png'
    assert rich_menu.is_rich_menu_enabled() is True


async def test_classic_delivery_failure_propagates(env, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False)
    error = TelegramForbiddenError(method=None, message='blocked')
    env.bot.send_message.side_effect = error
    with pytest.raises(TelegramForbiddenError) as raised:
        await sender.send_current_main_menu(env.bot, 100)
    assert raised.value is error
    assert env.bot.send_message.await_count == 1
