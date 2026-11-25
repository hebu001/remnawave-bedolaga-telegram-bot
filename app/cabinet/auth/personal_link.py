"""Permanent revocable cabinet links. Only the selector lives in PostgreSQL."""

import base64
import hmac
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User, UserStatus
from app.services.blacklist_service import blacklist_service
from app.services.maintenance_service import maintenance_service
from app.services.support_settings_service import SupportSettingsService

from .session_security import (
    auth_version,
    lock_auth_user,
    require_personal_link_copy_provenance,
    require_personal_link_rotation_proof,
    session_is_current,
)


_TOKEN = re.compile(r'v1\.([0-9a-f]{32})\.[A-Za-z0-9_-]{43}')
_SHORT_TOKEN = re.compile(r'[A-Za-z0-9_-]{22}')
_KEY = re.compile(r'[0-9a-fA-F]{64}')


def _configuration() -> tuple[str, bytes]:
    if not settings.CABINET_PERSONAL_LINK_ENABLED:
        raise HTTPException(status_code=404, detail='Personal links are unavailable')
    key = settings.CABINET_PERSONAL_LINK_SECRET or ''
    cabinet_url = (settings.CABINET_URL or '').strip().rstrip('/')
    try:
        url = urlsplit(cabinet_url)
        configured = (
            url.scheme == 'https'
            and url.hostname
            and url.hostname != 'example.com'
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and not any(c.isspace() or c == chr(92) for c in cabinet_url)
        )
    except ValueError:
        configured = False
    if not configured or not _KEY.fullmatch(key):
        raise HTTPException(status_code=503, detail='Personal links are unavailable')
    return cabinet_url, bytes.fromhex(key)


def personal_link_enabled() -> bool:
    try:
        _configuration()
    except HTTPException:
        return False
    return True


def personal_link_channel_exempt(user: User) -> bool:
    """Match ChannelCheckerMiddleware's Telegram staff exemption, without granting a role."""
    return user.telegram_id is not None and (
        settings.is_admin(user.telegram_id) or SupportSettingsService.is_moderator(user.telegram_id)
    )


async def require_personal_link_access(db: AsyncSession, user: User) -> None:
    _configuration()
    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(status_code=403, detail='Sign in using your usual method')
    if maintenance_service.is_maintenance_active():
        raise HTTPException(status_code=503, detail='Cabinet is under maintenance')
    if user.telegram_id is not None:
        blocked, _ = await blacklist_service.is_user_blacklisted(user.telegram_id, user.username)
        if blocked:
            raise HTTPException(status_code=403, detail='Account unavailable')
        if settings.CHANNEL_IS_REQUIRED_SUB and not personal_link_channel_exempt(user):
            from app.services.channel_subscription_service import channel_subscription_service

            channels = await channel_subscription_service.get_channels_with_status(user.telegram_id)
            missing = [ch for ch in channels if not ch['is_subscribed']]
            if missing:
                raise HTTPException(
                    status_code=403,
                    detail={
                        'code': 'required_channel_subscription_missing',
                        'channel_titles': [ch.get('title') for ch in missing],
                    },
                )


def _sign(user: User, cabinet_url: str, key: bytes) -> str:
    if user.cabinet_personal_link_key_version != 1 or not user.cabinet_personal_link_id:
        raise HTTPException(status_code=503, detail='Personal link unavailable')
    selector = user.cabinet_personal_link_id.hex
    message = f'cabinet-personal-link\x00v1\x00{cabinet_url}\x00{user.id}\x00{selector}'.encode()
    signature = base64.urlsafe_b64encode(hmac.digest(key, message, 'sha256')).rstrip(b'=').decode('ascii')
    return f'v1.{selector}.{signature}'


def personal_link_session_id(user: User) -> UUID:
    """Bind sessions to this link, its server key and its cabinet origin."""
    cabinet_url, key = _configuration()
    token = _sign(user, cabinet_url, key)
    return UUID(bytes=hmac.digest(key, b'cabinet-personal-session\x00' + token.encode(), 'sha256')[:16])


