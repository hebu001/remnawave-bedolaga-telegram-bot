"""Resolve subscription-page payments against numeric identities without sibling guesses."""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.config import settings
from app.database.models import Subscription
from app.services.subpage_payment_service import resolve_subscription_by_short_uuid
from tests.integration.test_traffic_threshold_notifications import NOW, account, sessions


__all__ = ['account', 'sessions']
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='isolated PostgreSQL')]
SHORT = 'SignedUuid123456'


@pytest.fixture
def panel(monkeypatch):
    import app.services.remnawave_service as panel_module

    api = SimpleNamespace(get_user_by_short_uuid=AsyncMock(return_value=SimpleNamespace(id=42, short_uuid=SHORT)))
    client = MagicMock()
    client.return_value.__aenter__.return_value = api
    monkeypatch.setattr(
        panel_module, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True, get_api_client=client)
    )
    return api


async def test_panel_numeric_id_resolves_exact_tariff_before_user_pointer(sessions, account, panel):
    async with sessions() as db:
        pair = await resolve_subscription_by_short_uuid(db, SHORT)
        assert pair[1].id == account and pair[0].id == pair[1].user_id
        assert pair[1].remnawave_id == 42
    panel.get_user_by_short_uuid.assert_awaited_once_with(SHORT)


async def test_multi_tariff_never_falls_back_to_user_account(sessions, account, panel):
    panel.get_user_by_short_uuid.return_value.id = 7  # only the user's stale mono-tariff pointer
    async with sessions() as db:
        assert await resolve_subscription_by_short_uuid(db, SHORT) is None


async def test_ambiguous_local_short_uuid_does_not_pick_first(sessions, account, panel):
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        sub.remnawave_short_uuid = SHORT
        db.add(
            Subscription(
                user_id=sub.user_id,
                status='active',
                end_date=NOW + timedelta(days=30),
                remnawave_short_uuid=SHORT,
                remnawave_short_id=uuid4().hex[:12],
            )
        )
        await db.commit()
        assert await resolve_subscription_by_short_uuid(db, SHORT) is None
    panel.get_user_by_short_uuid.assert_not_awaited()


async def test_panel_returning_another_short_uuid_cannot_select_payment_owner(sessions, account, panel):
    panel.get_user_by_short_uuid.return_value.short_uuid = 'DifferentUuid123'
    async with sessions() as db:
        assert await resolve_subscription_by_short_uuid(db, SHORT) is None


async def test_single_tariff_fallback_requires_matching_numeric_user(sessions, account, panel, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: False)
    panel.get_user_by_short_uuid.return_value.id = 7
    async with sessions() as db:
        sub = await db.get(Subscription, account)
        sub.remnawave_id = None
        await db.commit()
        pair = await resolve_subscription_by_short_uuid(db, SHORT)
        assert pair[1].id == account
        assert pair[0].remnawave_id == 7
