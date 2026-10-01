"""Cabinet notifications with one-use tickets and continuously checked sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.jwt_handler import get_token_payload
from app.cabinet.auth.ws_tickets import TICKET_TTL_SECONDS, consume_ws_ticket, issue_ws_ticket
from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user, security
from app.cabinet.ip_utils import get_client_ip
from app.cabinet.ws_manager import (  # noqa: F401 -- compatibility reexports for route callers
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_USER,
    SEND_TIMEOUT_SECONDS,
    CabinetConnectionManager,
    CabinetWsSession,
    PermissionService,
    cabinet_ws_manager,
)
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.utils.websocket_errors import CLIENT_GONE_ERRORS, is_client_gone


logger = structlog.get_logger(__name__)
router = APIRouter()
SESSION_CHECK_SECONDS = 30.0
MAX_MESSAGE_BYTES = 4096


@router.post('/ws/ticket')
async def create_cabinet_ws_ticket(
    request: Request,
    response: Response,
    user: User = Depends(get_current_cabinet_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_cabinet_db),
):
    payload = get_token_payload(credentials.credentials, expected_type='access')
    if not payload or payload.get('sub') != str(user.id):
        raise HTTPException(status_code=401, detail='Invalid access token')
    ticket = await issue_ws_ticket(db, payload, request.headers.get('origin', ''))
    response.headers['Cache-Control'] = 'no-store'
    return {'ticket': ticket, 'expires_in': TICKET_TTL_SECONDS}


async def _reject(websocket: WebSocket, reason: str) -> None:
    """Принять и сразу закрыть соединение с кодом отказа.

    Клиент может отвалиться и здесь — тогда закрывать уже нечего и некому.
    """
    with contextlib.suppress(*CLIENT_GONE_ERRORS):
        await websocket.accept()
        await websocket.close(code=1008, reason=reason)


@router.websocket('/ws')
async def cabinet_websocket_endpoint(websocket: WebSocket):
    # Long-lived bearer credentials are deliberately not accepted in a URL.
    # Client deployment must switch to POST /ws/ticket before this rollout.
    ticket = websocket.query_params.get('ticket', '')
    session = None
    try:
        if 'token' in websocket.query_params or not ticket:
            await websocket.close(code=1008, reason='WebSocket ticket required')
            return
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            async with AsyncSessionLocal() as db:
                payload = await consume_ws_ticket(db, ticket, websocket.headers.get('origin', ''))
            if payload is None:
                await websocket.close(code=1008, reason='Invalid WebSocket ticket')
                return
            session = CabinetWsSession(websocket, payload, client_ip=get_client_ip(websocket))
            if not await cabinet_ws_manager.validate(session):
                await websocket.close(code=1008, reason='Session revoked or expired')
                return
        if not await cabinet_ws_manager.connect(session):
            await websocket.close(code=1013, reason='Too many connections')
            return
        await websocket.accept()
        if not await cabinet_ws_manager.send(
            session,
            {
                'type': 'connected',
                'user_id': session.user_id,
                'is_admin': session.is_admin,
            },
        ):
            return
        next_check = time.monotonic() + SESSION_CHECK_SECONDS
        while session.seconds_left > 0:
            if time.monotonic() >= next_check:
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    if not await cabinet_ws_manager.validate(session):
                        break
                next_check = time.monotonic() + SESSION_CHECK_SECONDS
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=min(max(0.001, next_check - time.monotonic()), session.seconds_left),
                )
            except TimeoutError:
                continue
            if len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
                break
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get('type') == 'ping':
                if not await cabinet_ws_manager.send(session, {'type': 'pong'}):
                    return
    except WebSocketDisconnect:
        pass
    except Exception as error:
        # Never include request URLs, tickets or JWTs in application logs.
        if is_client_gone(error):
            logger.debug('Cabinet WS ended', error_type=type(error).__name__, client_gone=True)
        else:
            logger.error('Cabinet WS failed', error_type=type(error).__name__)
    finally:
        if session is not None:
            await cabinet_ws_manager.close(session)


# Функции для отправки уведомлений (используются из других модулей)
async def notify_user_ticket_reply(user_id: int, ticket_id: int, message: str) -> None:
    """Уведомить пользователя об ответе в тикете."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'ticket.admin_reply',
            'ticket_id': ticket_id,
            'message': message,
        },
    )


async def notify_admins_new_ticket(ticket_id: int, title: str, user_id: int) -> None:
    """Уведомить админов о новом тикете."""
    await cabinet_ws_manager.send_to_admins(
        {
            'type': 'ticket.new',
            'ticket_id': ticket_id,
            'title': title,
            'user_id': user_id,
        }
    )


async def notify_admins_ticket_reply(ticket_id: int, message: str, user_id: int) -> None:
    """Уведомить админов об ответе пользователя."""
    await cabinet_ws_manager.send_to_admins(
        {
            'type': 'ticket.user_reply',
            'ticket_id': ticket_id,
            'message': message,
            'user_id': user_id,
        }
    )


# ============================================================================
# Уведомления о балансе
# ============================================================================


