"""Fresh 80/90/100% traffic alerts with durable, shared delivery reservations.

Telegram has no idempotency key: a committed reservation is kept on an ambiguous
network failure. This favours no duplicate messages over retrying an uncertain
send. Only an explicit Telegram flood rejection is safe to retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import structlog
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, TrafficNotificationState, User
from app.external.remnawave_api import UserStatus as PanelStatus
from app.localization.texts import get_texts
from app.services.notification_settings_service import NotificationSettingsService
from app.services.panel_sync import find_foreign_panel_owner
from app.services.remnawave_service import RemnaWaveService
from app.utils.cache import cache
from app.utils.notification_prefs import is_traffic_warning_enabled


logger = structlog.get_logger(__name__)
THRESHOLDS = (80, 90, 100)
GB = 1024**3


def reached_threshold(used_bytes: int, limit_bytes: int, limited: bool = False) -> int:
    if limit_bytes <= 0:
        return 0
    if limited:
        return 100
    return max((level for level in THRESHOLDS if used_bytes * 100 >= limit_bytes * level), default=0)


def panel_user_id(subscription):
    return subscription.remnawave_id if settings.is_multi_tariff_enabled() else subscription.user.remnawave_id


def legacy_panel_uuid(subscription):
    """Historical namespace only; never pass this UUID to the 3.x API."""
    return subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else subscription.user.remnawave_uuid


def traffic_cycle(panel, limit_bytes: int, *, legacy_uuid: str | None = None) -> str:
    reset = panel.last_traffic_reset_at
    if reset is not None:
        reset = reset.replace(tzinfo=UTC) if reset.tzinfo is None else reset.astimezone(UTC)
    identity = [legacy_uuid or panel.id, reset.isoformat() if reset else None, limit_bytes]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return digest if legacy_uuid else f'id:{digest[:61]}'


class TrafficNotificationService:
    def __init__(self, bot, *, session_factory=None, panel_service=None):
        self.bot = bot
        self.sessions = session_factory or AsyncSessionLocal
        self.panel = panel_service or RemnaWaveService()

    async def check_all(self):
        """Page panel counters once, not one remote request per subscription."""
        try:
            async with self.sessions() as db:
                rows = (
                    await db.execute(
                        select(
                            Subscription.id,
                            Subscription.remnawave_id,
                            User.remnawave_id,
                            Subscription.traffic_limit_gb,
                            TrafficNotificationState.subscription_id,
                        )
                        .join(User, User.id == Subscription.user_id)
                        .outerjoin(
                            TrafficNotificationState, TrafficNotificationState.subscription_id == Subscription.id
                        )
                        .where(
                            Subscription.status.in_(['active', 'trial', 'limited']),
                            Subscription.traffic_limit_gb > 0,
                            User.telegram_id.is_not(None),
                            User.status.notin_(['blocked', 'deleted']),
                        )
                    )
                ).all()
            targets = {}
            for sid, sub_id, user_id, limit, state_id in rows:
                identifier = sub_id if settings.is_multi_tariff_enabled() else user_id
                if identifier:
                    targets.setdefault(identifier, []).append((sid, limit * GB, state_id is not None))
            if not targets:
                return
            cursor = None
            seen = set()
            async with self.panel.get_api_client() as api:
                while targets:
                    page = await api.get_all_users_page_stream(cursor=cursor, size=1000)
                    observed_at = datetime.now(UTC)
                    for panel in page.get('users', []):
                        matches = targets.pop(panel.id, [])
                        if panel.user_traffic is None:
                            continue
                        for sid, limit, has_state in matches:
                            # Low-use accounts need no state until first crossing.
                            if has_state or reached_threshold(
                                panel.used_traffic_bytes, limit, panel.status == PanelStatus.LIMITED
                            ):
                                try:
                                    await self.process_sample(sid, panel, observed_at)
                                except Exception:
                                    logger.exception('Traffic notification processing failed', subscription_id=sid)
                    if not page.get('hasMore'):
                        break
                    cursor = page.get('nextCursor')
                    if not cursor or cursor in seen:
                        raise RuntimeError('Panel traffic cursor did not advance')
                    seen.add(cursor)
        except Exception:
            # Never fall back to stale database counters to send an alert.
            logger.exception('Fresh traffic notification scan failed')

    async def check_subscription(self, subscription_id: int):
        async with self.sessions() as db:
            sub = await db.scalar(
                select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
            )
            identifier = panel_user_id(sub) if sub and sub.user else None
        if not identifier:
            return
        # This path is used by signed webhooks; fetch authoritative current data
        # instead of trusting a delayed webhook's counter or threshold.
        async with self.panel.get_api_client() as api:
            panel = await api.get_user_by_id(identifier)
        if panel:
            await self.process_sample(subscription_id, panel, datetime.now(UTC))

    async def process_sample(self, subscription_id: int, panel, observed_at: datetime):
        if (
            not self.bot
            or panel.user_traffic is None
            or not NotificationSettingsService.are_notifications_globally_enabled()
        ):
            return False
        if panel.status not in (PanelStatus.ACTIVE, PanelStatus.LIMITED):
            return False
        used = panel.used_traffic_bytes
        if not isinstance(used, int) or used < 0:
            return False
        try:
            legacy_sent = bool(await cache.get(f'traffic_warn:{subscription_id}'))
        except Exception:
            legacy_sent = False  # Durable state does not depend on Redis.
        async with self.sessions() as db:
            # Lock the existing subscription even when its notification row does
            # not exist yet. Polls and webhooks cannot both reserve a crossing.
            sub = await db.scalar(
                select(Subscription)
                .options(selectinload(Subscription.user))
                .where(Subscription.id == subscription_id)
                .with_for_update()
            )
            if not sub or not sub.user or panel_user_id(sub) != panel.id:
                return False
            user = sub.user
            if (
                await find_foreign_panel_owner(db, user, sub, panel.id, multi_tariff=settings.is_multi_tariff_enabled())
                is not None
            ):
                logger.warning('Traffic sample belongs to another subscription; state retained', subscription_id=sub.id)
                return False
            if (
                sub.status not in ('active', 'trial', 'limited')
                or not user.telegram_id
                or user.status in ('blocked', 'deleted')
                or not is_traffic_warning_enabled(user)
            ):
                return False
            limit = (sub.traffic_limit_gb or 0) * GB
            if limit <= 0:
                return False
            level = reached_threshold(used, limit, panel.status == PanelStatus.LIMITED)
            state = await db.get(TrafficNotificationState, sub.id)
            identity_changed = state is not None and state.panel_user_id not in (None, panel.id)
            # Keep old UUID hashes on the first verified numeric binding. Once
            # an account is replaced, its numeric namespace stays authoritative
            # even if an inert historical UUID is still present on the row.
            legacy_uuid = legacy_panel_uuid(sub)
            numeric_namespace = identity_changed or (state is not None and state.cycle_key.startswith('id:'))
            if state and not numeric_namespace and not legacy_uuid:
                logger.warning('Traffic cycle identity unresolved; state retained', subscription_id=sub.id)
                return False
            cycle = traffic_cycle(panel, limit, legacy_uuid=None if numeric_namespace else legacy_uuid)
            if state and observed_at <= state.observed_at:
                return False
            if state is None:
                # Carry forward the old 24h marker where it still exists.
                previous = reached_threshold(int((sub.traffic_used_gb or 0) * GB), limit) if legacy_sent else 0
                state = TrafficNotificationState(
                    subscription_id=sub.id,
                    panel_user_id=panel.id,
                    cycle_key=cycle,
                    generation=0,
                    highest_threshold=min(previous, level),
                    used_bytes=used,
                    observed_at=observed_at,
                    delivery_status='observed',
                )
                db.add(state)
            elif (
                identity_changed
                or state.cycle_key != cycle
                or (panel.last_traffic_reset_at is None and used < state.used_bytes)
            ):
                state.cycle_key = cycle
                state.generation += 1
                state.highest_threshold = 0
                state.delivery_status = 'observed'
                state.next_attempt_at = None
            state.panel_user_id = panel.id
            state.used_bytes = used
            state.observed_at = observed_at
            sub.traffic_used_gb = used / GB
            previous = state.highest_threshold
            if level <= previous or (state.next_attempt_at and observed_at < state.next_attempt_at):
                await db.commit()
                return False
            state.highest_threshold = level
            state.delivery_status = 'reserved'
            state.next_attempt_at = None
            generation = state.generation
            texts = get_texts(user.language or 'ru')
            key = 'TRAFFIC_LIMIT_REACHED_ALERT' if level == 100 else 'TRAFFIC_THRESHOLD_ALERT'
            default = (
                '⛔ <b>Лимит трафика исчерпан</b>\n\nИспользовано: {used:.1f} / {limit} ГБ ({percent:.0f}%).'
                if level == 100
                else '⚠️ <b>Использовано {threshold}% трафика</b>\n\n'
                'Использовано: {used:.1f} / {limit} ГБ ({percent:.0f}%).'
            )
            message = texts.get(key, default).format(
                used=used / GB, limit=sub.traffic_limit_gb, percent=used * 100 / limit, threshold=level
            )
            telegram_id = user.telegram_id
            await db.commit()  # Durable reservation BEFORE the external effect.
        result = 'sent'
        retry_at = None
        try:
            await self.bot.send_message(telegram_id, message, parse_mode='HTML')
        except TelegramRetryAfter as error:
            result = 'retry'
            retry_at = datetime.now(UTC) + timedelta(seconds=error.retry_after)
        except Exception as error:
            result = 'uncertain'
            logger.warning(
                'Traffic alert delivery failed; reservation retained',
                subscription_id=subscription_id,
                threshold=level,
                error_type=type(error).__name__,
            )
        async with self.sessions() as db:
            state = await db.scalar(
                select(TrafficNotificationState)
                .where(TrafficNotificationState.subscription_id == subscription_id)
                .with_for_update()
            )
            if state and (state.cycle_key, state.generation, state.highest_threshold) == (cycle, generation, level):
                state.delivery_status = result
                state.next_attempt_at = retry_at
                if retry_at:
                    state.highest_threshold = previous
                await db.commit()
        if result == 'sent':
            await asyncio.sleep(0.1)
        return result == 'sent'
