import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings


logger = structlog.get_logger(__name__)


# Invisible / zero-width characters sometimes appended to a command (e.g. "/start ㅤ")
# so a naive `text == '/start'` check fails while Telegram still routes it as /start.
# U+3164 (Hangul Filler) was used in the 2026-04-05 multi-account /start flood.
_INVISIBLE_CHARS = (
    '\u200b'  # zero-width space
    '‌'  # zero-width non-joiner
    '‍'  # zero-width joiner
    '⁠'  # word joiner
    '﻿'  # zero-width no-break space (BOM)
    '­'  # soft hyphen
    'ㅤ'  # hangul filler
    'ᅟ'  # hangul choseong filler
    'ᅠ'  # hangul jungseong filler
    '᠎'  # mongolian vowel separator
)
_INVISIBLE_TABLE = {ord(ch): None for ch in _INVISIBLE_CHARS}


def normalize_command(text: str | None) -> str:
    """Return the bare command for a message's first token, or '' if it isn't a command.

    Strips the leading slash, any ``@mention`` suffix and invisible/zero-width chars,
    then lowercases — so the throttle sees the SAME command aiogram's ``Command`` filter
    will dispatch. This closes the ``/start@BotName`` and ``/start<filler>`` bypasses
    where the burst-limit was skipped (raw text != '/start') but ``cmd_start`` still ran.
    """
    if not text:
        return ''
    parts = text.translate(_INVISIBLE_TABLE).split(maxsplit=1)
    if not parts:
        return ''
    first = parts[0]
    if not first.startswith('/'):
        return ''
    return first[1:].partition('@')[0].lower()


