"""Exercise RSA rotation and refresh admission without any provider traffic."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from app.external import wata_webhook as wata_module
from app.external.wata_webhook import WataPublicKeyProvider, WataWebhookHandler


pytestmark = pytest.mark.anyio
BODY = b'{ "orderId": "rotation-probe", "transactionStatus": "Paid" }\n'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(scope='module')
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3))


def pem(key):
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode('ascii')
    )


def sign(key, body=BODY):
    return base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA512())).decode('ascii')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class QueueProvider(WataPublicKeyProvider):
    def __init__(self, responses, *, clock, cache_seconds=3600):
        super().__init__(cache_seconds=cache_seconds, clock=clock)
        self.responses = deque(responses)
        self.calls = 0

    async def _fetch_public_key(self):
        self.calls += 1
        if not self.responses:
            raise AssertionError('Unexpected extra WATA public-key request')
        return self.responses.popleft()


def handler(provider):
    return WataWebhookHandler(SimpleNamespace(), public_key_provider=provider)


@pytest.mark.parametrize('interval', [0, -1, float('inf'), float('nan')])
def test_refresh_interval_must_be_finite_and_positive(interval):
    with pytest.raises(ValueError, match='must be positive'):
        WataPublicKeyProvider(refresh_interval_seconds=interval)


async def test_immediate_rotation_after_successful_initial_fetch(keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0]), pem(keys[1])], clock=clock)
    assert await provider.get_public_key() == pem(keys[0])

    assert await handler(provider)._verify_signature(BODY, sign(keys[1])) is True

    assert clock.now == 0
    assert provider.calls == 2
    assert await provider.get_public_key() == pem(keys[1])


async def test_invalid_signature_retries_only_once_after_refresh(monkeypatch, keys):
    provider = QueueProvider([pem(keys[0]), pem(keys[1]), pem(keys[2])], clock=Clock())
    signature = sign(keys[2])
    verify_attempts = []
    original_padding = wata_module.padding.PKCS1v15

    def record_padding():
        verify_attempts.append(True)
        return original_padding()

    monkeypatch.setattr(wata_module.padding, 'PKCS1v15', record_padding)

    assert await handler(provider)._verify_signature(BODY, signature) is False

    assert provider.calls == 2
    assert len(verify_attempts) == 2


async def test_valid_cached_signature_does_not_refresh(keys):
    provider = QueueProvider([pem(keys[0])], clock=Clock())
    subject = handler(provider)
    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert provider.calls == 1


@pytest.mark.parametrize('signature', ['', '   ', '!!!', 'YWJj\nZA==', 'c2lnbmF0dXJl!'])
async def test_malformed_base64_does_not_fetch_key(signature):
    provider = QueueProvider([], clock=Clock())
    assert await handler(provider)._verify_signature(BODY, signature) is False
    assert provider.calls == 0


async def test_outage_retains_old_key_and_allows_later_rotation(keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0]), None, pem(keys[1])], clock=clock)
    subject = handler(provider)
    assert await provider.get_public_key() == pem(keys[0])

    assert await subject._verify_signature(BODY, sign(keys[1])) is False
    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    clock.advance(29.999)
    assert await subject._verify_signature(BODY, sign(keys[1])) is False
    assert provider.calls == 2
    clock.advance(0.001)
    assert await subject._verify_signature(BODY, sign(keys[1])) is True
    assert provider.calls == 3


@pytest.mark.parametrize('replacement', ['not-a-pem', None, {'value': 'unexpected'}])
async def test_bad_endpoint_key_never_replaces_known_rsa_key(keys, replacement):
    provider = QueueProvider([pem(keys[0]), replacement], clock=Clock())
    assert await provider.get_public_key() == pem(keys[0])
    assert await handler(provider)._verify_signature(BODY, sign(keys[1])) is False
    assert await provider.get_public_key() == pem(keys[0])
    assert provider.calls == 2


async def test_non_rsa_endpoint_key_never_replaces_known_rsa_key(keys):
    non_rsa = ec.generate_private_key(ec.SECP256R1())
    provider = QueueProvider([pem(keys[0]), pem(non_rsa)], clock=Clock())
    assert await provider.get_public_key() == pem(keys[0])
    assert await handler(provider)._verify_signature(BODY, sign(keys[1])) is False
    assert await provider.get_public_key() == pem(keys[0])


@pytest.mark.parametrize('failure', ['http-503', 'timeout', 'invalid-pem', 'invalid-json', 'non-rsa'])
async def test_actual_fetch_failure_retains_cached_key_and_recovers(monkeypatch, keys, failure):
    clock = Clock()
    responses = deque(['initial', failure, 'recovered'])
    requests = []
    monkeypatch.setattr(wata_module.settings, 'WATA_PUBLIC_KEY_URL', 'https://api.wata.pro/api/h2h/public-key')

    class Response:
        def __init__(self, result):
            self.result = result
            self.status = 503 if result == 'http-503' else 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def text(self):
            return 'synthetic public-key response'

        async def json(self):
            if self.result == 'invalid-json':
                raise ValueError('synthetic invalid JSON')
            value = {
                'initial': pem(keys[0]),
                'recovered': pem(keys[1]),
                'invalid-pem': 'invalid-pem',
                'non-rsa': pem(ec.generate_private_key(ec.SECP256R1())),
            }.get(self.result)
            return {'value': value}

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def get(self, url):
            requests.append(url)
            result = responses.popleft()
            if result == 'timeout':
                raise TimeoutError('synthetic endpoint timeout')
            return Response(result)

    monkeypatch.setattr(wata_module, 'ClientSession', Session)
    provider = WataPublicKeyProvider(clock=clock)
    subject = handler(provider)
    assert await provider.get_public_key() == pem(keys[0])
    assert await subject._verify_signature(BODY, sign(keys[1])) is False
    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert await subject._verify_signature(BODY, sign(keys[1])) is False
    assert len(requests) == 2
    clock.advance(30)
    assert await subject._verify_signature(BODY, sign(keys[1])) is True
    assert requests == ['https://api.wata.pro/api/h2h/public-key'] * 3
    assert not responses


async def test_spoofed_webhooks_are_rate_limited_including_unchanged_key(keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0]), pem(keys[0]), pem(keys[0])], clock=clock)
    subject = handler(provider)
    assert await provider.get_public_key() == pem(keys[0])
    forged = sign(keys[2])
    for _ in range(100):
        assert await subject._verify_signature(BODY, forged) is False
    assert provider.calls == 2
    clock.advance(30)
    assert await subject._verify_signature(BODY, forged) is False
    assert provider.calls == 3


async def test_initial_outage_rate_limits_all_repeated_requests(keys):
    clock = Clock()
    provider = QueueProvider([None, pem(keys[0])], clock=clock)
    subject = handler(provider)
    for _ in range(100):
        assert await subject._verify_signature(BODY, sign(keys[0])) is False
    assert provider.calls == 1
    clock.advance(30)
    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert provider.calls == 2


async def test_expired_key_outage_rate_limits_normal_and_forced_requests(keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0]), None, pem(keys[1])], clock=clock, cache_seconds=1)
    subject = handler(provider)
    assert await provider.get_public_key() == pem(keys[0])
    clock.advance(31)
    for _ in range(30):
        assert await subject._verify_signature(BODY, sign(keys[1])) is False
        assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert provider.calls == 2
    clock.advance(30)
    assert await subject._verify_signature(BODY, sign(keys[1])) is True
    assert provider.calls == 3


@pytest.mark.parametrize('refresh_result', ['new', 'same', 'unavailable'])
async def test_concurrent_signature_failures_share_slow_refresh(monkeypatch, keys, refresh_result):
    clock = Clock()
    provider = QueueProvider([pem(keys[0])], clock=clock)
    assert await provider.get_public_key() == pem(keys[0])
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch():
        provider.calls += 1
        started.set()
        await release.wait()
        return {'new': pem(keys[1]), 'same': pem(keys[0]), 'unavailable': None}[refresh_result]

    monkeypatch.setattr(provider, '_fetch_public_key', slow_fetch)
    subject = handler(provider)
    signature = sign(keys[1])
    tasks = [asyncio.create_task(subject._verify_signature(BODY, signature)) for _ in range(64)]
    await started.wait()
    await asyncio.sleep(0)
    clock.advance(120)
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [refresh_result == 'new'] * 64
    assert provider.calls == 2


@pytest.mark.parametrize('initial_result', ['available', 'unavailable'])
async def test_concurrent_initial_requests_share_slow_fetch(monkeypatch, keys, initial_result):
    clock = Clock()
    provider = QueueProvider([], clock=clock)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch():
        provider.calls += 1
        started.set()
        await release.wait()
        return pem(keys[0]) if initial_result == 'available' else None

    monkeypatch.setattr(provider, '_fetch_public_key', slow_fetch)
    tasks = [asyncio.create_task(provider.get_public_key()) for _ in range(64)]
    await started.wait()
    await asyncio.sleep(0)
    clock.advance(120)
    release.set()
    results = await asyncio.gather(*tasks)

    expected = pem(keys[0]) if initial_result == 'available' else None
    assert results == [expected] * 64
    assert provider.calls == 1


async def test_concurrent_expired_requests_share_slow_outage(monkeypatch, keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0])], clock=clock, cache_seconds=1)
    assert await provider.get_public_key() == pem(keys[0])
    clock.advance(31)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch():
        provider.calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(provider, '_fetch_public_key', slow_fetch)
    tasks = [asyncio.create_task(provider.get_public_key()) for _ in range(64)]
    await started.wait()
    await asyncio.sleep(0)
    clock.advance(120)
    release.set()

    assert await asyncio.gather(*tasks) == [pem(keys[0])] * 64
    assert provider.calls == 2


async def test_late_request_with_observed_old_key_reuses_rotated_key(keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0]), pem(keys[1])], clock=clock)
    old_key = await provider.get_public_key()
    assert await provider.get_public_key(force_refresh=True, observed_key=old_key) == pem(keys[1])
    clock.advance(120)

    assert await provider.get_public_key(force_refresh=True, observed_key=old_key) == pem(keys[1])
    assert provider.calls == 2


async def test_cancelled_refresh_keeps_old_key_and_releases_lock(monkeypatch, keys):
    clock = Clock()
    provider = QueueProvider([pem(keys[0])], clock=clock)
    assert await provider.get_public_key() == pem(keys[0])
    started = asyncio.Event()

    async def interrupted_fetch():
        provider.calls += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, '_fetch_public_key', interrupted_fetch)
    subject = handler(provider)
    task = asyncio.create_task(subject._verify_signature(BODY, sign(keys[1])))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await subject._verify_signature(BODY, sign(keys[0])) is True
    assert await subject._verify_signature(BODY, sign(keys[1])) is False
    assert provider.calls == 2

    async def available_fetch():
        provider.calls += 1
        return pem(keys[1])

    monkeypatch.setattr(provider, '_fetch_public_key', available_fetch)
    clock.advance(30)
    assert await subject._verify_signature(BODY, sign(keys[1])) is True
    assert provider.calls == 3


@pytest.mark.parametrize(
    'body', [BODY, b'\xef\xbb\xbf' + BODY, b'{"memo":"\xd1\x82\xd0\xb5\xd1\x81\xd1\x82"}', b'\xff\xfe\xfd']
)
async def test_verification_uses_exact_signed_bytes(keys, body):
    provider = QueueProvider([pem(keys[0])], clock=Clock())
    assert await handler(provider)._verify_signature(body, sign(keys[0], body)) is True
    assert provider.calls == 1


async def test_changed_bytes_fail_signature(keys):
    provider = QueueProvider([pem(keys[0]), pem(keys[0])], clock=Clock())
    assert await handler(provider)._verify_signature(BODY.rstrip(), sign(keys[0], BODY)) is False
    assert provider.calls == 2


@pytest.mark.parametrize('body', [b'\xff\xfe\xfd', b'not-json'])
async def test_aiohttp_signed_invalid_json_does_not_open_database(monkeypatch, keys, body):
    monkeypatch.setattr(type(wata_module.settings), 'is_wata_enabled', lambda self: True)
    database = AsyncMock(side_effect=AssertionError('Invalid JSON must not open a database session'))
    monkeypatch.setattr(wata_module, 'AsyncSessionLocal', database)
    provider = QueueProvider([pem(keys[0])], clock=Clock())
    request = SimpleNamespace(
        read=AsyncMock(return_value=body),
        text=AsyncMock(side_effect=AssertionError('Must not decode the request before verification')),
        headers={'X-Signature': sign(keys[0], body), 'Content-Type': 'application/json; charset=latin-1'},
    )

    response = await handler(provider).handle_webhook(request)

    assert response.status == 400
    assert database.call_count == 0
    request.read.assert_awaited_once()
    request.text.assert_not_awaited()


async def test_aiohttp_invalid_signature_never_opens_database(monkeypatch, keys):
    monkeypatch.setattr(type(wata_module.settings), 'is_wata_enabled', lambda self: True)
    database = AsyncMock(side_effect=AssertionError('Invalid signature must not open a database session'))
    monkeypatch.setattr(wata_module, 'AsyncSessionLocal', database)
    provider = QueueProvider([pem(keys[0]), pem(keys[1])], clock=Clock())
    request = SimpleNamespace(read=AsyncMock(return_value=BODY), headers={'X-Signature': sign(keys[2])})

    response = await handler(provider).handle_webhook(request)

    assert response.status == 401
    assert database.call_count == 0
    assert provider.calls == 2


async def test_aiohttp_rotated_key_processes_payment_exactly_once(monkeypatch, keys):
    monkeypatch.setattr(type(wata_module.settings), 'is_wata_enabled', lambda self: True)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    class Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(wata_module, 'AsyncSessionLocal', Session)
    payment_service = SimpleNamespace(process_wata_webhook=AsyncMock(return_value=True))
    provider = QueueProvider([pem(keys[0]), pem(keys[1])], clock=Clock())
    assert await provider.get_public_key() == pem(keys[0])
    request = SimpleNamespace(read=AsyncMock(return_value=BODY), headers={'X-Signature': sign(keys[1])})

    response = await WataWebhookHandler(payment_service, public_key_provider=provider).handle_webhook(request)

    assert response.status == 200
    payment_service.process_wata_webhook.assert_awaited_once_with(
        db, {'orderId': 'rotation-probe', 'transactionStatus': 'Paid'}
    )
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    assert provider.calls == 2
