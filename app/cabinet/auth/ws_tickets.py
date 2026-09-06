"""Short-lived, single-use browser WebSocket capabilities bound to an origin."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.session_security import lock_auth_user, session_version_matches
from app.config import settings
from app.database.models import CabinetWsTicket


TICKET_TTL_SECONDS = 30
MAX_PENDING_TICKETS_PER_USER = 10


def canonical_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ':' in host:
            host = f'[{host}]'
        suffix = f':{port}' if port and port != (443 if parsed.scheme == 'https' else 80) else ''
        return f'{parsed.scheme}://{host}{suffix}'
    except ValueError:
        return None


def allowed_ws_origin(value: str) -> str | None:
    origin = canonical_origin(value)
    base = urlsplit(settings.CABINET_URL)
    configured = [f'{base.scheme}://{base.netloc}', *settings.get_cabinet_allowed_origins()]
    # A wildcard CORS setting is not a WebSocket origin policy.
    allowed = {canonical_origin(item) for item in configured if item != '*'}
    return origin if origin and origin in allowed else None


async def issue_ws_ticket(db: AsyncSession, payload: dict, origin: str) -> str:
    origin = allowed_ws_origin(origin)
    if not origin:
        raise HTTPException(status_code=403, detail='WebSocket origin is not allowed')
    expires = payload.get('exp')
    now = datetime.now(UTC)
    if type(expires) not in (int, float) or expires <= now.timestamp():
        raise HTTPException(status_code=401, detail='Access token expired')
    user = await lock_auth_user(db, int(payload['sub']))
    if not user or user.status != 'active' or not session_version_matches(payload, user):
        raise HTTPException(status_code=401, detail='Session revoked')
    await db.execute(delete(CabinetWsTicket).where(CabinetWsTicket.expires_at <= now))
    pending = await db.scalar(
        select(func.count()).select_from(CabinetWsTicket).where(CabinetWsTicket.user_id == user.id)
    )
    if pending >= MAX_PENDING_TICKETS_PER_USER:
        raise HTTPException(status_code=429, detail='Too many pending WebSocket tickets')
    ticket = secrets.token_urlsafe(32)
    access_expires_at = datetime.fromtimestamp(expires, UTC)
    db.add(
        CabinetWsTicket(
            token_hash=hashlib.sha256(ticket.encode()).hexdigest(),
            user_id=user.id,
            auth_version=payload.get('auth_version', 0),
            origin=origin,
            expires_at=min(now + timedelta(seconds=TICKET_TTL_SECONDS), access_expires_at),
            access_expires_at=access_expires_at,
        )
    )
    await db.commit()
    return ticket


async def consume_ws_ticket(db: AsyncSession, ticket: str, origin: str) -> dict | None:
    origin = allowed_ws_origin(origin)
    if not origin or len(ticket) != 43:
        return None
    result = await db.execute(
        delete(CabinetWsTicket)
        .where(
            CabinetWsTicket.token_hash == hashlib.sha256(ticket.encode()).hexdigest(),
            CabinetWsTicket.origin == origin,
            CabinetWsTicket.expires_at > datetime.now(UTC),
        )
        .returning(CabinetWsTicket.user_id, CabinetWsTicket.auth_version, CabinetWsTicket.access_expires_at)
    )
    row = result.first()
    await db.commit()
    if row is None:
        return None
    return {'sub': str(row.user_id), 'auth_version': row.auth_version, 'exp': row.access_expires_at.timestamp()}
