"""Send a fresh personal menu without running registration or /start handlers."""

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound

from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.database.models import UserStatus
from app.localization.texts import get_texts
from app.utils import rich_menu


async def send_current_main_menu(bot: Bot, telegram_id: int) -> None:
    """Send one menu, leaving delivery failures to the broadcast retry/accounting loop.

    All database reads finish before Telegram I/O. In particular, this does not
    update activity, run pending /start payloads, or create a subscription.
    """
    # menu.py imports rich_menu itself; defer the handler import to avoid cycles.
    from app.handlers.menu import build_main_menu_keyboard, get_main_menu_text

    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, telegram_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise ValueError('Main menu recipient is missing or inactive')

        texts = get_texts(user.language)
        language = user.language
        keyboard = await build_main_menu_keyboard(user, db)
        fallback_text = await get_main_menu_text(user, texts, db)
        payload = (
            await rich_menu.build_main_menu_rich_payload(user, texts, db) if rich_menu.is_rich_menu_enabled() else None
        )

    if payload is not None:
        # A definitive logo rejection can be retried without the already-rendered
        # leading image. Do not reopen a session or re-run registration to do so.
        without_logo = payload.without_logo()
        for _attempt in range(2):
            try:
                sent = await rich_menu._send_rich_menu(bot, telegram_id, payload, keyboard, language)
            except (TelegramBadRequest, TelegramNotFound) as error:
                if rich_menu._looks_like_unsupported(error):
                    rich_menu._mark_rich_unavailable(error)
                elif rich_menu._is_media_fetch_error(error) and payload != without_logo:
                    # Another recipient may already have disabled the logo. This
                    # specific rejected message still contained it and may retry.
                    rich_menu._mark_logo_unavailable_once(error)
                    payload = without_logo
                    continue
                break
            else:
                await rich_menu.remember_live_menu(
                    telegram_id, getattr(sent, 'message_id', None), user, texts, keyboard, payload=payload
                )
                return

    # Only a definitive rich rejection reaches this fallback. Forbidden,
    # FloodWait, transport and decode errors propagate, avoiding an extra send
    # here when Telegram may already have delivered the rich message.
    await bot.send_message(
        chat_id=telegram_id,
        text=fallback_text,
        reply_markup=keyboard,
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
