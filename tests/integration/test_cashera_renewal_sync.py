"""Real PostgreSQL: Cashera recurring money, replay and durable panel intent."""

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud import transaction as transaction_crud
from app.database.models import CasheraSubscription, RenewalSyncTask
from app.services import renewal_sync_service as sync
from app.services.payment import cashera as cashera_module
from tests.integration.test_purchase_atomicity import context, seed_subscription, sessions, snapshot
from tests.integration.test_renewal_sync_recovery import task_state
from tests.services.test_cashera_recurrent import _charge_event


__all__ = ['context', 'sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs owned local PostgreSQL'),
]


async def binding(sessions, context, monkeypatch, *, status='active'):
    sub_id, end = await seed_subscription(sessions, context, status=status)
    async with sessions() as db:
        record = CasheraSubscription(
            user_id=context.user_id,
            subscription_id=sub_id,
            tariff_id=context.tariff_id,
            cashera_subscription_uuid='sub-1',
            external_id='synthetic-cashera-binding',
            interval='monthly',
            charge_days=30,
            amount_kopeks=30000,
        )
        db.add(record)
        await db.commit()
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr(cashera_module._CasheraRecurrentAgent, '_notify_cashera_recurring', AsyncMock())
    return sub_id, end


async def charge(sessions, charge_id='synthetic-charge'):
    async with sessions() as db:
        return await cashera_module._CasheraRecurrentAgent().process_cashera_webhook(db, _charge_event(charge_id))


@pytest.mark.parametrize('status', ['active', 'expired'])
@pytest.mark.parametrize('reset', [False, True])
async def test_charge_queues_selected_paid_reset_policy_before_commit(sessions, context, monkeypatch, status, reset):
    sub_id, before = await binding(sessions, context, monkeypatch, status=status)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', True)
    # Simulate a crash immediately after the commit, before any panel delivery.
    monkeypatch.setattr(cashera_module, 'process_renewal_sync', AsyncMock(side_effect=RuntimeError('worker stopped')))
    assert await charge(sessions)
    state = await task_state(sessions, sub_id)
    assert state['status'] == 'pending' and state['version'] == 1 and state['attempts'] == 0
    assert state['reset_traffic'] is reset and state['reset_devices'] and state['sync_squads']
    committed = await snapshot(sessions)
    assert committed['users'][0][1] == 50000
    assert committed['ledger'] == [(context.user_id, -30000, 'subscription_payment')]
    if status == 'active':
        assert committed['subs'][0][1] == before + timedelta(days=30)
    assert await charge(sessions)
    assert await snapshot(sessions) == committed
    assert await task_state(sessions, sub_id) == state


async def test_failed_commit_rolls_back_charge_subscription_and_intent_then_replay_succeeds(
    sessions,
    context,
    monkeypatch,
):
    sub_id, _ = await binding(sessions, context, monkeypatch)
    monkeypatch.setattr(cashera_module, 'process_renewal_sync', AsyncMock(return_value=False))
    before = await snapshot(sessions)
    async with sessions() as db:
        with monkeypatch.context() as patch:
            patch.setattr(db, 'commit', AsyncMock(side_effect=RuntimeError('synthetic commit failure')))
            assert not await cashera_module._CasheraRecurrentAgent().process_cashera_webhook(
                db, _charge_event('replay-after-commit-failure')
            )
        await db.rollback()
    assert await snapshot(sessions) == before
    async with sessions() as db:
        assert await db.get(RenewalSyncTask, sub_id) is None
        record = await db.scalar(select(CasheraSubscription))
        assert record.charges_success == 0 and record.last_charge_external_id is None
    assert await charge(sessions, 'replay-after-commit-failure')
    assert (await task_state(sessions, sub_id))['version'] == 1
    assert len((await snapshot(sessions))['ledger']) == 1


async def test_panel_failure_survives_duplicate_webhook_and_new_worker_retry(sessions, context, monkeypatch):
    sub_id, _ = await binding(sessions, context, monkeypatch)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    context.panel.update_remnawave_user.side_effect = ConnectionError('synthetic panel outage')
    assert await charge(sessions)
    state = await task_state(sessions, sub_id)
    assert state['status'] == 'pending' and state['last_error'] == 'ConnectionError' and state['attempts'] == 1
    paid = await snapshot(sessions)
    assert await charge(sessions)
    assert await task_state(sessions, sub_id) == state
    context.panel.update_remnawave_user.side_effect = None
    context.panel.update_remnawave_user.return_value = {'synced': True}
    assert await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    assert (await task_state(sessions, sub_id))['status'] == 'done'
    assert await snapshot(sessions) == paid


async def test_concurrent_duplicate_charge_has_one_extension_ledger_and_intent(sessions, context, monkeypatch):
    sub_id, before = await binding(sessions, context, monkeypatch)
    monkeypatch.setattr(cashera_module, 'process_renewal_sync', AsyncMock(return_value=False))
    assert all(await asyncio.gather(charge(sessions, 'same-charge'), charge(sessions, 'same-charge')))
    state = await snapshot(sessions)
    assert state['subs'][0][1] == before + timedelta(days=30)
    assert len(state['ledger']) == 1
    assert (await task_state(sessions, sub_id))['version'] == 1


async def test_new_cashera_charge_during_panel_sync_remains_pending(sessions, context, monkeypatch):
    sub_id, _ = await binding(sessions, context, monkeypatch)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(cashera_module, 'process_renewal_sync', AsyncMock(return_value=False))
    assert await charge(sessions, 'first-charge')
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'synced': True}

    context.panel.update_remnawave_user.side_effect = blocked
    first = asyncio.create_task(sync.process_renewal_sync(sub_id, session_factory=sessions, force=True))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            assert await charge(sessions, 'second-charge')
    finally:
        release.set()
        await first
    state = await task_state(sessions, sub_id)
    assert state['version'] == 2 and state['status'] == 'pending' and state['sync_squads']
    assert len((await snapshot(sessions))['ledger']) == 2
    assert await sync.process_renewal_sync(sub_id, session_factory=sessions, force=True)
    assert (await task_state(sessions, sub_id))['status'] == 'done'
