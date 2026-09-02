from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.cabinet.auth.subpage_bff import (
    build_subpage_bff_signature,
    verify_subpage_bff_request,
)
from app.config import settings
from app.utils.cache import cache


SECRET = '0123456789abcdef0123456789abcdef'
NOW = 1_770_000_000
NONCE = 'abcdefghijklmnopqrstuvwx'
SHORT_UUID = 'AbCdEf0123456789'
CLIENT_IP = '203.0.113.9'
PATH = f'/cabinet/subpage/{SHORT_UUID}/invoice'
BODY = b'{"method":"wata","periodDays":30}'


def _build_request(
    *,
    body: bytes = BODY,
    path_short_uuid: str | None = None,
    signature_body: bytes = BODY,
    timestamp: str = str(NOW),
) -> Request:
    signature = build_subpage_bff_signature(
        secret=SECRET,
        timestamp=timestamp,
        nonce=NONCE,
        short_uuid=SHORT_UUID,
        client_ip=CLIENT_IP,
        method='POST',
        path=PATH,
        body=signature_body,
    )
    headers = {
        'content-type': 'application/json',
        'x-subpage-client-ip': CLIENT_IP,
        'x-subpage-nonce': NONCE,
        'x-subpage-short-uuid': SHORT_UUID,
        'x-subpage-signature': signature,
        'x-subpage-timestamp': timestamp,
    }
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': PATH,
        'path_params': {'short_uuid': path_short_uuid} if path_short_uuid else {},
        'headers': [(key.encode(), value.encode()) for key, value in headers.items()],
    }

    async def receive() -> dict[str, Any]:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def configure_bff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SUBPAGE_BFF_SECRET', SECRET, raising=False)
    monkeypatch.setattr('app.cabinet.auth.subpage_bff.time.time', lambda: NOW)


def test_signature_matches_cross_runtime_vector() -> None:
    signature = build_subpage_bff_signature(
        secret=SECRET,
        timestamp=str(NOW),
        nonce=NONCE,
        short_uuid=SHORT_UUID,
        client_ip=CLIENT_IP,
        method='POST',
        path=PATH,
        body=BODY,
    )

    assert signature == '8dea10187729a531c14fe6679c3a5d9442bc4d5d652a3fae9dff86d437ca1038'


@pytest.mark.anyio('asyncio')
async def test_valid_request_sets_trusted_state(monkeypatch: pytest.MonkeyPatch) -> None:
    setnx = AsyncMock(return_value=True)
    monkeypatch.setattr(cache, 'setnx', setnx)
    request = _build_request()

    await verify_subpage_bff_request(request)

    assert request.state.subpage_client_ip == CLIENT_IP
    assert request.state.subpage_short_uuid == SHORT_UUID
    setnx.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_changed_body_is_rejected_before_nonce_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setnx = AsyncMock(return_value=True)
    monkeypatch.setattr(cache, 'setnx', setnx)
    request = _build_request(body=b'{"method":"yookassa","periodDays":365}')

    with pytest.raises(HTTPException) as exc_info:
        await verify_subpage_bff_request(request)

    assert exc_info.value.status_code == 401
    setnx.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_replayed_nonce_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, 'setnx', AsyncMock(return_value=False))

    with pytest.raises(HTTPException) as exc_info:
        await verify_subpage_bff_request(_build_request())

    assert exc_info.value.status_code == 401


@pytest.mark.anyio('asyncio')
async def test_route_short_uuid_must_match_signed_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setnx = AsyncMock(return_value=True)
    monkeypatch.setattr(cache, 'setnx', setnx)

    with pytest.raises(HTTPException) as exc_info:
        await verify_subpage_bff_request(_build_request(path_short_uuid='OtherUuid123456'))

    assert exc_info.value.status_code == 401
    setnx.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_stale_request_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    setnx = AsyncMock(return_value=True)
    monkeypatch.setattr(cache, 'setnx', setnx)
    request = _build_request(timestamp=str(NOW - 91))

    with pytest.raises(HTTPException) as exc_info:
        await verify_subpage_bff_request(request)

    assert exc_info.value.status_code == 401
    setnx.assert_not_awaited()
