"""Durable renewal intents: restart, failure, cancellation and concurrent workers."""

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import RenewalSyncTask
from app.services import renewal_sync_service as sync, subscription_renewal_service as renewal
from tests.integration.test_purchase_atomicity import (
    context,
    renew,
    seed_subscription,
    sessions,
    snapshot,
)


__all__ = ['context', 'sessions']  # Re-export shared pytest fixtures.


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]


async def queue_without_inline(sessions, context, sub_id, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(renewal, 'process_renewal_sync', AsyncMock(return_value=False))
        await renew(sessions, context, sub_id)


async def task_state(sessions, sub_id):
    async with sessions() as db:
        row = await db.get(RenewalSyncTask, sub_id)
        return {
            key: getattr(row, key)
            for key in ('version', 'status', 'attempts', 'last_error', 'reset_traffic', 'reset_devices', 'sync_squads')
        }


async def test_none_result_remains_pending_until_new_worker_succeeds(sessions, context):
    sub_id, _ = await seed_subscription(sessions, context)
    context.panel.update_remnawave_user.side_effect = None
    context.panel.update_remnawave_user.return_value = None
    await renew(sessions, context, sub_id)
    before = await snapshot(sessions)
    state = await task_state(sessions, sub_id)
    assert state['status'] == 'pending' and state['last_error'] == 'panel_returned_none'
    context.panel.update_remnawave_user.return_value = {'ok': True}
    # New DB sessions are the only state supplied to the restarted worker.
    await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    assert (await task_state(sessions, sub_id))['status'] == 'done'
    assert await snapshot(sessions) == before


async def test_timeout_preserves_intent_and_returns_promptly(sessions, context, monkeypatch):
    sub_id, _ = await seed_subscription(sessions, context)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(30)

    context.panel.update_remnawave_user.side_effect = stalled
    monkeypatch.setattr(sync, 'REMNAWAVE_SYNC_TIMEOUT', 0.02)
    monkeypatch.setattr(renewal, 'REMNAWAVE_SYNC_TIMEOUT', 1)
    async with asyncio.timeout(2):
        await renew(sessions, context, sub_id)
    state = await task_state(sessions, sub_id)
    assert state['status'] == 'pending' and state['last_error'] == 'TimeoutError'


async def test_retry_exhaustion_never_discards_paid_renewal(sessions, context):
    sub_id, _ = await seed_subscription(sessions, context)
    context.panel.update_remnawave_user.side_effect = None
    context.panel.update_remnawave_user.return_value = None
    await renew(sessions, context, sub_id)
    for _ in range(6):
        await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    state = await task_state(sessions, sub_id)
    assert state['status'] == 'pending' and state['attempts'] == 7
    assert len((await snapshot(sessions))['ledger']) == 1


async def test_two_workers_cannot_reset_same_subscription_concurrently(sessions, context, monkeypatch):
    sub_id, _ = await seed_subscription(sessions, context)
    await queue_without_inline(sessions, context, sub_id, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'ok': True}

    context.panel.update_remnawave_user.side_effect = blocked
    first = asyncio.create_task(sync.process_renewal_sync(sub_id, session_factory=sessions, force=True))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            assert await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True) is False
        context.panel.update_remnawave_user.assert_awaited_once()
    finally:
        release.set()
        await first
    assert (await task_state(sessions, sub_id))['status'] == 'done'


async def test_new_renewal_during_panel_call_is_not_acknowledged_by_old_worker(sessions, context, monkeypatch):
    sub_id, _ = await seed_subscription(sessions, context)
    await queue_without_inline(sessions, context, sub_id, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'ok': True}

    context.panel.update_remnawave_user.side_effect = blocked
    first = asyncio.create_task(sync.process_renewal_sync(sub_id, session_factory=sessions, force=True))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            await queue_without_inline(sessions, context, sub_id, monkeypatch)
    finally:
        release.set()
        await first
    state = await task_state(sessions, sub_id)
    assert state['version'] == 2 and state['status'] == 'pending'
    assert state['sync_squads'] is True
    await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    assert (await task_state(sessions, sub_id))['status'] == 'done'
    assert len((await snapshot(sessions))['ledger']) == 2


async def test_cancelled_worker_releases_lock_and_preserves_intent(sessions, context, monkeypatch):
    sub_id, _ = await seed_subscription(sessions, context)
    await queue_without_inline(sessions, context, sub_id, monkeypatch)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.sleep(30)

    context.panel.update_remnawave_user.side_effect = blocked
    task = asyncio.create_task(sync.process_renewal_sync(sub_id, session_factory=sessions, force=True))
    async with asyncio.timeout(2):
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await task_state(sessions, sub_id))['status'] == 'pending'
    context.panel.update_remnawave_user.side_effect = None
    context.panel.update_remnawave_user.return_value = {'ok': True}
    assert await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)


@pytest.mark.parametrize('reset_result', [False, True])
async def test_reset_intent_survives_until_all_requested_steps_succeed(
    sessions,
    context,
    monkeypatch,
    reset_result,
):
    sub_id, _ = await seed_subscription(sessions, context)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', True)
    await queue_without_inline(sessions, context, sub_id, monkeypatch)
    device_api = SimpleNamespace(
        reset_user_devices=AsyncMock(return_value=reset_result),
        reset_user_traffic=AsyncMock(return_value={'ok': True}),
    )

    @asynccontextmanager
    async def fake_api(_self):
        yield device_api

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService.get_api_client', fake_api)
    await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    kwargs = context.panel.update_remnawave_user.await_args.kwargs
    assert kwargs['reset_traffic'] is False and kwargs['sync_squads'] is True
    device_api.reset_user_traffic.assert_awaited_once_with('test-panel-user')
    device_api.reset_user_devices.assert_awaited_once_with('test-panel-user')
    state = await task_state(sessions, sub_id)
    assert state['status'] == ('done' if reset_result else 'pending')
    if not reset_result:
        assert state['reset_devices'] and state['reset_traffic']
        assert state['last_error'] == 'device_reset_failed'


@pytest.mark.parametrize('traffic_result', [None, False, 'exception', True])
async def test_traffic_reset_must_be_acknowledged_before_completing_intent(
    sessions,
    context,
    monkeypatch,
    traffic_result,
):
    sub_id, _ = await seed_subscription(sessions, context)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    await queue_without_inline(sessions, context, sub_id, monkeypatch)
    panel_api = SimpleNamespace(reset_user_traffic=AsyncMock(return_value=traffic_result))
    if traffic_result == 'exception':
        panel_api.reset_user_traffic.side_effect = ConnectionError('panel unavailable')

    @asynccontextmanager
    async def fake_api(_self):
        yield panel_api

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService.get_api_client', fake_api)
    before = await snapshot(sessions)
    await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    state = await task_state(sessions, sub_id)
    assert state['status'] == ('done' if traffic_result is True else 'pending')
    if traffic_result is not True:
        assert state['reset_traffic'] is True
        assert state['last_error'] == ('ConnectionError' if traffic_result == 'exception' else 'traffic_reset_failed')
        panel_api.reset_user_traffic.side_effect = None
        panel_api.reset_user_traffic.return_value = {'ok': True}
        await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
        assert (await task_state(sessions, sub_id))['status'] == 'done'
    assert await snapshot(sessions) == before
