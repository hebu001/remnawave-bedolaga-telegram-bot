"""Correlate confirmed panel traffic resets with a committed renewal.

Short-lived, one-shot context affects notification wording only. Missing Redis,
unknown/old events, or mismatched subscription dates retain the generic notice.
"""

from datetime import UTC, datetime, timedelta

from app.utils.cache import cache


RENEWAL_REASONS = frozenset(
    {
        'продление подписки',
        'продление тарифа',
        'автопродление истёкшей подписки',
        'subscription renewal',
    }
)
NOTICE_TTL = 180


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_tariff_renewal(subscription, tariff_id):
    """Call before purchase mutates the existing subscription/trial flag."""
    return bool(subscription and not subscription.is_trial and subscription.tariff_id == tariff_id)


def _key(panel_user_id):
    return f'notification:renewal-reset:{panel_user_id}'


async def reset_traffic_with_notice(api, panel_user_id, subscription, *, renewal=False):
    """Preserve the reset API result/errors; never send a notification ourselves."""
    key = _key(panel_user_id)
    await cache.delete(key)
    now = datetime.now(UTC)
    end = _date(subscription.end_date) if subscription is not None else None
    marked = False
    if renewal and subscription is not None and not subscription.is_trial and end and end > now:
        marked = await cache.set(
            key,
            {
                'subscription_id': subscription.id,
                'user_id': subscription.user_id,
                'end_date': end.isoformat(),
                'started_at': now.isoformat(),
            },
            expire=NOTICE_TTL,
        )
    try:
        result = await api.reset_user_traffic(panel_user_id)
    except BaseException:
        if marked:
            await cache.delete(key)
        raise
    if not result and marked:
        await cache.delete(key)
    return result


async def consume_renewal_reset(subscription, data):
    """Accept only a fresh reset for the same panel account, owner and paid term."""
    panel_id = data.get('id')
    if not isinstance(panel_id, int) or isinstance(panel_id, bool) or panel_id <= 0:
        return None
    context = await cache.get(_key(panel_id))
    if not isinstance(context, dict):
        return None
    now = datetime.now(UTC)
    start = _date(context.get('started_at'))
    end = _date(context.get('end_date'))
    reset_at = _date(data.get('lastTrafficResetAt'))
    panel_end = _date(data.get('expireAt'))
    actual_end = _date(subscription.end_date)
    if (
        context.get('subscription_id') != subscription.id
        or context.get('user_id') != subscription.user_id
        or subscription.is_trial
        or not all((start, end, reset_at, panel_end, actual_end))
    ):
        return None
    if not (
        start <= now <= start + timedelta(seconds=NOTICE_TTL)
        and start - timedelta(seconds=2) <= reset_at <= now + timedelta(seconds=2)
        and end == actual_end
        and end > now
        and abs((panel_end - end).total_seconds()) < 1
    ):
        return None
    # GETDEL gives at most one webhook the renewal wording, even with retries.
    consumed = await cache.getdel(_key(panel_id))
    return end if consumed == context else None
