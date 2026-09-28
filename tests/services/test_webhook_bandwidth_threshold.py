"""Panel 3.x threshold events refetch counters through the shared durable gate.

The custom 80/90/100 policy uses fresh panel state. Delayed event percentages
must not bypass the polling reservation or produce a second direct message.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.traffic_notification_service import TrafficNotificationService


@pytest.mark.parametrize(
    'data',
    [
        {'id': 42, 'lastTriggeredThreshold': 90},
        {'id': 42, 'lastTriggeredThreshold': 0},
        {'user': {'id': 42}, 'thresholdPercent': 75},
        {'id': 42, '_meta': {'thresholdPercent': 100}},
    ],
)
async def test_threshold_event_requests_authoritative_sample_without_direct_message(monkeypatch, data):
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    check = AsyncMock()
    monkeypatch.setattr(TrafficNotificationService, 'check_subscription', check)

    await svc._handle_bandwidth_threshold(None, SimpleNamespace(id=1), SimpleNamespace(id=99), data)

    check.assert_awaited_once_with(99)
    svc._notify_user.assert_not_awaited()


async def test_unresolved_event_does_not_guess_subscription(monkeypatch):
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    check = AsyncMock()
    monkeypatch.setattr(TrafficNotificationService, 'check_subscription', check)

    await svc._handle_bandwidth_threshold(None, SimpleNamespace(id=1), None, {'id': 42, 'lastTriggeredThreshold': 90})

    check.assert_not_awaited()
    svc._notify_user.assert_not_awaited()
