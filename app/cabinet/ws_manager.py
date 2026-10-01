"""One notification manager for cabinet routes and upstream service callers.

Sessions use single-use tickets at the route boundary. Every send rechecks expiry,
auth generation and current permissions; imports here avoid routes/payment cycles.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import structlog
from fastapi import WebSocket
from sqlalchemy import select

from app.cabinet.auth.session_security import session_version_matches
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.permission_service import PermissionService


logger = structlog.get_logger(__name__)
SEND_TIMEOUT_SECONDS = 3.0
MAX_CONNECTIONS_PER_USER = 5
MAX_CONNECTIONS = 1000


@dataclass(eq=False)
class CabinetWsSession:
    websocket: WebSocket
    payload: dict
    is_admin: bool = False
    client_ip: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def user_id(self) -> int:
        return int(self.payload['sub'])

    @property
    def seconds_left(self) -> float:
        return self.payload['exp'] - time.time()


class CabinetConnectionManager:
    def __init__(self, *, session_factory=None):
        self._sessions: set[CabinetWsSession] = set()
        self._lock = asyncio.Lock()
        self._session_factory = session_factory

    async def validate(self, session: CabinetWsSession) -> bool:
        if session.seconds_left <= 0:
            return False
        factory = self._session_factory or AsyncSessionLocal
        async with factory() as db:
            user = await db.scalar(select(User).where(User.id == session.user_id))
            if not user or user.status != 'active' or not session_version_matches(session.payload, user):
                return False
            # Evaluate current RBAC/ABAC and trusted legacy-admin configuration;
            # roles embedded in the original JWT never authorize a broadcast.
            was_admin = session.is_admin
            session.is_admin, _ = await PermissionService.check_permission(
                db, user, 'tickets:read', ip_address=session.client_ip
            )
            if was_admin and not session.is_admin:
                return False  # Reconnect as an ordinary user after a role downgrade.
        return session.seconds_left > 0

    async def connect(self, session: CabinetWsSession) -> bool:
        async with self._lock:
            per_user = sum(item.user_id == session.user_id for item in self._sessions)
            if len(self._sessions) >= MAX_CONNECTIONS or per_user >= MAX_CONNECTIONS_PER_USER:
                return False
            self._sessions.add(session)
        return True

    async def disconnect(self, session: CabinetWsSession) -> None:
        async with self._lock:
            self._sessions.discard(session)

    async def close(self, session: CabinetWsSession, code: int = 1008) -> None:
        await self.disconnect(session)
        try:
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                async with session.send_lock:
                    await session.websocket.close(code=code, reason='Session ended')
        except Exception:
            pass

    async def send(self, session: CabinetWsSession, message: dict, *, admin_only: bool = False) -> bool:
        try:
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                async with session.send_lock:
                    if not await self.validate(session):
                        raise PermissionError('Session revoked or expired')
                    if admin_only and not session.is_admin:
                        return False
                    await session.websocket.send_text(json.dumps(message, default=str, ensure_ascii=False))
            return True
        except Exception as error:
            logger.debug('Cabinet WS send stopped', user_id=session.user_id, error_type=type(error).__name__)
            await self.close(session)
            return False

    async def send_to_user(self, user_id: int, message: dict) -> None:
        async with self._lock:
            sessions = [session for session in self._sessions if session.user_id == user_id]
        await asyncio.gather(*(self.send(session, message) for session in sessions))

    async def send_to_admins(self, message: dict) -> None:
        async with self._lock:
            sessions = [session for session in self._sessions if session.is_admin]
        # Bound task fan-out so slow recipients cannot occupy all DB connections.
        for offset in range(0, len(sessions), 20):
            await asyncio.gather(
                *(self.send(session, message, admin_only=True) for session in sessions[offset : offset + 20])
            )


cabinet_ws_manager = CabinetConnectionManager()
