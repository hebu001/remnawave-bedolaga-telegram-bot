"""Durable panel synchronization for paid renewals.

The intent is written in the money transaction. One row per subscription merges
flags; a version prevents a running worker from acknowledging a newer renewal.
Provider calls are at-least-once, since the panel has no idempotent reset API.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import RenewalSyncTask
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)
REMNAWAVE_SYNC_TIMEOUT = 10.0
_LOCK_NAMESPACE = 5478736


class RenewalSyncUnavailable(Exception):
    """A stable, non-sensitive reason suitable for the persisted retry record."""


async def schedule_renewal_sync(
    db: AsyncSession,
    subscription_id: int,
    *,
    reset_traffic: bool,
    reset_devices: bool,
    sync_squads: bool = True,
) -> None:
    """No commit or external calls: the caller owns the money transaction."""
    now = datetime.now(UTC)
    statement = insert(RenewalSyncTask).values(
        subscription_id=subscription_id,
        version=1,
        status='pending',
        reset_traffic=reset_traffic,
        reset_devices=reset_devices,
        sync_squads=sync_squads,
        attempts=0,
        next_attempt_at=now,
        updated_at=now,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[RenewalSyncTask.subscription_id],
            set_={
                'version': RenewalSyncTask.version + 1,
                'status': 'pending',
                'reset_traffic': or_(RenewalSyncTask.reset_traffic, statement.excluded.reset_traffic),
                'reset_devices': or_(RenewalSyncTask.reset_devices, statement.excluded.reset_devices),
                'sync_squads': or_(RenewalSyncTask.sync_squads, statement.excluded.sync_squads),
                'attempts': 0,
                'last_error': None,
                'next_attempt_at': now,
                'updated_at': now,
            },
        )
    )


async def process_renewal_sync(subscription_id: int, *, session_factory=None, force: bool = False) -> bool:
    factory = session_factory or AsyncSessionLocal
    # A separate transaction holds the advisory lock while panel helpers commit
    # their own session. A process crash releases the lock automatically.
    async with factory() as guard:
        acquired = await guard.scalar(
            text('SELECT pg_try_advisory_xact_lock(:namespace, :subscription_id)'),
            {'namespace': _LOCK_NAMESPACE, 'subscription_id': subscription_id},
        )
        if not acquired:
            return False
        async with factory() as db:
            job = await db.scalar(
                select(RenewalSyncTask)
                .where(
                    RenewalSyncTask.subscription_id == subscription_id,
                )
                .with_for_update()
            )
            if job is None or job.status == 'done':
                return True
            now = datetime.now(UTC)
            if not force and job.next_attempt_at > now:
                return False
            version = job.version
            reset_traffic, reset_devices, sync_squads = job.reset_traffic, job.reset_devices, job.sync_squads
            attempt = job.attempts + 1
            job.attempts = attempt
            job.next_attempt_at = now + timedelta(seconds=60)
            await db.commit()

            error_name = None
            try:
                from app.database.crud.subscription import get_subscription_by_id

                sub = await get_subscription_by_id(db, subscription_id)
                if sub is None:
                    return True  # ON DELETE CASCADE removes its obsolete intent.
                service = SubscriptionService()
                async with asyncio.timeout(REMNAWAVE_SYNC_TIMEOUT):
                    should_create = (
                        not sub.remnawave_uuid if settings.is_multi_tariff_enabled() else not sub.user.remnawave_uuid
                    )
                    if should_create:
                        result = await service.create_remnawave_user(
                            db,
                            sub,
                            reset_traffic=False,
                            reset_reason='subscription renewal',
                        )
                    else:
                        result = await service.update_remnawave_user(
                            db,
                            sub,
                            reset_traffic=False,
                            reset_reason='subscription renewal',
                            sync_squads=sync_squads,
                        )
                    if result is None:
                        raise RenewalSyncUnavailable('panel_returned_none')
                    if reset_traffic or reset_devices:
                        from app.services.remnawave_service import RemnaWaveService

                        await db.refresh(sub)
                        await db.refresh(sub, ['user'])
                        panel_uuid = (
                            sub.remnawave_uuid if settings.is_multi_tariff_enabled() else sub.user.remnawave_uuid
                        )
                        if not panel_uuid:
                            raise RenewalSyncUnavailable('panel_uuid_missing')
                        async with RemnaWaveService().get_api_client() as api:
                            # SubscriptionService's reset helper is best effort
                            # and swallows failures. Durable intents require an
                            # explicit acknowledgement for every requested step.
                            if reset_traffic and not await api.reset_user_traffic(panel_uuid):
                                raise RenewalSyncUnavailable('traffic_reset_failed')
                            if reset_devices and not await api.reset_user_devices(panel_uuid):
                                raise RenewalSyncUnavailable('device_reset_failed')
            except asyncio.CancelledError:
                await db.rollback()
                raise  # Persisted intent remains pending for another worker.
            except Exception as error:
                error_name = str(error) if isinstance(error, RenewalSyncUnavailable) else type(error).__name__
                await db.rollback()

            job = await db.scalar(
                select(RenewalSyncTask)
                .where(
                    RenewalSyncTask.subscription_id == subscription_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None:
                return True
            if job.version == version:
                job.last_error = error_name
                if error_name is None:
                    job.status = 'done'
                    job.reset_traffic = job.reset_devices = job.sync_squads = False
                else:
                    job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2 ** min(attempt, 7)))
            # If a new renewal arrived during the call, its merged flags and due
            # time remain pending. A successful old version must not erase it.
            job.updated_at = datetime.now(UTC)
            await db.commit()
            if error_name:
                logger.warning(
                    'Renewal panel sync deferred',
                    subscription_id=subscription_id,
                    attempt=attempt,
                    error_type=error_name,
                )
            return error_name is None


async def process_pending_renewal_syncs(*, session_factory=None, limit: int = 20) -> None:
    factory = session_factory or AsyncSessionLocal
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(RenewalSyncTask.subscription_id)
                    .where(
                        RenewalSyncTask.status == 'pending',
                        RenewalSyncTask.next_attempt_at <= datetime.now(UTC),
                    )
                    .order_by(RenewalSyncTask.next_attempt_at)
                    .limit(limit)
                )
            ).all()
        )
    for subscription_id in ids:
        try:
            await process_renewal_sync(subscription_id, session_factory=factory)
        except Exception as error:
            logger.error('Renewal sync worker failed', subscription_id=subscription_id, error_type=type(error).__name__)
