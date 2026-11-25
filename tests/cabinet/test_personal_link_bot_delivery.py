"""The Telegram menu carries no credentials; only the private button owner's DM receives a URL."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from fastapi import HTTPException

from app.cabinet.auth import personal_link
from app.cabinet.dependencies import get_primary_cabinet_user
from app.handlers import start
from app.keyboards.inline import _with_personal_link_button


def test_configured_menus_have_only_a_reusable_callback(monkeypatch):
    monkeypatch.setattr(personal_link, 'personal_link_enabled', lambda: True)
    source = types.InlineKeyboardMarkup(inline_keyboard=[])
    result = _with_personal_link_button(source, 'ru', is_staff=False)
    assert source.inline_keyboard == []
    button = result.inline_keyboard[0][0]
    assert button.callback_data == 'cabinet_personal_link' and button.url is None
    assert button.text == 'Сохранить доступ к боту'
    repeated = _with_personal_link_button(result, 'ru', is_staff=False)
    assert repeated == result
    assert (
        _with_personal_link_button(source, 'ru', is_staff=True).inline_keyboard[0][0].callback_data
        == 'cabinet_personal_link'
    )
    monkeypatch.setattr(personal_link, 'personal_link_enabled', lambda: False)
    assert _with_personal_link_button(source, 'ru', is_staff=False) is source
    assert _with_personal_link_button(repeated, 'ru', is_staff=False).inline_keyboard == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('chat_type', 'chat_id', 'deliver'), [('private', 101, True), ('private', 102, False), ('group', 101, False)]
)
async def test_bot_resolves_authenticated_caller_only_in_own_private_chat(monkeypatch, chat_type, chat_id, deliver):
    message = MagicMock(spec=types.Message)
    message.chat = SimpleNamespace(type=chat_type, id=chat_id)
    message.answer = AsyncMock()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=101), answer=AsyncMock())
    db = MagicMock()
    lookup = AsyncMock(return_value=SimpleNamespace(id=99, language='ru'))
    url = 'https://example.invalid/s/abc_def-ghijk1234567890'
    get_link = AsyncMock(return_value=url)
    monkeypatch.setattr(start, 'get_user_by_telegram_id', lookup)
    monkeypatch.setattr(personal_link, 'get_personal_link', get_link)
    await start.process_cabinet_personal_link(callback, db)
    if deliver:
        lookup.assert_awaited_once_with(db, 101)
        get_link.assert_awaited_once_with(db, 99, verified_telegram_id=101)
        args, kwargs = message.answer.await_args
        assert args[0] == (
            'Личная ссылка для входа. Любой, у кого она есть, сможет открыть ваш кабинет VPN. '
            f'Не пересылайте её.\n\n{url}'
        )
        assert kwargs['protect_content'] is False
        assert kwargs['parse_mode'] is None
        assert kwargs['link_preview_options'].is_disabled is True
        [row] = kwargs['reply_markup'].inline_keyboard
        [back] = row
        assert back.text == '⬅️ Назад'
        assert back.callback_data == 'back_to_menu'
        assert back.url is None and back.web_app is None
    else:
        lookup.assert_not_awaited()
        get_link.assert_not_awaited()
        message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('language', 'titles', 'expected'),
    [
        (
            'ru',
            ['Новости VPN'],
            'Чтобы получить ссылку на кабинет, подпишитесь на канал «Новости VPN» и попробуйте снова.',
        ),
        (
            'ru',
            ['Новости VPN', 'Обновления'],
            (
                'Чтобы получить ссылку на кабинет, подпишитесь на обязательные каналы, '
                'включая «Новости VPN», и попробуйте снова.'
            ),
        ),
        (
            'ru',
            ['  Новости\nVPN\u202e  '],
            'Чтобы получить ссылку на кабинет, подпишитесь на канал «Новости VPN» и попробуйте снова.',
        ),
        (
            'en',
            ['VPN News'],
            'To get your cabinet link, subscribe to the channel “VPN News” and try again.',
        ),
    ],
)
async def test_bot_explains_missing_channel_without_rechecking_or_issuing_link(monkeypatch, language, titles, expected):
    message = MagicMock(spec=types.Message)
    message.chat = SimpleNamespace(type='private', id=101)
    message.answer = AsyncMock()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=101), answer=AsyncMock())
    db = MagicMock()
    lookup = AsyncMock(return_value=SimpleNamespace(id=99, language=language))
    get_link = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail={
                'code': 'required_channel_subscription_missing',
                'channel_titles': titles,
                'url': 'https://example.invalid/s/do-not-display',
                'channel_id': 'do-not-display',
            },
        )
    )
    recheck = AsyncMock()
    monkeypatch.setattr(start, 'get_user_by_telegram_id', lookup)
    monkeypatch.setattr(personal_link, 'get_personal_link', get_link)
    monkeypatch.setattr(start.channel_subscription_service, 'get_channels_with_status', recheck)
    await start.process_cabinet_personal_link(callback, db)
    callback.answer.assert_awaited_once_with(expected, show_alert=True)
    lookup.assert_awaited_once_with(db, 101)
    get_link.assert_awaited_once_with(db, 99, verified_telegram_id=101)
    recheck.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.parametrize(
    'titles',
    [
        None,
        'not-a-list',
        [],
        [None, 123, ''],
        ['\n\u202e'],
        ['https://t.me/example'],
        ['t.me/example'],
        ['news.example.com'],
        ['@channel'],
        ['<b>News</b>'],
    ],
)
def test_channel_alert_has_safe_fallback_for_unavailable_or_unsafe_titles(titles):
    assert start._personal_link_channel_alert(start.get_texts('ru'), {'channel_titles': titles}) == (
        'Чтобы получить ссылку на кабинет, подпишитесь на обязательные каналы и попробуйте снова.'
    )


def test_channel_alert_fits_telegram_limit_with_long_unicode_title():
    alert = start._personal_link_channel_alert(start.get_texts('ru'), {'channel_titles': ['🔒' * 200, 'Обновления']})
    assert '🔒' * 40 + '…' in alert
    assert len(alert.encode('utf-16-le')) // 2 <= 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'detail'),
    [(503, 'Personal links are unavailable'), (403, {'code': 'other_reason', 'url': 'do-not-display'})],
)
async def test_bot_keeps_other_denials_generic(monkeypatch, status, detail):
    message = MagicMock(spec=types.Message)
    message.chat = SimpleNamespace(type='private', id=101)
    message.answer = AsyncMock()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=101), answer=AsyncMock())
    monkeypatch.setattr(start, 'get_user_by_telegram_id', AsyncMock(return_value=SimpleNamespace(id=99, language='ru')))
    monkeypatch.setattr(personal_link, 'get_personal_link', AsyncMock(side_effect=HTTPException(status, detail)))
    await start.process_cabinet_personal_link(callback, MagicMock())
    callback.answer.assert_awaited_once_with('Не удалось получить ссылку на кабинет.', show_alert=True)
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'allowed'), [('primary', True), ('personal_link', False), ('guest', False), (None, False)]
)
async def test_only_proven_primary_login_can_manage_the_link(method, allowed):
    request = SimpleNamespace(state=SimpleNamespace(cabinet_auth_method=method))
    user = SimpleNamespace()
    if allowed:
        assert await get_primary_cabinet_user(request, user) is user
    else:
        with pytest.raises(HTTPException) as denied:
            await get_primary_cabinet_user(request, user)
        assert denied.value.status_code == 403
