from collections.abc import Iterable

import structlog
from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SentNotification


logger = structlog.get_logger(__name__)

NotificationKey = tuple[int, int, str, int | None]
NOTIFICATION_LOOKUP_BATCH_SIZE = 500


def _notification_key(notification: SentNotification) -> NotificationKey:
    return (
        notification.user_id,
        notification.subscription_id,
        notification.notification_type,
        notification.days_before,
    )


async def get_sent_notification_keys(
    db: AsyncSession,
    user_subscription_pairs: Iterable[tuple[int, int]],
    notification_types: Iterable[str],
) -> set[NotificationKey]:
    """Load only matching keys in bounded batches, including unflushed records.

    Pairs preserve ownership: separate user/subscription IN filters would also
    select historical rows belonging to another owner. NULL days remain NULL.
    This is a lookup optimization, not an exactly-once delivery guarantee.
    """
    pairs = set(user_subscription_pairs)
    types = set(notification_types)
    if not pairs or not types:
        return set()
    found = {
        _notification_key(item)
        for item in db.new
        if isinstance(item, SentNotification)
        and (item.user_id, item.subscription_id) in pairs
        and item.notification_type in types
    }
    ordered_pairs = sorted((subscription_id, user_id) for user_id, subscription_id in pairs)
    for offset in range(0, len(ordered_pairs), NOTIFICATION_LOOKUP_BATCH_SIZE):
        result = await db.execute(
            select(
                SentNotification.user_id,
                SentNotification.subscription_id,
                SentNotification.notification_type,
                SentNotification.days_before,
            ).where(
                tuple_(SentNotification.subscription_id, SentNotification.user_id).in_(
                    ordered_pairs[offset : offset + NOTIFICATION_LOOKUP_BATCH_SIZE]
                ),
                SentNotification.notification_type.in_(types),
            )
        )
        found.update(tuple(row) for row in result)
    return found


async def notification_sent(
    db: AsyncSession,
    user_id: int,
    subscription_id: int,
    notification_type: str,
    days_before: int | None = None,
) -> bool:
    key = (user_id, subscription_id, notification_type, days_before)
    if any(isinstance(item, SentNotification) and _notification_key(item) == key for item in db.new):
        return True
    return bool(
        await db.scalar(
            select(
                select(SentNotification.id)
                .where(
                    SentNotification.user_id == user_id,
                    SentNotification.subscription_id == subscription_id,
                    SentNotification.notification_type == notification_type,
                    SentNotification.days_before == days_before,
                )
                .exists()
            )
        )
    )


async def record_notification(
    db: AsyncSession,
    user_id: int,
    subscription_id: int,
    notification_type: str,
    days_before: int | None = None,
    *,
    commit: bool = True,
) -> None:
    already_exists = await notification_sent(db, user_id, subscription_id, notification_type, days_before)
    if already_exists:
        return
    notification = SentNotification(
        user_id=user_id,
        subscription_id=subscription_id,
        notification_type=notification_type,
        days_before=days_before,
    )
    db.add(notification)
    if commit:
        await db.commit()


async def clear_notifications(db: AsyncSession, subscription_id: int, *, commit: bool = True) -> None:
    await db.execute(delete(SentNotification).where(SentNotification.subscription_id == subscription_id))
    if commit:
        await db.commit()


async def clear_notification_by_type(
    db: AsyncSession,
    subscription_id: int,
    notification_type: str,
    *,
    commit: bool = True,
) -> None:
    await db.execute(
        delete(SentNotification).where(
            SentNotification.subscription_id == subscription_id,
            SentNotification.notification_type == notification_type,
        )
    )
    if commit:
        await db.commit()
