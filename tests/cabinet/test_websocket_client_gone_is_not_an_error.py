"""Обрыв клиента на вебсокете не должен выглядеть аварией приложения.

Владельцу несколько раз в день приходил отчёт «⚠️ Ошибка во время работы» с
`ClientDisconnected: Cabinet WS: Failed to accept from`. Ничего не ломалось:
браузер закрывал вкладку (телефон уходил в сон, сеть моргала) между запросом
и нашим `accept()`. Uvicorn поднимает на этом `ClientDisconnected`, а код
писал его в `logger.error` — и конвейер отчётов честно нёс это владельцу,
топя в шуме настоящие ошибки.

Сторож требует: на обрыве клиента журнал молчит на уровне error, а на любой
другой поломке — по-прежнему говорит.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.websocket_errors import is_client_gone


def _client_disconnected() -> BaseException:
    from uvicorn.protocols.utils import ClientDisconnected

    return ClientDisconnected()


class _Socket:
    """Сокет, который умирает на первом же обращении — как ушедший клиент."""

    def __init__(self, error: BaseException):
        self.error = error
        self.client = type('C', (), {'host': '1.2.3.4'})()
        self.query_params: dict[str, str] = {}
        self.headers = {'origin': 'https://cabinet.example.com'}

    async def accept(self, *args, **kwargs):
        raise self.error

    async def close(self, *args, **kwargs):
        raise self.error

    async def send_json(self, *args, **kwargs):
        raise self.error

    async def receive_text(self):
        raise self.error


def test_known_disconnects_are_recognised() -> None:
    from starlette.websockets import WebSocketDisconnect

    assert is_client_gone(_client_disconnected())
    assert is_client_gone(WebSocketDisconnect(code=1006))
    assert is_client_gone(ConnectionResetError())
    assert is_client_gone(BrokenPipeError())
    # Starlette отвечает обычным RuntimeError на запись в закрытый сокет.
    assert is_client_gone(RuntimeError('Cannot call "send" once a close message has been sent.'))


def test_real_failures_are_not_mistaken_for_a_disconnect() -> None:
    assert not is_client_gone(ValueError('bad payload'))
    assert not is_client_gone(RuntimeError('database is on fire'))
    assert not is_client_gone(KeyError('token'))


@pytest.fixture
def cabinet_ticket_auth(monkeypatch):
    """Exercise the real endpoint through its current one-use ticket boundary."""
    from app.cabinet.routes import websocket as ws_route

    db = AsyncMock()
    db.__aenter__.return_value = db
    session_factory = MagicMock(return_value=db)
    consume = AsyncMock(return_value={'sub': '1', 'exp': time.time() + 60, 'auth_version': 3})
    manager = SimpleNamespace(
        validate=AsyncMock(return_value=True),
        connect=AsyncMock(return_value=True),
        close=AsyncMock(),
        send=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ws_route, 'AsyncSessionLocal', session_factory)
    monkeypatch.setattr(ws_route, 'consume_ws_ticket', consume)
    monkeypatch.setattr(ws_route, 'cabinet_ws_manager', manager)
    monkeypatch.setattr(ws_route, 'get_client_ip', lambda socket: socket.client.host)
    return SimpleNamespace(consume=consume, manager=manager, db=db, session_factory=session_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize('error_factory', [_client_disconnected, ConnectionResetError])
async def test_cabinet_socket_stays_quiet_when_client_is_gone(error_factory, cabinet_ticket_auth) -> None:
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(error_factory())
    socket.query_params = {'ticket': 'one-use-ticket'}

    with (
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    cabinet_ticket_auth.consume.assert_awaited_once_with(
        cabinet_ticket_auth.db, 'one-use-ticket', 'https://cabinet.example.com'
    )
    cabinet_ticket_auth.manager.connect.assert_awaited_once()
    session = cabinet_ticket_auth.manager.connect.await_args.args[0]
    assert session.payload['auth_version'] == 3
    cabinet_ticket_auth.manager.close.assert_awaited_once_with(session)
    assert not errored.called, 'обрыв клиента ушёл в отчёт владельцу как авария'
    assert not excepted.called


@pytest.mark.asyncio
async def test_cabinet_socket_still_reports_a_real_failure_without_credentials(cabinet_ticket_auth) -> None:
    from app.cabinet.routes import websocket as ws_route

    secret = 'private-one-use-ticket'
    socket = _Socket(RuntimeError(f'accept handler is broken: ?ticket={secret}'))
    socket.query_params = {'ticket': secret}

    with (
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    errored.assert_called_once_with('Cabinet WS failed', error_type='RuntimeError')
    excepted.assert_not_called()
    assert secret not in str(errored.call_args)
    cabinet_ticket_auth.manager.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'query', [{}, {'token': 'legacy-jwt'}, {'ticket': 'bad'}, {'ticket': 'good', 'token': 'legacy-jwt'}]
)
async def test_cabinet_socket_survives_a_disconnect_while_refusing(query, cabinet_ticket_auth) -> None:
    """Отказ неавторизованному тоже пишет в сокет — и тоже может не застать клиента."""
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(_client_disconnected())
    socket.query_params = query
    cabinet_ticket_auth.consume.return_value = None

    with (
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    if query == {'ticket': 'bad'}:
        cabinet_ticket_auth.consume.assert_awaited_once()
    else:
        cabinet_ticket_auth.consume.assert_not_awaited()
        cabinet_ticket_auth.session_factory.assert_not_called()
    cabinet_ticket_auth.manager.connect.assert_not_awaited()
    assert not errored.called
    assert not excepted.called


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [{'token': 'legacy-jwt'}, {'ticket': 'good', 'token': 'legacy-jwt'}])
async def test_legacy_bearer_query_cannot_authenticate_or_consume_ticket(query, cabinet_ticket_auth) -> None:
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(RuntimeError('must not accept a bearer query'))
    socket.query_params = query
    socket.accept = AsyncMock()
    socket.close = AsyncMock()

    await ws_route.cabinet_websocket_endpoint(socket)

    socket.close.assert_awaited_once_with(code=1008, reason='WebSocket ticket required')
    socket.accept.assert_not_awaited()
    cabinet_ticket_auth.consume.assert_not_awaited()
    cabinet_ticket_auth.session_factory.assert_not_called()
    cabinet_ticket_auth.manager.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_session_cannot_connect_after_ticket_consumption(cabinet_ticket_auth) -> None:
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(RuntimeError('revoked session must never be accepted'))
    socket.query_params = {'ticket': 'issued-before-revocation'}
    socket.accept = AsyncMock()
    socket.close = AsyncMock()
    cabinet_ticket_auth.manager.validate.return_value = False

    await ws_route.cabinet_websocket_endpoint(socket)

    cabinet_ticket_auth.consume.assert_awaited_once()
    cabinet_ticket_auth.manager.validate.assert_awaited_once()
    cabinet_ticket_auth.manager.connect.assert_not_awaited()
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(code=1008, reason='Session revoked or expired')


async def test_webapi_socket_stays_quiet_when_client_is_gone() -> None:
    from app.webapi.routes import websocket as ws_route

    socket = _Socket(_client_disconnected())
    socket.query_params = {'token': 'good'}

    with (
        patch.object(ws_route, 'verify_websocket_token', AsyncMock(return_value=True)),
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.websocket_endpoint(socket)

    assert not errored.called
    assert not excepted.called
