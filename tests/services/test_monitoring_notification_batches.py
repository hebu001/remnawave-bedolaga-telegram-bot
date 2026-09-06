"""C13: batching preserves thresholds and only caches inside one monitoring cycle."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.services import monitoring_service as monitoring


async def test_expiring_queries_each_threshold_once_and_uses_batch_history(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_autopay_warning_days', lambda _: [3, 1, 2, 3])
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)
    users = [SimpleNamespace(id=i, telegram_id=1000 + i, notification_settings={}) for i in (1, 2, 3)]
    subs = [SimpleNamespace(id=i, user_id=user.id, user=user, autopay_enabled=False) for i, user in enumerate(users, 1)]
    service = monitoring.MonitoringService(bot=MagicMock())
    fetch = AsyncMock(side_effect=lambda _db, days: subs[:days])
    send = AsyncMock(return_value=True)
    service._get_expiring_paid_subscriptions = fetch
    service._send_subscription_expiring_notification = send
    service._log_monitoring_event = AsyncMock()
    history = {(2, 2, 'expiring', 2)}
    loads = []

    async def load(_db, pairs, types):
        loads.append((set(pairs), types))
        return set(history)

    async def record(_db, user, sub, kind, days):
        history.add((user, sub, kind, days))

    monkeypatch.setattr(monitoring, 'get_sent_notification_keys', load)
    monkeypatch.setattr(monitoring, 'record_notification', record)
    # Ensure the old per-user/per-history lookups are never used on this path.
    old_lookup = AsyncMock(side_effect=AssertionError('per-subscription notification lookup'))
    monkeypatch.setattr(monitoring, 'notification_sent', old_lookup)
    monkeypatch.setattr(
        monitoring, 'get_user_by_id', AsyncMock(side_effect=AssertionError('per-subscription user lookup'))
    )

    await service._check_expiring_subscriptions(AsyncMock())
    assert fetch.await_count == 3
    assert {(call.args[1].id, call.args[2]) for call in send.await_args_list} == {(1, 1), (3, 3)}
    assert loads == [({(1, 1), (2, 2), (3, 3)}, ['expiring'])]
    old_lookup.assert_not_awaited()

    # A fresh cycle must reload current history, not reuse cached eligibility.
    send.reset_mock()
    await service._check_expiring_subscriptions(AsyncMock())
    assert fetch.await_count == 6
    assert len(loads) == 2
    send.assert_not_awaited()
