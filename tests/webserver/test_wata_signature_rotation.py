"""Exercise real WATA crypto/cache through the mounted FastAPI payment route.

Only the public-key fetch and the business callback/DB are replaced. The callback
records delivery; these tests make no claim about payment-ledger idempotency.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.external.wata_webhook import WataPublicKeyProvider, WataWebhookHandler
from app.webserver import payments


pytestmark = pytest.mark.anyio
BODY = b'{"orderId":"route-rotation","transactionStatus":"Paid"}'
PATH = '/wata-rotation-test'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture(scope='module')
def signing_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3))


def _pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode('ascii')
    )


def _signature(key: rsa.RSAPrivateKey, body: bytes) -> str:
    return base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA512())).decode('ascii')


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def _mounted_app(monkeypatch: pytest.MonkeyPatch, provider: WataPublicKeyProvider):
    # Isolate the actual WATA route from unrelated provider credentials inherited
    # by the test process, without replacing the route or signature verification.
    for name in vars(type(settings)):
        if name.startswith('is_') and name.endswith('_configured'):
            monkeypatch.setattr(type(settings), name, lambda _settings: False)
    monkeypatch.setattr(type(settings), 'is_wata_configured', lambda _settings: True)
    monkeypatch.setattr(settings, 'WATA_ENABLED', True)
    monkeypatch.setattr(settings, 'WATA_WEBHOOK_PATH', PATH)
    monkeypatch.setattr(payments, '_webhook_callback_semaphore', None)

    database = object()
    database_events: list[str] = []

    async def get_db():
        database_events.append('opened')
        try:
            yield database
        finally:
            database_events.append('closed')

    callback = AsyncMock(return_value=True)
    service = SimpleNamespace(process_wata_webhook=callback)
    handler = WataWebhookHandler(service, public_key_provider=provider)
    monkeypatch.setattr(payments, 'get_db', get_db)
    monkeypatch.setattr(payments, 'WataWebhookHandler', lambda passed_service: handler)
    router = payments.create_payment_router(SimpleNamespace(), service)
    assert router is not None
    app = FastAPI()
    app.include_router(router)
    return app, callback, database, database_events


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url='https://wata-route.invalid')


async def _post(client: AsyncClient, key: rsa.RSAPrivateKey, body: bytes = BODY, *, content_type='application/json'):
    return await client.post(
        PATH, content=body, headers={'X-Signature': _signature(key, body), 'Content-Type': content_type}
    )


async def test_mounted_route_accepts_rotation_before_cache_expiry(monkeypatch, signing_keys) -> None:
    old, new, _ = signing_keys
    provider = WataPublicKeyProvider(cache_seconds=3600)
    fetch = AsyncMock(side_effect=[_pem(old), _pem(new)])
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    assert await provider.get_public_key() == _pem(old)
    app, callback, database, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        response = await _post(client, new)

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
    assert fetch.await_count == 2
    callback.assert_awaited_once_with(database, json.loads(BODY))
    assert events == ['opened', 'closed']


async def test_mounted_route_rejects_signature_invalid_after_refresh(monkeypatch, signing_keys) -> None:
    old, new, unrelated = signing_keys
    provider = WataPublicKeyProvider(cache_seconds=3600)
    fetch = AsyncMock(side_effect=[_pem(old), _pem(new)])
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    await provider.get_public_key()
    app, callback, _, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        response = await _post(client, unrelated)

    assert response.status_code == 401
    assert response.json() == {'status': 'error', 'reason': 'invalid_signature'}
    assert fetch.await_count == 2
    callback.assert_not_awaited()
    assert events == []


async def test_mounted_route_rejects_signed_invalid_encoding_after_crypto(monkeypatch, signing_keys) -> None:
    old, _, _ = signing_keys
    body = b'{"orderId":"route","note":"\xff"}'
    provider = WataPublicKeyProvider(cache_seconds=3600)
    fetch = AsyncMock(return_value=_pem(old))
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    app, callback, _, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        response = await _post(client, old, body)

    # 400 rather than 401 demonstrates that actual RSA verification succeeded
    # before the route rejected the encoding; no payment callback is admitted.
    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'reason': 'invalid_json'}
    assert fetch.await_count == 1
    callback.assert_not_awaited()
    assert events == []


@pytest.mark.parametrize('content_type', ['application/json', 'application/json; charset=iso-8859-1'])
async def test_mounted_route_verifies_original_utf8_whitespace_and_unicode(
    monkeypatch, signing_keys, content_type
) -> None:
    old, _, _ = signing_keys
    body = ' \n{ "orderId" : "сырой-ключ", "transactionStatus" : "Paid" }\t\n'.encode()
    provider = WataPublicKeyProvider(cache_seconds=3600)
    fetch = AsyncMock(return_value=_pem(old))
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    app, callback, database, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        response = await _post(client, old, body, content_type=content_type)

    assert response.status_code == 200
    callback.assert_awaited_once_with(database, json.loads(body.decode('utf-8')))
    assert fetch.await_count == 1
    assert events == ['opened', 'closed']


@pytest.mark.parametrize(
    'body',
    [b'\xef\xbb\xbf' + BODY, BODY.decode('ascii').encode('utf-16')],
    ids=['utf8-bom', 'utf16-bom'],
)
async def test_mounted_route_verifies_original_bom_and_utf16_bytes(monkeypatch, signing_keys, body) -> None:
    old, _, _ = signing_keys
    provider = WataPublicKeyProvider(cache_seconds=3600)
    fetch = AsyncMock(return_value=_pem(old))
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    app, callback, database, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        response = await _post(client, old, body)

    # Standard JSON bytes decoding is allowed after verification. Removing the
    # BOM or transcoding before crypto would invalidate these signed raw bytes.
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
    assert fetch.await_count == 1
    callback.assert_awaited_once_with(database, json.loads(BODY))
    assert events == ['opened', 'closed']


async def test_mounted_route_keeps_old_key_during_outage_and_can_retry(monkeypatch, signing_keys) -> None:
    old, new, _ = signing_keys
    clock = Clock()
    provider = WataPublicKeyProvider(cache_seconds=3600, refresh_interval_seconds=30, clock=clock)
    fetch = AsyncMock(side_effect=[_pem(old), None, _pem(new)])
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    await provider.get_public_key()
    app, callback, database, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        rejected = await _post(client, new)
        assert rejected.status_code == 401
        callback.assert_not_awaited()
        accepted_old = await _post(client, old)
        assert accepted_old.status_code == 200
        assert fetch.await_count == 2
        clock.now = 30
        accepted_new = await _post(client, new)

    assert accepted_new.status_code == 200
    assert fetch.await_count == 3
    assert callback.await_count == 2
    assert all(call.args == (database, json.loads(BODY)) for call in callback.await_args_list)
    assert events == ['opened', 'closed', 'opened', 'closed']


async def test_mounted_concurrent_routes_share_one_refresh(monkeypatch, signing_keys) -> None:
    old, new, _ = signing_keys
    provider = WataPublicKeyProvider(cache_seconds=3600)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch_key():
        nonlocal calls
        calls += 1
        if calls == 1:
            return _pem(old)
        started.set()
        await release.wait()
        return _pem(new)

    fetch = AsyncMock(side_effect=fetch_key)
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    await provider.get_public_key()
    app, callback, _, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        requests = [asyncio.create_task(_post(client, new)) for _ in range(12)]
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            release.set()
        responses = await asyncio.gather(*requests)

    assert [response.status_code for response in responses] == [200] * 12
    assert fetch.await_count == 2
    assert callback.await_count == 12
    assert events.count('opened') == events.count('closed') == 12


async def test_mounted_bad_webhooks_cannot_bypass_refresh_cooldown(monkeypatch, signing_keys) -> None:
    old, new, unrelated = signing_keys
    clock = Clock()
    provider = WataPublicKeyProvider(cache_seconds=3600, refresh_interval_seconds=30, clock=clock)
    fetch = AsyncMock(side_effect=[_pem(old), _pem(new), _pem(new)])
    monkeypatch.setattr(provider, '_fetch_public_key', fetch)
    await provider.get_public_key()
    app, callback, _, events = _mounted_app(monkeypatch, provider)

    async with _client(app) as client:
        responses = [await _post(client, unrelated) for _ in range(20)]
        assert [response.status_code for response in responses] == [401] * 20
        assert fetch.await_count == 2
        clock.now = 29.999
        assert (await _post(client, unrelated)).status_code == 401
        assert fetch.await_count == 2
        clock.now = 30
        assert (await _post(client, unrelated)).status_code == 401

    assert fetch.await_count == 3
    callback.assert_not_awaited()
    assert events == []
