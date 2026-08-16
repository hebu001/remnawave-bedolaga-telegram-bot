"""Normalize every inline back button before it is sent to Telegram."""

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.miniapp_buttons import strip_leading_emoji


BACK_BUTTON_CUSTOM_EMOJI_ID = '5258236805890710909'

_BACK_GLYPHS = ('⬅', '◀', '↩', '🔙')
_BACK_WORDS = (
    'назад',
    'back',
    'previous',
    'повернутися',
    'повернутись',
    '返回',
    'قبلی',
    'بازگشت',
)


def _canonical_back_text(text: str) -> str | None:
    """Return the localized short label when *text* is a back-navigation button."""
    raw_text = text.strip()
    cleaned_text = strip_leading_emoji(raw_text).strip()
    folded_text = cleaned_text.casefold()
    has_back_word = any(folded_text.startswith(word) for word in _BACK_WORDS)
    has_back_glyph = raw_text.startswith(_BACK_GLYPHS)

    if not has_back_word and not has_back_glyph:
        return None

    if any('\u4e00' <= char <= '\u9fff' for char in cleaned_text):
        return '返回'
    if any('\u0600' <= char <= '\u06ff' for char in cleaned_text):
        return 'قبلی'
    if any('a' <= char.casefold() <= 'z' for char in cleaned_text):
        return 'Back'
    return 'Назад'


def normalize_back_buttons(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Apply the common custom emoji and compact label to all back buttons."""
    changed = False
    rows: list[list[InlineKeyboardButton]] = []

    for row in markup.inline_keyboard:
        normalized_row: list[InlineKeyboardButton] = []
        for button in row:
            label = _canonical_back_text(button.text)
            if label is None:
                normalized_row.append(button)
                continue

            changed = True
            normalized_row.append(
                button.model_copy(
                    update={
                        'text': label,
                        'icon_custom_emoji_id': BACK_BUTTON_CUSTOM_EMOJI_ID,
                    }
                )
            )
        rows.append(normalized_row)

    if not changed:
        return markup
    return markup.model_copy(update={'inline_keyboard': rows})


class BackButtonRequestMiddleware(BaseRequestMiddleware):
    """Normalize inline back buttons in every outgoing Bot API request."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        reply_markup = getattr(method, 'reply_markup', None)
        if isinstance(reply_markup, InlineKeyboardMarkup):
            normalized_markup = normalize_back_buttons(reply_markup)
            if normalized_markup is not reply_markup:
                method = method.model_copy(update={'reply_markup': normalized_markup})

        return await make_request(bot, method)
