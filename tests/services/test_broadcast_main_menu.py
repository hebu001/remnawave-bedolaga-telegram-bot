"""Menu broadcasts use per-recipient rendering and existing delivery accounting."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from app.services.broadcast_service import BroadcastConfig, BroadcastService


def menu_config(**kwargs):
    return BroadcastConfig(target='all', message_text='', selected_buttons=[], message_type='main_menu', **kwargs)


@pytest.mark.parametrize(
    'field,value',
    [('message_text', 'unexpected'), ('selected_buttons', ['home']), ('custom_buttons', [{'text': 'bad'}])],
)
def test_menu_rejects_unexpected_content(field, value):
    values = dict(target='all', message_text='', selected_buttons=[], message_type='main_menu')
    values[field] = value
    with pytest.raises(ValueError):
        BroadcastConfig(**values)


async def test_menu_deliver_limits_concurrent_personalization(monkeypatch):
    current = maximum = 0
    ids = []

    async def send(bot, telegram_id):
        nonlocal current, maximum
        current += 1
        maximum = max(current, maximum)
        ids.append(telegram_id)
        await asyncio.sleep(0)
        current -= 1

    monkeypatch.setattr('app.services.main_menu_broadcast_service.send_current_main_menu', send)
    service = BroadcastService()
    bot = AsyncMock()
    service.set_bot(bot)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)
    await service._send_batched(1, list(range(20)), menu_config(), None, asyncio.Event())
    assert sorted(ids) == list(range(20))
    assert maximum <= 5
    bot.send_message.assert_not_awaited()


async def test_menu_broadcast_counts_blocked_and_does_not_retry_ambiguous_delivery(monkeypatch):
    service = BroadcastService()
    calls = []

    async def deliver(telegram_id, config, keyboard):
        calls.append(telegram_id)
        if telegram_id == 2:
            raise TelegramForbiddenError(method=None, message='blocked')
        if telegram_id == 3:
            raise TelegramNetworkError(method=None, message='connection lost')

    monkeypatch.setattr(service, '_deliver_message', deliver)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)
    assert await service._send_batched(1, [1, 2, 3], menu_config(), None, asyncio.Event()) == (1, 1, 1, False)
    assert sorted(calls) == [1, 2, 3]


async def test_menu_broadcast_honors_stop_before_sending(monkeypatch):
    service = BroadcastService()
    deliver = AsyncMock()
    monkeypatch.setattr(service, '_deliver_message', deliver)
    monkeypatch.setattr(service, '_mark_cancelled', AsyncMock())
    cancel = asyncio.Event()
    cancel.set()
    assert await service._send_batched(1, [1, 2], menu_config(), None, cancel) == (0, 0, 0, True)
    deliver.assert_not_awaited()


async def test_menu_broadcast_retries_explicit_flood_wait(monkeypatch):
    service = BroadcastService()
    deliver = AsyncMock(side_effect=[TelegramRetryAfter(method=None, message='retry', retry_after=0), None])
    monkeypatch.setattr(service, '_deliver_message', deliver)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)
    assert await service._send_batched(1, [1], menu_config(), None, asyncio.Event()) == (1, 0, 0, False)
    assert deliver.await_count == 2


async def test_stop_prevents_sending_menu_to_queued_recipients(monkeypatch):
    service = BroadcastService()
    cancel = asyncio.Event()
    calls = []

    async def deliver(telegram_id, config, keyboard):
        calls.append(telegram_id)
        if len(calls) == 5:
            cancel.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_deliver_message', deliver)
    monkeypatch.setattr(service, '_mark_cancelled', AsyncMock())
    assert await service._send_batched(1, list(range(25)), menu_config(), None, cancel) == (5, 0, 0, True)
    assert calls == list(range(5))


async def test_menu_decode_error_does_not_log_personal_response(monkeypatch):
    service = BroadcastService()
    secret = 'private-subscription-link'
    deliver = AsyncMock(side_effect=RuntimeError(secret))
    warning = Mock()
    monkeypatch.setattr(service, '_deliver_message', deliver)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    monkeypatch.setattr('app.services.broadcast_service.logger.warning', warning)
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)
    assert await service._send_batched(1, [1], menu_config(), None, asyncio.Event()) == (0, 1, 0, False)
    assert secret not in str(warning.call_args_list)
    deliver.assert_awaited_once()