def _short_cipher(cabinet_url: str, key: bytes) -> Cipher:
    derived_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'cabinet-personal-short-link-hkdf-v1',
        info=b'cabinet-origin\x00' + cabinet_url.encode(),
    ).derive(key)
    # This is a keyed permutation of exactly one random 128-bit selector. It
    # encrypts no message or structured payload, so there are no repeated blocks
    # or padding. Only a current selector in the database grants access.
    return Cipher(algorithms.AES(derived_key), modes.ECB())  # noqa: S305


def _short_token(user: User, cabinet_url: str, key: bytes) -> str:
    if user.cabinet_personal_link_key_version != 1 or not user.cabinet_personal_link_id:
        raise HTTPException(status_code=503, detail='Personal link unavailable')
    encryptor = _short_cipher(cabinet_url, key).encryptor()
    encrypted = encryptor.update(user.cabinet_personal_link_id.bytes) + encryptor.finalize()
    return base64.urlsafe_b64encode(encrypted).rstrip(b'=').decode('ascii')


def _short_selector(token: str, cabinet_url: str, key: bytes) -> UUID | None:
    encrypted = base64.urlsafe_b64decode(token + '==')
    canonical = base64.urlsafe_b64encode(encrypted).rstrip(b'=').decode('ascii')
    if not hmac.compare_digest(token, canonical):
        return None
    decryptor = _short_cipher(cabinet_url, key).decryptor()
    return UUID(bytes=decryptor.update(encrypted) + decryptor.finalize())


async def get_personal_link(
    db: AsyncSession,
    user_id: int,
    *,
    auth_payload: dict | None = None,
    verified_telegram_id: int | None = None,
    rotate: bool = False,
) -> str:
    """Use after owner authentication; rotation requires separate recent proof."""
    cabinet_url, key = _configuration()
    user = await lock_auth_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail='Account unavailable')
    # An earlier HTTP dependency check can become stale while waiting for this
    # lock. Never reveal a replacement to a session revoked by that rotation.
    if auth_payload is not None and (
        auth_payload.get('sub') != str(user.id) or not await session_is_current(db, auth_payload, user)
    ):
        raise HTTPException(status_code=401, detail='Session revoked; sign in again')
    if rotate:
        require_personal_link_rotation_proof(auth_payload or {}, user)
    else:
        # A private Telegram callback proves current sender ownership directly,
        # independently of JWT generations. HTTP always keeps its original one.
        copy_payload = auth_payload if auth_payload is not None else {'auth_version': auth_version(user)}
        require_personal_link_copy_provenance(copy_payload, user, verified_telegram_id)
    await require_personal_link_access(db, user)
    if rotate:
        user.cabinet_personal_link_id = uuid4()
    token = _short_token(user, cabinet_url, key)
    await db.commit()
    return f'{cabinet_url}/s/{token}'


async def resolve_personal_link(db: AsyncSession, token: str) -> User:
    """Verify under the same user lock used by refresh storage and rotation."""
    cabinet_url, key = _configuration()
    match = _TOKEN.fullmatch(token)
    selector = None
    if match:
        selector = UUID(hex=match.group(1))
    elif _SHORT_TOKEN.fullmatch(token):
        selector = _short_selector(token, cabinet_url, key)
    if selector is not None:
        user = await db.scalar(
            select(User)
            .where(User.cabinet_personal_link_id == selector)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user:
            expected = _sign(user, cabinet_url, key) if match else _short_token(user, cabinet_url, key)
            # Also rejects non-canonical Base64 encodings of the same ciphertext.
            if hmac.compare_digest(token, expected):
                await require_personal_link_access(db, user)
                return user
    raise HTTPException(status_code=401, detail='Personal link is invalid or has been replaced')