async def notify_user_balance_topup(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
    description: str = '',
) -> None:
    """Уведомить пользователя о пополнении баланса."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'balance.topup',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
            'description': description,
        },
    )


async def notify_user_balance_change(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
    description: str = '',
) -> None:
    """Уведомить пользователя об изменении баланса."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'balance.change',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
            'description': description,
        },
    )


# ============================================================================
# Уведомления о подписке
# ============================================================================


def _iso_utc(value: datetime | str | None) -> str:
    """Дата для WebSocket-события — ISO 8601 в UTC; кабинет форматирует её для человека сам.

    Раньше сюда прилетала строка ``format_email_datetime`` («27.11.2030, 12:00»),
    и кабинет показывал «Действует до: Invalid Date». Строка допускается только
    как уже готовый ISO (обратная совместимость), ``None`` — пустая строка.
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


async def notify_user_subscription_activated(
    user_id: int,
    subscription_id: int | None = None,
    expires_at: datetime | str | None = None,
    tariff_name: str = '',
) -> None:
    """Уведомить пользователя об активации подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.activated',
            'subscription_id': subscription_id,
            'expires_at': _iso_utc(expires_at),
            'tariff_name': tariff_name,
        },
    )


async def notify_user_subscription_expiring(
    user_id: int,
    days_left: int,
    expires_at: datetime | str | None,
) -> None:
    """Уведомить пользователя о скором истечении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.expiring',
            'days_left': days_left,
            'expires_at': _iso_utc(expires_at),
        },
    )


async def notify_user_subscription_expired(user_id: int) -> None:
    """Уведомить пользователя об истечении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.expired',
        },
    )


async def notify_user_subscription_renewed(
    user_id: int,
    subscription_id: int | None = None,
    new_expires_at: datetime | str | None = None,
    amount_kopeks: int = 0,
) -> None:
    """Уведомить пользователя о продлении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.renewed',
            'subscription_id': subscription_id,
            'new_expires_at': _iso_utc(new_expires_at),
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


async def notify_user_devices_purchased(
    user_id: int,
    devices_added: int,
    new_device_limit: int,
    amount_kopeks: int,
) -> None:
    """Уведомить пользователя о покупке устройств."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.devices_purchased',
            'devices_added': devices_added,
            'new_device_limit': new_device_limit,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


async def notify_user_traffic_purchased(
    user_id: int,
    traffic_gb_added: int,
    new_traffic_limit_gb: int,
    amount_kopeks: int,
) -> None:
    """Уведомить пользователя о покупке трафика."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.traffic_purchased',
            'traffic_gb_added': traffic_gb_added,
            'new_traffic_limit_gb': new_traffic_limit_gb,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


# ============================================================================
# Уведомления об автопродлении
# ============================================================================


async def notify_user_autopay_success(
    user_id: int,
    amount_kopeks: int,
    new_expires_at: datetime | str | None,
) -> None:
    """Уведомить пользователя об успешном автопродлении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.success',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_expires_at': _iso_utc(new_expires_at),
        },
    )


async def notify_user_autopay_failed(
    user_id: int,
    reason: str = '',
) -> None:
    """Уведомить пользователя о неудачном автопродлении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.failed',
            'reason': reason,
        },
    )


async def notify_user_autopay_insufficient_funds(
    user_id: int,
    required_kopeks: int,
    balance_kopeks: int,
) -> None:
    """Уведомить о недостатке средств для автопродления."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.insufficient_funds',
            'required_kopeks': required_kopeks,
            'required_rubles': required_kopeks / 100,
            'balance_kopeks': balance_kopeks,
            'balance_rubles': balance_kopeks / 100,
        },
    )


# ============================================================================
# Уведомления о бане/разбане
# ============================================================================


async def notify_user_ban(user_id: int, reason: str = '') -> None:
    """Уведомить пользователя о блокировке."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.banned',
            'reason': reason,
        },
    )


async def notify_user_unban(user_id: int) -> None:
    """Уведомить пользователя о разблокировке."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.unbanned',
        },
    )


async def notify_user_warning(user_id: int, message: str) -> None:
    """Уведомить пользователя о предупреждении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.warning',
            'message': message,
        },
    )


# ============================================================================
# Уведомления о рефералах
# ============================================================================


async def notify_user_referral_bonus(
    user_id: int,
    bonus_kopeks: int,
    referral_name: str = '',
) -> None:
    """Уведомить пользователя о реферальном бонусе."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'referral.bonus',
            'bonus_kopeks': bonus_kopeks,
            'bonus_rubles': bonus_kopeks / 100,
            'referral_name': referral_name,
        },
    )


async def notify_user_referral_registered(
    user_id: int,
    referral_name: str = '',
) -> None:
    """Уведомить пользователя о регистрации нового реферала."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'referral.registered',
            'referral_name': referral_name,
        },
    )


# ============================================================================
# Прочие уведомления
# ============================================================================


async def notify_user_daily_debit(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
) -> None:
    """Уведомить о ежедневном списании."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.daily_debit',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
        },
    )


async def notify_user_traffic_reset(user_id: int) -> None:
    """Уведомить о сбросе трафика."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.traffic_reset',
        },
    )


async def notify_user_payment_received(
    user_id: int,
    amount_kopeks: int,
    payment_method: str = '',
) -> None:
    """Уведомить о полученном платеже."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'payment.received',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'payment_method': payment_method,
        },
    )
