"""Живое меню: фон перерисовывает последнее rich-меню, когда меняются видимые данные.

Ключ live_menu:{chat_id} ставит rich_menu.remember_live_menu, снимает нажатие на меню
(forget_live_menu_on_callback). Проход — только когда бот свободен: 15 мин, при нагрузке,
429 панели, лимите Telegram или ошибке интервал удваивается до 360.
"""

import asyncio
import json
import time
from bisect import bisect_right

import structlog
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import EditMessageText

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal, _pool_counters, engine
from app.external.remnawave_api import RemnaWaveAPI
from app.localization.texts import get_texts
from app.services.broadcast_service import broadcast_service
from app.utils.cache import cache
from app.utils.rich_menu import (
    LIVE_MENU_TTL,
    _apply_inline_buttons,
    _input_rich_message,
    _is_media_fetch_error,
    _is_rich_date_error,
    _looks_like_unsupported,
    _mark_logo_unavailable_once,
    _mark_rich_unavailable,
    _resolve_rich_logo_url,
    _strip_tg_time,
    build_main_menu_rich_html,
    forget_live_menu_state,
    is_rich_menu_enabled,
    live_menu_fingerprint,
)
from app.webserver.telegram import TelegramWebhookProcessor


logger = structlog.get_logger(__name__)

MIN_INTERVAL, MAX_INTERVAL = 15 * 60, 360 * 60
# Compact menus use canonical bot DB state; traffic is not displayed and is not
# polled or written by this service. Existing signed panel webhooks/panel-sync
# provide remote status. Bounds keep a production-size tracking set from turning
# one optional background pass into hours of DB/Telegram work.
MAX_MENUS_PER_PASS = 500
MAX_PASS_SECONDS = 120.0
LIVE_MENU_CURSOR_KEY = 'live_menu_cursor'
PAUSE, LAG_BUSY, LAG_ABORT = 0.2, 0.05, 0.25
# Новый снимок пишется, только если ключ не менялся с начала обработки (нажатие, новое меню).
_CAS = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL') end return false"
)


