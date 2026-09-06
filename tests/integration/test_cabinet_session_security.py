"""PostgreSQL/ASGI regressions for password revocation and one-use WS tickets."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from app.cabinet import dependencies
from app.cabinet.auth import jwt_handler, password_utils, session_security, ws_tickets
from app.cabinet.routes import auth, support_ws, websocket as ws_routes
from app.cabinet.schemas.auth import AutoLoginRequest, EmailLoginRequest, PasswordResetRequest, RefreshTokenRequest
from app.config import settings
from app.database.models import CabinetRefreshToken, CabinetWsTicket, User
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs local PostgreSQL'),
]
OLD_PASSWORD = 'original-password'
NEW_PASSWORD = 'replacement-password'
ORIGIN = 'https://cabinet.example.com'


def request(*, rotation=True):
    return Request(
        {
            'type': 'http',
            'method': 'POST',
            'path': '/cabinet/auth',
            'headers': [(b'x-refresh-token-rotation', b'1')] if rotation else [],
            'client': ('127.0.0.1', 1000),
            'scheme': 'http',
            'server': ('testserver', 80),
        }
    )


@pytest_asyncio.fixture
async def account(sessions, monkeypatch):
    monkeypatch.setattr(password_utils, 'BCRYPT_ROUNDS', 4)
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'session-test-secret-with-more-than-32-characters')
    monkeypatch.setattr(settings, 'CABINET_URL', ORIGIN + '/cabinet')
    monkeypatch.setattr(settings, 'CABINET_ALLOWED_ORIGINS', ORIGIN)
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(auth, 'ensure_superadmin_role_on_login', AsyncMock())
    monkeypatch.setattr(auth.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=([], [], 0)))
    monkeypatch.setattr(auth, '_process_campaign_bonus', AsyncMock(return_value=None))
    monkeypatch.setattr(dependencies, 'schedule_cabinet_action_log', lambda *args: None)
    monkeypatch.setattr(dependencies.maintenance_service, 'is_maintenance_active', lambda: False)
    monkeypatch.setattr(dependencies.blacklist_service, 'is_user_blacklisted', AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(ws_routes.PermissionService, 'check_permission', AsyncMock(return_value=(False, 'test')))
    monkeypatch.setattr(ws_routes, 'AsyncSessionLocal', sessions)
    monkeypatch.setattr(support_ws, 'AsyncSessionLocal', sessions)
    monkeypatch.setattr(ws_routes, 'cabinet_ws_manager', ws_routes.CabinetConnectionManager(session_factory=sessions))
    monkeypatch.setattr(ws_routes, 'SESSION_CHECK_SECONDS', 0.03)
    async with sessions() as db:
        await db.execute(text('TRUNCATE users RESTART IDENTITY CASCADE'))
        user = User(
            email='audit@example.com',
            email_verified=True,
            auth_type='email',
            first_name='Test',
            status='active',
            cabinet_last_login=datetime.now(UTC),
            password_hash=password_utils.hash_password(OLD_PASSWORD),
            password_reset_token='reset-secret',
            password_reset_expires=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(user)
        await db.commit()
        user_id = user.id
        pair = await auth._create_auth_response(user, db)
        await auth._store_refresh_token(db, user_id, pair.refresh_token)
    return SimpleNamespace(user_id=user_id, pair=pair)


async def reset(sessions):
    async with sessions() as db:
        return await auth.reset_password(
            PasswordResetRequest(token='reset-secret', password=NEW_PASSWORD), request(), db
        )


async def refresh(sessions, token, *, rotation=True):
    async with sessions() as db:
        return await auth.refresh_token(RefreshTokenRequest(refresh_token=token), request(rotation=rotation), db)


async def issue(sessions, account, *, payload=None, origin=ORIGIN):
    payload = payload or jwt_handler.get_token_payload(account.pair.access_token)
    async with sessions() as db:
        return await ws_tickets.issue_ws_ticket(db, payload, origin)


async def consume(sessions, ticket, origin=ORIGIN):
    async with sessions() as db:
        return await ws_tickets.consume_ws_ticket(db, ticket, origin)


async def test_reset_revokes_all_old_credentials_and_allows_new_login(sessions, account):
    async with sessions() as db:
        other = jwt_handler.create_refresh_token(account.user_id)
        await auth._store_refresh_token(db, account.user_id, other)
    old_auto = jwt_handler.create_auto_login_token(account.user_id)
    await reset(sessions)
    async with sessions() as db:
        user = await db.get(User, account.user_id)
        assert user.cabinet_auth_version == 1
        assert user.password_reset_token is None
        assert password_utils.verify_password(NEW_PASSWORD, user.password_hash)
        assert (
            await db.scalar(
                select(func.count()).select_from(CabinetRefreshToken).where(CabinetRefreshToken.revoked_at.is_(None))
            )
            == 0
        )
        credentials = HTTPAuthorizationCredentials(scheme='Bearer', credentials=account.pair.access_token)
        with pytest.raises(HTTPException, match='Session revoked'):
            await dependencies.get_current_cabinet_user(request(), credentials, db)
        assert await dependencies.get_optional_cabinet_user(request(), credentials, db) is None
    for old_refresh in (account.pair.refresh_token, other):
        with pytest.raises(HTTPException):
            await refresh(sessions, old_refresh)
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await auth.auto_login(AutoLoginRequest(token=old_auto), request(), db)
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await auth.login_email(EmailLoginRequest(email='audit@example.com', password=OLD_PASSWORD), request(), db)
    async with sessions() as db:
        pair = await auth.login_email(
            EmailLoginRequest(email='audit@example.com', password=NEW_PASSWORD), request(), db
        )
        assert jwt_handler.get_token_payload(pair.access_token)['auth_version'] == 1
        assert jwt_handler.get_token_payload(pair.refresh_token, 'refresh')['auth_version'] == 1
    assert (await refresh(sessions, pair.refresh_token)).access_token


async def test_two_resets_can_consume_token_only_once(sessions, account):
    results = await asyncio.gather(reset(sessions), reset(sessions), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, HTTPException) for result in results) == 1
    async with sessions() as db:
        assert (await db.get(User, account.user_id)).cabinet_auth_version == 1


@pytest.mark.parametrize('failure', ['sql', 'commit', 'cancel'])
async def test_failed_reset_rolls_back_password_and_revocations(sessions, account, monkeypatch, failure):
    original = auth.revoke_password_sessions

    async def fail(db, user):
        await original(db, user)
        if failure == 'cancel':
            raise asyncio.CancelledError
        if failure == 'sql':
            await db.execute(text('SELECT 1 / 0'))
        else:
            await db.execute(
                text(
                    'CREATE TEMP TABLE reset_commit_failure (user_id integer REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED)'
                )
            )
            await db.execute(text('INSERT INTO reset_commit_failure VALUES (-99999)'))

    monkeypatch.setattr(auth, 'revoke_password_sessions', fail)
    with pytest.raises((DBAPIError, asyncio.CancelledError)):
        await reset(sessions)
    async with sessions() as db:
        user = await db.get(User, account.user_id)
        assert user.cabinet_auth_version == 0 and user.password_reset_token == 'reset-secret'
        assert password_utils.verify_password(OLD_PASSWORD, user.password_hash)
        row = await db.scalar(select(CabinetRefreshToken))
        assert row.revoked_at is None


@pytest.mark.parametrize('reset_first', [True, False])
@pytest.mark.parametrize('rotation', [True, False])
async def test_refresh_racing_reset_cannot_preserve_old_access(sessions, account, monkeypatch, reset_first, rotation):
    entered, release = asyncio.Event(), asyncio.Event()
    if reset_first:
        original = auth.revoke_password_sessions

        async def paused(db, user):
            entered.set()
            await release.wait()
            await original(db, user)

        monkeypatch.setattr(auth, 'revoke_password_sessions', paused)
        first = asyncio.create_task(reset(sessions))
    else:
        original = auth.lock_auth_user

        async def paused(db, user_id):
            user = await original(db, user_id)
            entered.set()
            await release.wait()
            return user

        monkeypatch.setattr(auth, 'lock_auth_user', paused)
        first = asyncio.create_task(refresh(sessions, account.pair.refresh_token, rotation=rotation))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        refresh(sessions, account.pair.refresh_token, rotation=rotation) if reset_first else reset(sessions)
    )
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert any(isinstance(result, dict) for result in results)
    async with sessions() as db:
        user = await db.get(User, account.user_id)
        assert user.cabinet_auth_version == 1
        assert (
            await db.scalar(
                select(func.count()).select_from(CabinetRefreshToken).where(CabinetRefreshToken.revoked_at.is_(None))
            )
            == 0
        )
        for result in results:
            if hasattr(result, 'access_token'):
                assert not session_security.session_version_matches(
                    jwt_handler.get_token_payload(result.access_token), user
                )


async def test_login_issued_before_reset_cannot_store_refresh_after_reset(sessions, account):
    await reset(sessions)
    async with sessions() as db:
        with pytest.raises(HTTPException, match='Session revoked'):
            await auth._store_refresh_token(db, account.user_id, account.pair.refresh_token)
        assert (
            await db.scalar(
                select(func.count()).select_from(CabinetRefreshToken).where(CabinetRefreshToken.revoked_at.is_(None))
            )
            == 0
        )


async def test_ticket_is_hashed_origin_bound_and_consumed_once_across_workers(sessions, account):
    ticket = await issue(sessions, account)
    async with sessions() as db:
        row = await db.scalar(select(CabinetWsTicket))
        assert row.token_hash == hashlib.sha256(ticket.encode()).hexdigest() and row.token_hash != ticket
    assert await consume(sessions, ticket, 'https://evil.example.com') is None
    results = await asyncio.gather(*(consume(sessions, ticket) for _ in range(4)))
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result)['sub'] == str(account.user_id)


async def test_expired_ticket_and_disallowed_origin_rejected(sessions, account):
    with pytest.raises(HTTPException):
        await issue(sessions, account, origin='https://evil.example.com')
    ticket = await issue(sessions, account)
    async with sessions() as db:
        row = await db.scalar(select(CabinetWsTicket))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await consume(sessions, ticket) is None


async def test_ticket_issue_is_bounded_and_never_outlives_access_token(sessions, account, monkeypatch):
    monkeypatch.setattr(ws_tickets, 'MAX_PENDING_TICKETS_PER_USER', 1)
    payload = jwt_handler.get_token_payload(account.pair.access_token)
    payload['exp'] = datetime.now(UTC).timestamp() + 5
    await issue(sessions, account, payload=payload)
    with pytest.raises(HTTPException) as error:
        await issue(sessions, account)
    assert error.value.status_code == 429
    async with sessions() as db:
        row = await db.scalar(select(CabinetWsTicket))
        assert row.expires_at == row.access_expires_at


async def test_ticket_endpoint_requires_bearer_and_returns_no_store(sessions, account):
    app = FastAPI()
    app.include_router(ws_routes.router, prefix='/cabinet')

    async def db_override():
        async with sessions() as db:
            yield db

    app.dependency_overrides[dependencies.get_cabinet_db] = db_override
    with TestClient(app) as client:
        assert client.post('/cabinet/ws/ticket', headers={'Origin': ORIGIN}).status_code == 401
        response = client.post(
            '/cabinet/ws/ticket', headers={'Origin': ORIGIN, 'Authorization': 'Bearer ' + account.pair.access_token}
        )
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        assert len(response.json()['ticket']) == 43


@pytest.mark.parametrize('reason', ['expiry', 'reset', 'block'])
async def test_live_idle_websocket_closes_when_access_is_lost(sessions, account, reason):
    payload = jwt_handler.get_token_payload(account.pair.access_token)
    if reason == 'expiry':
        payload['exp'] = datetime.now(UTC).timestamp() + 0.5
    ticket = await issue(sessions, account, payload=payload)
    app = FastAPI()
    app.include_router(ws_routes.router, prefix='/cabinet')
    with (
        TestClient(app) as client,
        client.websocket_connect('/cabinet/ws?ticket=' + ticket, headers={'Origin': ORIGIN}) as socket,
    ):
        assert socket.receive_json()['type'] == 'connected'
        if reason == 'reset':
            await reset(sessions)
        elif reason == 'block':
            async with sessions() as db:
                user = await db.get(User, account.user_id)
                user.status = 'blocked'
                await db.commit()
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert not ws_routes.cabinet_ws_manager._sessions


async def test_ticket_issued_before_reset_cannot_open_socket(sessions, account):
    ticket = await issue(sessions, account)
    await reset(sessions)
    app = FastAPI()
    app.include_router(ws_routes.router)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect('/ws?ticket=' + ticket, headers={'Origin': ORIGIN}),
    ):
        pass


async def test_admin_broadcast_rechecks_permissions_and_drops_revoked_sessions(sessions, account, monkeypatch):
    permissions = AsyncMock(return_value=(True, 'admin'))
    monkeypatch.setattr(ws_routes.PermissionService, 'check_permission', permissions)
    socket = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    session = ws_routes.CabinetWsSession(socket, jwt_handler.get_token_payload(account.pair.access_token))
    manager = ws_routes.cabinet_ws_manager
    assert await manager.validate(session) and session.is_admin
    await manager.connect(session)
    permissions.return_value = (False, 'role revoked')
    await manager.send_to_admins({'type': 'ticket.new', 'message': 'private'})
    socket.send_text.assert_not_awaited()
    socket.close.assert_awaited_once()
    assert session not in manager._sessions
    await reset(sessions)
    await manager.send_to_user(account.user_id, {'type': 'balance.change'})
    socket.send_text.assert_not_awaited()
    assert session not in manager._sessions


async def test_slow_websocket_is_closed_without_blocking_other_users(sessions, account, monkeypatch):
    async def stalled(_data):
        await asyncio.sleep(60)

    monkeypatch.setattr(ws_routes, 'SEND_TIMEOUT_SECONDS', 0.15)
    manager = ws_routes.cabinet_ws_manager
    fast = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
    slow = SimpleNamespace(send_text=AsyncMock(side_effect=stalled), close=AsyncMock())
    payload = jwt_handler.get_token_payload(account.pair.access_token)
    for socket in (fast, slow):
        await manager.connect(ws_routes.CabinetWsSession(socket, payload))
    async with asyncio.timeout(1):
        await manager.send_to_user(account.user_id, {'type': 'test'})
    fast.send_text.assert_awaited_once()
    slow.close.assert_awaited_once()
    assert len(manager._sessions) == 1


async def test_support_socket_also_rejects_reset_credentials(sessions, account):
    payload = jwt_handler.get_token_payload(account.pair.access_token)
    async with sessions() as db:
        user = await db.get(User, account.user_id)
        context = support_ws.WsUserContext(user=user, token_payload=payload, role='owner')
    session = support_ws.SupportWsSession(SimpleNamespace(close=AsyncMock()), context)
    await reset(sessions)
    async with sessions() as db:
        assert not await support_ws._refresh_session_context(db, session)
        websocket = SimpleNamespace(headers={})
        context, error = await support_ws._authenticate_ws(db, websocket, access_token=account.pair.access_token)
        assert context is None and error['code'] == 'AUTH_REQUIRED'


async def test_login_with_old_password_racing_reset_cannot_issue_current_credentials(sessions, account, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = auth._create_auth_response

    async def paused(user, db):
        entered.set()
        await release.wait()
        return await original(user, db)

    monkeypatch.setattr(auth, '_create_auth_response', paused)

    async def login():
        async with sessions() as db:
            return await auth.login_email(
                EmailLoginRequest(email='audit@example.com', password=OLD_PASSWORD), request(), db
            )

    login_task = asyncio.create_task(login())
    await asyncio.wait_for(entered.wait(), 2)
    await reset(sessions)
    release.set()
    with pytest.raises(HTTPException, match='Session revoked'):
        await login_task


async def test_pre_migration_tokens_work_only_until_first_reset(sessions, account):
    import jwt

    def legacy(token, token_type):
        payload = jwt_handler.get_token_payload(token, token_type)
        payload.pop('auth_version')
        return jwt.encode(payload, settings.get_cabinet_jwt_secret(), algorithm=jwt_handler.JWT_ALGORITHM)

    access = legacy(account.pair.access_token, 'access')
    old_refresh = legacy(account.pair.refresh_token, 'refresh')
    async with sessions() as db:
        await auth._store_refresh_token(db, account.user_id, old_refresh)
        credentials = HTTPAuthorizationCredentials(scheme='Bearer', credentials=access)
        assert (await dependencies.get_optional_cabinet_user(request(), credentials, db)).id == account.user_id
    assert (await refresh(sessions, old_refresh, rotation=False)).refresh_token == old_refresh
    await reset(sessions)
    async with sessions() as db:
        assert await dependencies.get_optional_cabinet_user(request(), credentials, db) is None
    with pytest.raises(HTTPException):
        await refresh(sessions, old_refresh, rotation=False)


async def test_repeated_store_never_resurrects_logged_out_refresh(sessions, account):
    async with sessions() as db:
        await auth.logout(RefreshTokenRequest(refresh_token=account.pair.refresh_token), db)
        await auth._store_refresh_token(db, account.user_id, account.pair.refresh_token)
        assert (await db.scalar(select(CabinetRefreshToken))).revoked_at is not None


async def test_live_socket_rejects_legacy_bearer_url_and_replayed_ticket(sessions, account):
    app = FastAPI()
    app.include_router(ws_routes.router)
    ticket = await issue(sessions, account)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect('/ws?token=' + account.pair.access_token):
            pass
        with client.websocket_connect('/ws?ticket=' + ticket, headers={'Origin': ORIGIN}) as socket:
            assert socket.receive_json()['type'] == 'connected'
            socket.send_json({'type': 'ping'})
            assert socket.receive_json()['type'] == 'pong'
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect('/ws?ticket=' + ticket, headers={'Origin': ORIGIN}),
        ):
            pass


async def test_connection_limits_are_atomic(sessions, account, monkeypatch):
    monkeypatch.setattr(ws_routes, 'MAX_CONNECTIONS_PER_USER', 1)
    manager = ws_routes.cabinet_ws_manager
    payload = jwt_handler.get_token_payload(account.pair.access_token)
    attempts = [ws_routes.CabinetWsSession(SimpleNamespace(), payload) for _ in range(4)]
    results = await asyncio.gather(*(manager.connect(session) for session in attempts))
    assert results.count(True) == 1 and len(manager._sessions) == 1