class ThrottlingMiddleware(BaseMiddleware):
    """
    Многоуровневый rate-limiter:
    1. Общий троттлинг — 0.5 сек между любыми сообщениями (UX).
    2. /start burst-лимит — макс ``start_max_calls`` вызовов за окно на пользователя.
    3. Глобальный /start-потолок — анти-DoS: лимит вызовов за окно по всем пользователям.
    4. Авто-изолятор — после ``penalty_after_blocks`` подряд заблокированных /start
       пользователь молча игнорируется ``penalty_cooldown`` секунд. Ключевое: на
       заблокированный флуд бот НЕ отвечает на каждое сообщение (раньше это
       превращало входящий флуд в исходящий и клало бота — инцидент 2026-04-05).

    Админы (``settings.is_admin``) освобождены от п.2–4.

    NOTE: Assumes single-process, single-event-loop execution.
    For multi-worker deployments, replace with Redis-based rate limiting.
    """

    def __init__(
        self,
        rate_limit: float = 0.5,
        start_max_calls: int = 3,
        start_window: float = 60.0,
        start_global_max_calls: int = 100,
        penalty_after_blocks: int = 5,
        penalty_cooldown: float = 300.0,
    ):
        self.rate_limit = rate_limit
        self.user_buckets: dict[int, float] = {}

        # /start anti-spam: sliding window per user
        self.start_max_calls = start_max_calls
        self.start_window = start_window
        self.start_buckets: dict[int, list[float]] = {}

        # /start anti-DoS: global sliding window across ALL users
        self.start_global_max_calls = start_global_max_calls
        self.start_global: list[float] = []
        self._global_logged_at: float = 0.0

        # Escalation: count consecutive blocked /start per user, then silently
        # ignore the user for a cooldown instead of replying to every attempt.
        self.penalty_after_blocks = penalty_after_blocks
        self.penalty_cooldown = penalty_cooldown
        self.start_block_counts: dict[int, int] = {}
        self.start_notice_at: dict[int, float] = {}
        self.penalty_until: dict[int, float] = {}

        self._last_cleanup: float = time.monotonic()
        self._cleanup_interval: float = 30.0

    def _maybe_cleanup(self, now: float) -> None:
        """Periodic cleanup of stale entries. Runs at most once per _cleanup_interval."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cleanup_threshold = now - 60
        self.user_buckets = {uid: ts for uid, ts in self.user_buckets.items() if ts > cleanup_threshold}
        self.start_buckets = {
            uid: [ts for ts in tss if now - ts < self.start_window]
            for uid, tss in self.start_buckets.items()
            if any(now - ts < self.start_window for ts in tss)
        }
        self.start_global = [ts for ts in self.start_global if now - ts < self.start_window]
        self.penalty_until = {uid: until for uid, until in self.penalty_until.items() if until > now}
        self.start_block_counts = {uid: c for uid, c in self.start_block_counts.items() if uid in self.start_buckets}
        self.start_notice_at = {uid: ts for uid, ts in self.start_notice_at.items() if now - ts < self.start_window}

    def _handle_start(self, event: Message, user_id: int, now: float) -> Awaitable[None] | bool:
        """Apply the /start rate-limits. Returns True to let the message through,
        or a coroutine (to await) that has already produced the blocked response.
        """
        # 3. Global anti-DoS ceiling — counts every /start attempt that reaches here
        # (penalised users are dropped earlier, so they don't inflate this).
        self.start_global = [ts for ts in self.start_global if now - ts < self.start_window]
        if len(self.start_global) >= self.start_global_max_calls:
            if now - self._global_logged_at >= self.start_window:
                self._global_logged_at = now
                logger.warning(
                    'Rate-limit /start GLOBAL ceiling hit — dropping excess',
                    global_count=len(self.start_global),
                    max_calls=self.start_global_max_calls,
                    window_sec=int(self.start_window),
                )
            return self._noop()  # silent drop, do not append (list drains as it ages)

        # 2. Per-user burst window
        timestamps = [ts for ts in self.start_buckets.get(user_id, []) if now - ts < self.start_window]
        if len(timestamps) >= self.start_max_calls:
            self.start_buckets[user_id] = timestamps
            return self._block_start(event, user_id, now, timestamps)

        # Allowed: record (per-user + global) and reset escalation
        timestamps.append(now)
        self.start_buckets[user_id] = timestamps
        self.start_global.append(now)
        self.start_block_counts.pop(user_id, None)
        return True

    async def _block_start(self, event: Message, user_id: int, now: float, timestamps: list[float]) -> None:
        """Over the per-user burst budget. Escalate to a silent ban after repeated
        blocks; otherwise notify at most once per window (no per-message amplification).
        """
        blocked = self.start_block_counts.get(user_id, 0) + 1
        self.start_block_counts[user_id] = blocked

        # 4. Escalate to a silent temp-ban after repeated blocks
        if blocked >= self.penalty_after_blocks:
            self.penalty_until[user_id] = now + self.penalty_cooldown
            self.start_block_counts.pop(user_id, None)
            logger.warning(
                'Auto temp-ban: repeated /start flood',
                user_id=user_id,
                blocked_attempts=blocked,
                cooldown_sec=int(self.penalty_cooldown),
            )
            return

        # Notify at most once per window — NOT on every blocked message
        last_notice = self.start_notice_at.get(user_id, 0.0)
        if now - last_notice >= self.start_window:
            self.start_notice_at[user_id] = now
            cooldown = max(1, int(self.start_window - (now - timestamps[0])) + 1)
            logger.warning(
                'Rate-limit /start burst exceeded',
                user_id=user_id,
                call_count=len(timestamps),
                window_sec=int(self.start_window),
                max_calls=self.start_max_calls,
            )
            try:
                await event.answer(f'⏳ Слишком много запросов. Попробуйте через {cooldown} сек.')
            except TelegramAPIError:
                pass

    @staticmethod
    async def _noop() -> None:
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id if event.from_user else None

        if not user_id:
            return await handler(event, data)

        now = time.monotonic()

        # Always run cleanup (independent of throttle path)
        self._maybe_cleanup(now)

        is_admin = settings.is_admin(user_id)

        # --- Авто-изолятор: молча игнорируем нарушителя на время cooldown ---
        # Без ответа => без исходящей амплификации, которая клала бота.
        if not is_admin:
            penalty_until = self.penalty_until.get(user_id)
            if penalty_until is not None and now < penalty_until:
                return None

        # --- /start burst + global rate-limit ---
        if isinstance(event, Message) and normalize_command(event.text) == 'start' and not is_admin:
            outcome = self._handle_start(event, user_id, now)
            if outcome is not True:
                await outcome
                return None

        # --- Общий троттлинг (0.5 сек) ---
        last_call = self.user_buckets.get(user_id, 0)

        if now - last_call < self.rate_limit:
            logger.debug('Throttling user', user_id=user_id)

            # Для сообщений: молчим только если это состояние работы с тикетами; иначе показываем блок
            if isinstance(event, Message):
                try:
                    fsm: FSMContext | None = data.get('state')
                    current = await fsm.get_state() if fsm else None
                except Exception:
                    current = None
                if current:
                    state_str = str(current)
                    is_ticket_state = (':waiting_for_message' in state_str or ':waiting_for_reply' in state_str) and (
                        'TicketStates' in state_str or 'AdminTicketStates' in state_str
                    )
                    if is_ticket_state:
                        return None
                try:
                    await event.answer('⏳ Пожалуйста, не отправляйте сообщения так часто!')
                except TelegramAPIError:
                    pass
                return None
            # Для callback допустим краткое уведомление
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer('⏳ Слишком быстро! Подождите немного.', show_alert=True)
                except TelegramAPIError:
                    pass
                return None

        self.user_buckets[user_id] = now

        return await handler(event, data)
