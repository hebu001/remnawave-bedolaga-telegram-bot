"""Exercise actual form parser and client signature through the mounted route."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.services.cardlink_service import CardlinkService
from app.webserver import payments


@pytest.fixture
def mounted(monkeypatch):
    for name in vars(type(settings)):
        if name.startswith('is_') and name.endswith('_configured'):
            monkeypatch.setattr(type(settings), name, lambda self: False)
    monkeypatch.setattr(type(settings), 'is_cardlink_configured', lambda self: True)
    monkeypatch.setattr(settings, 'CARDLINK_ENABLED', False)
    monkeypatch.setattr(settings, 'CARDLINK_API_TOKEN', 'test-token')
    monkeypatch.setattr(settings, 'CARDLINK_SHOP_ID', 'test-shop')
    monkeypatch.setattr(settings, 'CARDLINK_WEBHOOK_PATH', '/test-cardlink')
    monkeypatch.setattr(payments, '_webhook_callback_semaphore', None)
    callback = AsyncMock(return_value=True)
    service = SimpleNamespace(cardlink_service=CardlinkService(), process_cardlink_webhook=callback)
    database = object()
    events = []

    async def db():
        events.append('open')
        try:
            yield database
        finally:
            events.append('close')

    monkeypatch.setattr(payments, 'get_db', db)
    app = FastAPI()
    app.include_router(payments.create_payment_router(SimpleNamespace(), service))
    return SimpleNamespace(app=app, callback=callback, database=database, events=events)


def payload():
    fields = dict(
        InvId='order-123', OutSum='100.00', Commission='0.00', TrsId='trs-123', Status='SUCCESS', CurrencyIn='RUB'
    )
    fields['SignatureValue'] = hashlib.md5(b'100.00:order-123:test-token', usedforsecurity=False).hexdigest().upper()
    return fields


async def post(mounted, raw=None, content_type='application/x-www-form-urlencoded'):
    async with AsyncClient(transport=ASGITransport(app=mounted.app), base_url='https://test.invalid') as client:
        return await client.post(
            '/test-cardlink',
            content=raw if raw is not None else urlencode(payload()),
            headers={'content-type': content_type},
        )


async def test_disabled_creation_keeps_signed_callback_live(mounted):
    response = await post(mounted)
    assert response.status_code == 200 and response.text == 'OK'
    mounted.callback.assert_awaited_once_with(mounted.database, payload())
    assert mounted.events == ['open', 'close']


@pytest.mark.parametrize(
    'raw,content_type,expected',
    [
        (b'', 'application/x-www-form-urlencoded', 400),
        (b'{}', 'application/json', 400),
        (b'OutSum=100%ZZ', 'application/x-www-form-urlencoded', 400),
        (b'OutSum=%FF', 'application/x-www-form-urlencoded', 400),
        (b'Invalid', 'application/x-www-form-urlencoded', 400),
        (b'k=v&k=v', 'application/x-www-form-urlencoded', 400),
        (b'a=' + b'x' * 16384, 'application/x-www-form-urlencoded', 413),
    ],
    ids=['empty', 'wrong-media', 'bad-percent', 'bad-utf8', 'no-separator', 'duplicate', 'oversized'],
)
async def test_invalid_transport_never_opens_database(mounted, raw, content_type, expected):
    response = await post(mounted, raw, content_type)
    assert response.status_code == expected
    mounted.callback.assert_not_awaited()
    assert mounted.events == []


async def test_duplicate_signed_field_is_rejected_before_authentication(mounted):
    raw = urlencode(payload()) + '&OutSum=100.00'
    response = await post(mounted, raw)
    assert response.status_code == 400
    assert mounted.events == []


async def test_invalid_signature_does_not_reach_business_service(mounted):
    fields = payload()
    fields['SignatureValue'] = '0' * 32
    response = await post(mounted, urlencode(fields))
    assert response.status_code == 401
    assert mounted.events == []


@pytest.mark.parametrize('result', [False, RuntimeError('unavailable')])
async def test_unconfirmed_payment_is_retryable(mounted, result):
    if isinstance(result, Exception):
        mounted.callback.side_effect = result
    else:
        mounted.callback.return_value = result
    response = await post(mounted)
    assert response.status_code == 503
    assert mounted.events == ['open', 'close']


async def test_chunked_body_without_content_length_is_bounded(mounted):
    """Exercise the streamed size guard without relying on an HTTP Content-Length header."""
    received = 0
    messages = []
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'https',
        'path': '/test-cardlink',
        'raw_path': b'/test-cardlink',
        'root_path': '',
        'query_string': b'',
        'headers': [(b'content-type', b'application/x-www-form-urlencoded')],
        'server': ('test.invalid', 443),
        'client': ('127.0.0.1', 1234),
    }

    async def receive():
        nonlocal received
        received += 1
        assert received <= 2, 'Oversized request must not consume further chunks'
        return {'type': 'http.request', 'body': b'a=' + b'x' * 8191, 'more_body': True}

    async def send(message):
        messages.append(message)

    await mounted.app(scope, receive, send)
    assert next(item['status'] for item in messages if item['type'] == 'http.response.start') == 413
    assert received == 2
    mounted.callback.assert_not_awaited()
    assert mounted.events == []
