"""Credential generations shared by HTTP, refresh and WebSocket authentication."""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CabinetRefreshToken, CabinetWsTicket, User


def auth_version(user: User) -> int:
    return getattr(user, 'cabinet_auth_version', 0) or 0


def session_version_matches(payload: dict, user: User) -> bool:
    # Tokens from before migration 0104 remain usable only until the first
    # credential reset. Missing is generation zero, never the current version.
    version = payload.get('auth_version', 0)
    return type(version) is int and version >= 0 and version == auth_version(user)


async def lock_auth_user(db: AsyncSession, user_id: int) -> User | None:
    # All refresh/reset/store paths lock user BEFORE refresh records.
    return await db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )


async def revoke_password_sessions(db: AsyncSession, user: User) -> None:
    """Caller holds the user lock and commits password + revocation together."""
    user.cabinet_auth_version = auth_version(user) + 1
    await db.execute(
        update(CabinetRefreshToken)
        .where(CabinetRefreshToken.user_id == user.id, CabinetRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.execute(delete(CabinetWsTicket).where(CabinetWsTicket.user_id == user.id))
