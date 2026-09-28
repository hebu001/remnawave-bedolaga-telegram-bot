"""Retain the numeric owner of pre-upgrade traffic delivery history.

Call before clearing/replacing a verified local identity, in the same transaction.
No markers or reservations are discarded. A later authoritative sample of the
replacement account opens the new cycle, including when no poll ran since backfill.
"""

from sqlalchemy import select, update

from app.database.models import Subscription, TrafficNotificationState, User


async def remember_previous_traffic_identity(db, subscription_id: int, panel_user_id: int | None) -> None:
    if db is None or not panel_user_id:
        return
    # Paid renewals lock User then Subscription. Polls only lock Subscription
    # then delivery state. Match both orders without refreshing pending fields.
    owner_id = select(Subscription.user_id).where(Subscription.id == subscription_id).scalar_subquery()
    await db.execute(select(User.id).where(User.id == owner_id).with_for_update())
    await db.execute(select(Subscription.id).where(Subscription.id == subscription_id).with_for_update())
    await db.execute(
        update(TrafficNotificationState)
        .where(
            TrafficNotificationState.subscription_id == subscription_id,
            TrafficNotificationState.panel_user_id.is_(None),
        )
        .values(panel_user_id=panel_user_id)
    )
