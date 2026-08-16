from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.middlewares.back_button import (
    BACK_BUTTON_CUSTOM_EMOJI_ID,
    BackButtonRequestMiddleware,
    normalize_back_buttons,
)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('⬅️ Назад', 'Назад'),
        ('◀️ Назад к тарифу', 'Назад'),
        ('⬅️ К списку', 'Назад'),
        ('⬅️ Back to menu', 'Back'),
        ('⬅️ قبلی', 'قبلی'),
        ('⬅️ بازگشت به پنل', 'قبلی'),
        ('⬅️返回列表', '返回'),
    ],
)
def test_normalize_back_buttons_for_all_supported_languages(text, expected):
    untouched = InlineKeyboardButton(text='Продолжить', callback_data='continue')
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data='go_back')], [untouched]]
    )

    normalized = normalize_back_buttons(markup)
    back_button = normalized.inline_keyboard[0][0]

    assert back_button.text == expected
    assert back_button.icon_custom_emoji_id == BACK_BUTTON_CUSTOM_EMOJI_ID
    assert back_button.callback_data == 'go_back'
    assert normalized.inline_keyboard[1][0] is untouched


async def test_request_middleware_normalizes_outgoing_keyboard():
    middleware = BackButtonRequestMiddleware()
    make_request = AsyncMock(return_value=MagicMock())
    bot = MagicMock()
    method = SendMessage(
        chat_id=1,
        text='test',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back')]]
        ),
    )

    await middleware(make_request, bot, method)

    sent_method = make_request.await_args.args[1]
    sent_button = sent_method.reply_markup.inline_keyboard[0][0]
    assert sent_button.text == 'Назад'
    assert sent_button.icon_custom_emoji_id == BACK_BUTTON_CUSTOM_EMOJI_ID
