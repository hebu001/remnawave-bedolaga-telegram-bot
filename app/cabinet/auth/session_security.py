"""Credential generations shared by HTTP, refresh and WebSocket authentication."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CabinetRefreshToken, CabinetWsTicket, User


def auth_version(user: User) -> int:
    return getattr(user, 'cabinet_auth_version', 0) or 0


def auth_proof_identity(user: User, proof: str | None) -> str | None:
    """Bind a verified login proof to the identity still attached to this account."""
    if proof == 'email' and user.email and user.email_verified:
        identity = user.email.strip().lower()
    elif proof == 'telegram' and user.telegram_id is not None:
        identity = str(user.telegram_id)
    else:
        return None
    return hashlib.sha256(f'{proof}\0{identity}'.encode()).hexdigest()


def session_version_matches(payload: dict, user: User) -> bool:
    # Tokens from before migration 0104 remain usable only until the first
    # credential reset. Missing is generation zero, never the current version.
    version = payload.get('auth_version', 0)
    return type(version) is int and version >= 0 and version == auth_version(user)


def require_personal_link_copy_provenance(payload: dict, user: User, verified_telegram_id: int | None = None) -> None:
    """Recheck owner provenance using the same locked user whose link is returned."""
    if not session_version_matches(payload, user):
        raise HTTPException(status_code=401, detail='Session revoked; sign in again')
    method = payload.get('auth_method')
    if method in ('primary', 'personal_link'):
        return
    if method is None and type(verified_telegram_id) is int and verified_telegram_id == user.telegram_id:
        return
    raise HTTPException(
        status_code=403,
        detail={
            'code': 'primary_login_required',
            'message': 'Чтобы получить личную ссылку, войдите обычным способом или откройте бота в Telegram.',
        },
    )


def require_personal_link_rotation_proof(payload: dict, user: User) -> None:
    """A refresh preserves proof age; identity changes invalidate the original proof."""
    if not session_version_matches(payload, user):
        raise HTTPException(status_code=401, detail='Session revoked; sign in again')
    proof = payload.get('auth_proof')
    authenticated_at = payload.get('auth_time')
    now = int(datetime.now(UTC).timestamp())
    identity = auth_proof_identity(user, proof)
    if (
        payload.get('auth_method') == 'primary'
        and identity is not None
        and payload.get('auth_identity') == identity
        and type(authenticated_at) is int
        and 0 <= now - authenticated_at <= 600
    ):
        return
    raise HTTPException(
        status_code=403,
        detail={
            'code': 'personal_link_reauthentication_required',
            'message': (
                'Чтобы заменить ссылку, подтвердите вход через Telegram или подтверждённую почту. '
                'Если вы входите через другой сервис, сначала привяжите Telegram или подтвердите почту '
                'в настройках после обычного входа.'
            ),
            'allowed_proofs': ['email', 'telegram'],
            'max_age_seconds': 600,
        },
    )


async def session_is_current(db: AsyncSession, payload: dict, user: User) -> bool:
    """Validate credential generation and, for link logins, the specific revocable link."""
    if not session_version_matches(payload, user):
        return False
    if payload.get('auth_method') != 'personal_link':
        return 'personal_link_id' not in payload
    from app.cabinet.auth.personal_link import personal_link_session_id, require_personal_link_access

    try:
        if payload.get('personal_link_id') != str(personal_link_session_id(user)):
            return False
        await require_personal_link_access(db, user)
    except HTTPException:
        return False
    return True


async def lock_auth_user(db: AsyncSession, user_id: int) -> User | None:
    # All refresh/reset/store paths lock user BEFORE refresh records.
    return await db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )


async def revoke_password_sessions(db: AsyncSession, user: User) -> None:
    """Caller holds the user lock and commits password + revocation together."""
    user.cabinet_personal_link_id = uuid4()
    user.cabinet_auth_version = auth_version(user) + 1
    await db.execute(
        update(CabinetRefreshToken)
        .where(CabinetRefreshToken.user_id == user.id, CabinetRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.execute(delete(CabinetWsTicket).where(CabinetWsTicket.user_id == user.id))
