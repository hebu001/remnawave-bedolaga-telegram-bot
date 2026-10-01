"""Upstream notification callers share the continuously validated ticket sessions."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet import ws_manager
from app.cabinet.routes import websocket as ws_routes
from app.config import settings
from app.services.cashera_recurring_cancel import notify_cashera_recurring


@pytest.fixture
def manager_env(monkeypatch):
    user = SimpleNamespace(id=7, status='active', cabinet_auth_version=0)
    db = SimpleNamespace(scalar=AsyncMock(return_value=user))

    @asynccontextmanager
    async def sessions():
        yield db

    permissions = AsyncMock(return_value=(False, 'ordinary user'))
    monkeypatch.setattr(ws_manager.PermissionService, 'check_permission', permissions)
    manager = ws_manager.CabinetConnectionManager(session_factory=sessions)
    payload = {'sub': '7', 'exp': time.time() + 60, 'auth_version': 0}
    return SimpleNamespace(user=user, db=db, permissions=permissions, manager=manager, payload=payload)


def test_route_reexports_same_session_manager_and_singleton():
    assert ws_routes.cabinet_ws_manager is ws_manager.cabinet_ws_manager
    assert ws_routes.CabinetConnectionManager is ws_manager.CabinetConnectionManager
    assert ws_routes.CabinetWsSession is ws_manager.CabinetWsSession


async def test_cashera_sender_reaches_shared_route_session_and_rechecks_revocation(manager_env, monkeypatch):
    env = manager_env
    # Preserve singleton identity: all service imports use this exact instance.
    shared = ws_manager.cabinet_ws_manager
    assert not shared._sessions
    monkeypatch.setattr(shared, '_session_factory', env.manager._session_factory)
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    socket = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    session = ws_routes.CabinetWsSession(socket, env.payload)
    assert await shared.connect(session)
    record = SimpleNamespace(user_id=7, status='ACTIVE', amount_kopeks=100_00, next_charge_at=None, subscription_id=11)
    try:
        await notify_cashera_recurring(AsyncMock(), record, 'activated')
        socket.send_text.assert_awaited_once()
        message = json.loads(socket.send_text.await_args.args[0])
        assert message['type'] == 'cashera_recurring.activated'
        assert message['subscription_id'] == 11
        env.user.cabinet_auth_version = 1
        await notify_cashera_recurring(AsyncMock(), record, 'confirmed')
        assert socket.send_text.await_count == 1, 'revoked session receives no second event'
        socket.close.assert_awaited_once()
        assert session not in shared._sessions
    finally:
        await shared.disconnect(session)


async def test_admin_role_downgrade_and_expiry_deny_sends(manager_env):
    env = manager_env
    env.permissions.return_value = (True, 'admin')
    socket = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    session = ws_manager.CabinetWsSession(socket, env.payload, client_ip='127.0.0.1')
    assert await env.manager.validate(session)
    assert await env.manager.connect(session)
    env.permissions.return_value = (False, 'role removed')
    await env.manager.send_to_admins({'type': 'ticket.private'})
    socket.send_text.assert_not_awaited()
    socket.close.assert_awaited_once()
    assert session not in env.manager._sessions
    assert env.permissions.await_args.kwargs['ip_address'] == '127.0.0.1'

    expired = ws_manager.CabinetWsSession(socket, {**env.payload, 'exp': time.time() - 1})
    assert await env.manager.connect(expired)
    assert not await env.manager.send(expired, {'type': 'ordinary'})
    socket.send_text.assert_not_awaited()
    assert expired not in env.manager._sessions


async def test_send_deadline_is_bounded_and_does_not_block_other_recipient(manager_env, monkeypatch):
    env = manager_env
    monkeypatch.setattr(ws_manager, 'SEND_TIMEOUT_SECONDS', 0.02)

    async def stalled(data):
        await asyncio.sleep(60)

    fast = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    slow = SimpleNamespace(send_text=AsyncMock(side_effect=stalled), close=AsyncMock())
    for socket in (fast, slow):
        assert await env.manager.connect(ws_manager.CabinetWsSession(socket, env.payload))
    async with asyncio.timeout(0.5):
        await env.manager.send_to_user(7, {'type': 'test'})
    fast.send_text.assert_awaited_once()
    slow.close.assert_awaited_once()
    assert len(env.manager._sessions) == 1


async def test_connection_limit_remains_atomic(manager_env, monkeypatch):
    env = manager_env
    monkeypatch.setattr(ws_manager, 'MAX_CONNECTIONS_PER_USER', 1)
    attempts = [ws_manager.CabinetWsSession(SimpleNamespace(), env.payload) for _ in range(4)]
    results = await asyncio.gather(*(env.manager.connect(session) for session in attempts))
    assert results.count(True) == 1
    assert len(env.manager._sessions) == 1
