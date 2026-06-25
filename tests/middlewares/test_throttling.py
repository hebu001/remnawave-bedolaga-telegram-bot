"""Tests for ThrottlingMiddleware.

Covers the hardening added after the 2026-04-05 multi-account /start flood:
1. reply to a blocked /start at most once per window (no outbound amplification);
2. global /start ceiling across all users (anti-DoS);
3. auto temp-ban that silently ignores a repeat flooder;
4. the `/start@BotName` and `/start<invisible-char>` bypass fix via normalize_command.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.middlewares import throttling as thr  # noqa: E402


def make_message(text: str | None, user_id: int = 1000) -> MagicMock:
    """A Message mock that passes isinstance(event, Message) and records .answer()."""
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.answer = AsyncMock()
    return msg


@pytest.fixture(autouse=True)
def _non_admin():
    # Default: nobody is an admin (so the limits actually apply).
    with patch.object(thr.settings, 'is_admin', return_value=False):
        yield


class TestNormalizeCommand:
    @pytest.mark.parametrize(
        'text,expected',
        [
            ('/start', 'start'),
            ('/start 3838', 'start'),
            ('/start@EvoVPN_bot', 'start'),       # @mention bypass closed
            ('/start@EvoVPN_bot ref1', 'start'),
            ('/start ㅤ', 'start'),           # the 2026-04-05 attack form
            ('/startㅤ', 'start'),            # filler attached directly
            ('/​start', 'start'),            # zero-width inside
            ('/START', 'start'),                  # case-insensitive
            ('/help', 'help'),
            ('hello', ''),
            ('', ''),
            (None, ''),
        ],
    )
    def test_normalize(self, text, expected):
        assert thr.normalize_command(text) == expected


class TestStartBurst:
    @pytest.mark.asyncio
    async def test_at_mention_is_rate_limited(self):
        """`/start@Bot` used to skip the burst limit while still running cmd_start."""
        mw = thr.ThrottlingMiddleware(rate_limit=0, start_max_calls=3)
        handler = AsyncMock(return_value='ok')

        for _ in range(3):
            assert await mw(handler, make_message('/start@SomeBot'), {}) == 'ok'
        assert handler.await_count == 3

        blocked = make_message('/start@SomeBot')
        assert await mw(handler, blocked, {}) is None
        assert handler.await_count == 3          # 4th did not reach the handler
        blocked.answer.assert_awaited_once()     # and the user got a notice

    @pytest.mark.asyncio
    async def test_reply_at_most_once_per_window(self):
        """The core fix: a sustained flood must NOT trigger a reply per message."""
        mw = thr.ThrottlingMiddleware(rate_limit=0, start_max_calls=3, penalty_after_blocks=10_000)
        handler = AsyncMock()

        for _ in range(3):
            await mw(handler, make_message('/start'), {})

        replies = 0
        for _ in range(50):
            msg = make_message('/start')
            await mw(handler, msg, {})
            replies += msg.answer.await_count
        assert replies == 1


class TestAutoTempBan:
    @pytest.mark.asyncio
    async def test_repeat_flooder_is_silenced(self):
        mw = thr.ThrottlingMiddleware(
            rate_limit=0, start_max_calls=3, penalty_after_blocks=5, penalty_cooldown=300
        )
        handler = AsyncMock()
        uid = 7777

        # 3 allowed + 5 blocked attempts -> escalates to a silent temp-ban
        for _ in range(3 + 5):
            await mw(handler, make_message('/start', uid), {})
        assert uid in mw.penalty_until

        handler.reset_mock()
        # While banned, EVERY event from the user is dropped silently.
        for text in ['/start', 'hello there', '/help']:
            msg = make_message(text, uid)
            assert await mw(handler, msg, {}) is None
            msg.answer.assert_not_awaited()
        handler.assert_not_awaited()


class TestGlobalCeiling:
    @pytest.mark.asyncio
    async def test_excess_dropped_across_users(self):
        """Distributed flood: many accounts each under the per-user limit."""
        mw = thr.ThrottlingMiddleware(
            rate_limit=0, start_max_calls=100, start_global_max_calls=10, penalty_after_blocks=10_000
        )
        handler = AsyncMock()

        for uid in range(1, 11):
            assert await mw(handler, make_message('/start', uid), {}) is not None
        assert handler.await_count == 10

        # 11th distinct account hits the global ceiling -> silent drop.
        msg = make_message('/start', 9999)
        assert await mw(handler, msg, {}) is None
        msg.answer.assert_not_awaited()
        assert handler.await_count == 10


class TestExemptions:
    @pytest.mark.asyncio
    async def test_admin_not_throttled(self):
        with patch.object(thr.settings, 'is_admin', return_value=True):
            mw = thr.ThrottlingMiddleware(rate_limit=0, start_max_calls=3)
            handler = AsyncMock(return_value='ok')
            for _ in range(10):
                assert await mw(handler, make_message('/start', 555), {}) == 'ok'
            assert handler.await_count == 10

    @pytest.mark.asyncio
    async def test_plain_message_passes_through(self):
        mw = thr.ThrottlingMiddleware(rate_limit=0)
        handler = AsyncMock(return_value='X')
        assert await mw(handler, make_message('just a message'), {}) == 'X'
        handler.assert_awaited_once()
