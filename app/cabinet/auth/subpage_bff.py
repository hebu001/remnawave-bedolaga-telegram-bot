"""Authentication for server-to-server subscription-page payment requests."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time

import structlog
from fastapi import HTTPException, Request, status

from app.config import settings
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

_CLOCK_SKEW_SECONDS = 90
_NONCE_TTL_SECONDS = 180
_NONCE_RE = re.compile(r'^[A-Za-z0-9_-]{20,64}$')
_SIGNATURE_RE = re.compile(r'^[0-9a-f]{64}$')
_SHORT_UUID_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
_TIMESTAMP_RE = re.compile(r'^\d{10}$')


def build_subpage_bff_signature(
    *,
    secret: str,
    timestamp: str,
    nonce: str,
    short_uuid: str,
    client_ip: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    """Build the canonical HMAC shared with the subscription-page BFF."""
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f'{timestamp}\n{nonce}\n{short_uuid}\n{client_ip}\n{method.upper()}\n{path}\n{body_hash}'
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


async def verify_subpage_bff_request(request: Request) -> None:
    """Verify HMAC, timestamp, client identity and one-time nonce."""
    secret = settings.SUBPAGE_BFF_SECRET or ''
    if len(secret) < 32:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')

    timestamp = request.headers.get('x-subpage-timestamp', '')
    nonce = request.headers.get('x-subpage-nonce', '')
    short_uuid = request.headers.get('x-subpage-short-uuid', '')
    client_ip = request.headers.get('x-subpage-client-ip', '')
    signature = request.headers.get('x-subpage-signature', '').lower()

    if (
        not _TIMESTAMP_RE.fullmatch(timestamp)
        or not _NONCE_RE.fullmatch(nonce)
        or not _SHORT_UUID_RE.fullmatch(short_uuid)
        or not _SIGNATURE_RE.fullmatch(signature)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    try:
        ipaddress.ip_address(client_ip)
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized') from error

    if abs(int(time.time()) - timestamp_value) > _CLOCK_SKEW_SECONDS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    path_short_uuid = request.path_params.get('short_uuid')
    if path_short_uuid is not None and path_short_uuid != short_uuid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    raw_body = await request.body()
    expected = build_subpage_bff_signature(
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        short_uuid=short_uuid,
        client_ip=client_ip,
        method=request.method,
        path=request.url.path,
        body=raw_body,
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    nonce_digest = hashlib.sha256(nonce.encode()).hexdigest()
    nonce_stored = await cache.setnx(
        cache_key('subpage_bff_nonce', nonce_digest),
        1,
        expire=_NONCE_TTL_SECONDS,
    )
    if not nonce_stored:
        logger.warning('Subpage BFF nonce rejected')
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    request.state.subpage_client_ip = client_ip
    request.state.subpage_short_uuid = short_uuid