async def live_menu_loop(monitoring) -> None:
    interval = MIN_INTERVAL
    while monitoring.is_running:
        await asyncio.sleep(interval)
        if not (settings.MAIN_MENU_LIVE_ENABLED and is_rich_menu_enabled()) or monitoring.bot is None:
            interval = MIN_INTERVAL
            continue
        try:
            throttled = await refresh_live_menus(monitoring.bot)
        except Exception as error:
            logger.exception('Живое меню: ошибка прохода', error=str(error))
            throttled = True
        new_interval = min(interval * 2, MAX_INTERVAL) if throttled else MIN_INTERVAL
        if new_interval != interval:
            logger.info('Живое меню: интервал', minutes=new_interval // 60)
        interval = new_interval


async def _loop_lag() -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(PAUSE)
    return loop.time() - started - PAUSE


def _pressure() -> str | None:
    if not (settings.MAIN_MENU_LIVE_ENABLED and is_rich_menu_enabled()):
        return 'disabled'
    if RemnaWaveAPI._throttled_until > time.monotonic():
        return 'panel_429'
    if broadcast_service._tasks:  # идёт рассылка из кабинета: лимит Telegram отдаём ей
        return 'broadcast'
    processor = TelegramWebhookProcessor.active
    if processor and processor.is_running and processor._queue.qsize():  # воркеры вебхука заняты, апдейты ждут
        return 'webhook_queue'
    pool = _pool_counters(engine.pool)
    if pool and pool['checked_out'] >= max(1, settings.DATABASE_POOL_SIZE // 2):
        return 'db_pool'
    return None


async def _tracked_keys() -> list[str]:
    """Ключи живых меню через SCAN: KEYS блокирует Redis на обход всего пространства ключей,
    а в нём FSM-состояния, кэши и очереди всего бота."""
    if not cache._connected or cache.redis_client is None:
        return []
    keys = [
        key.decode() if isinstance(key, bytes) else key
        async for key in cache.redis_client.scan_iter(match='live_menu:*', count=500)
    ]
    return list(dict.fromkeys(keys))  # SCAN может вернуть ключ дважды


async def refresh_live_menus(bot, subscription_service=None) -> bool:
    """Один проход. True — бот/панель/Telegram заняты или была ошибка: следующий проход реже."""
    reason = _pressure()
    if reason:
        return reason != 'disabled'
    keys = await _tracked_keys()
    if not keys:
        return False
    # Redis continuation, independent of SCAN ordering: consider the next slice
    # after the last completed key, wrapping once. A removed key still provides
    # an ordering boundary. This avoids repeatedly sampling the same prefix.
    keys.sort()
    tracked_count = len(keys)
    cursor = await cache.get(LIVE_MENU_CURSOR_KEY)
    after = cursor.get('after', '') if isinstance(cursor, dict) else ''
    start = bisect_right(keys, after if isinstance(after, str) else '')
    keys = (keys[start:] + keys[:start])[:MAX_MENUS_PER_PASS]
    lag = sorted([await _loop_lag() for _ in range(5)])[2]  # медиана: один всплеск GC не решает
    reason = _pressure() or ('loop_lag' if lag > LAG_BUSY else None)
    if reason:
        logger.info('Живое меню: бот занят, проход пропущен', reason=reason, lag_ms=int(lag * 1000), tracked=len(keys))
        return reason != 'disabled'

    throttled = False
    edited = dropped = 0
    completed_key = None
    started = time.monotonic()
    for index, key in enumerate(keys):
        if time.monotonic() - started >= MAX_PASS_SECONDS:
            logger.info('Живое меню: бюджет прохода', done=index, sampled=len(keys), tracked=tracked_count)
            break
        if index:
            lag = await _loop_lag()  # пауза между пользователями (≤5 правок/с) и замер нагрузки
            reason = _pressure() or ('loop_lag' if lag > LAG_ABORT else None)
            if reason:
                logger.warning('Живое меню: проход прерван', reason=reason, done=index, tracked=len(keys))
                await _remember_progress(completed_key)
                return reason != 'disabled'
        try:
            result = await _refresh_one(bot, key)
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as error:
            logger.warning(
                'Живое меню: проход прерван', reason='telegram', error=str(error), done=index, tracked=len(keys)
            )
            await _remember_progress(completed_key)
            return True
        except Exception as error:
            logger.warning('Живое меню: не удалось обновить меню', key=key, error=str(error))
            throttled = True
            completed_key = key
            continue
        completed_key = key
        if result == 'unsupported':
            await _remember_progress(completed_key)
            return False
        edited += result == 'edited'
        dropped += result == 'dropped'
    await _remember_progress(completed_key)
    if edited or dropped:
        logger.info(
            'Живое меню: проход',
            tracked=tracked_count,
            sampled=len(keys),
            edited=edited,
            dropped=dropped,
            took_s=round(time.monotonic() - started, 1),
        )
    return throttled


async def _remember_progress(key: str | None) -> None:
    if key is not None:
        await cache.set(LIVE_MENU_CURSOR_KEY, {'after': key}, expire=LIVE_MENU_TTL)


async def _refresh_one(bot, key: str) -> str | None:
    raw = await cache.redis_client.get(key)
    if not raw:
        return None
    state = json.loads(raw)
    chat_id = int(key.rsplit(':', 1)[1])
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, chat_id)
        if user is None or getattr(user, 'status', 'active') != 'active':
            await forget_live_menu_state(key, raw)
            return 'dropped'
        texts = get_texts(user.language)
        from app.handlers.menu import build_main_menu_keyboard  # menu.py сам импортирует rich_menu

        # Compare the actual visible buttons too: balance may be hidden in the
        # compact body but visible in a configured button. No guessed field list.
        keyboard = await build_main_menu_keyboard(user, db)
        fp = live_menu_fingerprint(user, texts, keyboard)
        if fp == state.get('fp'):
            return None
        rich_html = await build_main_menu_rich_html(user, texts, db)
        language = user.language
    rich_html, keyboard = _apply_inline_buttons(rich_html, keyboard, for_edit=True)
    if await cache.redis_client.get(key) != raw:
        return None  # пока собирали, нажали кнопку на меню или пришло новое меню

    async def edit(html_: str) -> None:
        await bot(
            EditMessageText(
                chat_id=chat_id,
                message_id=state['m'],
                parse_mode=None,
                rich_message=_input_rich_message(html_, language),
                reply_markup=keyboard,
            )
        )

    try:
        try:
            await edit(rich_html)
        except TelegramBadRequest as error:
            if not _is_rich_date_error(error):
                raise
            await edit(_strip_tg_time(rich_html))
    except (TelegramBadRequest, TelegramNotFound, TelegramForbiddenError) as error:
        if 'message is not modified' not in str(error).lower():
            if _looks_like_unsupported(error):
                _mark_rich_unavailable(error)
                return 'unsupported'
            if _resolve_rich_logo_url() and _is_media_fetch_error(error):
                _mark_logo_unavailable_once(error)  # следующий проход уже без логотипа, ключ живёт
                return None
            await forget_live_menu_state(key, raw)  # удалено, заблокирован, нельзя править
            logger.info('Живое меню: меню больше не отслеживается', chat_id=chat_id, error=str(error)[:200])
            return 'dropped'
    except ClientDecodeError:
        pass  # правка дошла, aiogram не разобрал rich-ответ (см. except Exception в try_send_rich_main_menu)
    await cache.redis_client.eval(_CAS, 1, key, raw, json.dumps({'m': state['m'], 'fp': fp}))
    return 'edited'
