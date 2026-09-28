import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.remnawave_service import RemnaWaveService


async def test_sync_existing_user_refreshes_subscription_links(monkeypatch):
    import app.services.remnawave_service as module

    user = SimpleNamespace(id=1, telegram_id=None, email=None, username='test', full_name='Test')
    sub = SimpleNamespace(
        id=1, user=user, status='active', end_date=datetime(2030, 1, 1, tzinfo=UTC),
        tariff=None, traffic_limit_gb=300, connected_squads=[], remnawave_short_id='abc',
        remnawave_uuid='panel-user', remnawave_short_uuid='old-short',
        subscription_url='https://old.example/old-short', subscription_crypto_link='old-crypto',
    )
    panel_user = SimpleNamespace(short_uuid='new-short', subscription_url='https://sub2.example/new-short',
                                 happ_crypto_link='new-crypto')
    api = MagicMock()
    api.update_user = AsyncMock(return_value=panel_user)

    @asynccontextmanager
    async def client():
        yield api

    @asynccontextmanager
    async def lease(subscription_id):
        assert subscription_id == sub.id
        yield SimpleNamespace(allowed=True, subscription=sub)

    monkeypatch.setattr('app.database.crud.subscription.get_subscriptions_batch', AsyncMock(return_value=[sub]))
    monkeypatch.setattr('app.services.grace_access_runtime.grace_sensitive_panel_update', lease)
    monkeypatch.setattr(module, 'resolve_hwid_device_limit_for_payload', lambda _: 2)
    monkeypatch.setattr(module, 'get_traffic_reset_strategy', lambda _: 'MONTH')
    monkeypatch.setattr(module, 'settings', SimpleNamespace(
        is_multi_tariff_enabled=lambda: True,
        build_remnawave_subscription_username=lambda **_: 'test_abc',
        format_remnawave_user_description=lambda **_: 'Test',
    ))
    service = _create_service()
    service.get_api_client = client
    db = AsyncMock()

    stats = await service.sync_users_to_panel(db)

    assert stats == {'created': 0, 'updated': 1, 'errors': 0}
    assert sub.remnawave_short_uuid == panel_user.short_uuid
    assert sub.subscription_url == panel_user.subscription_url
    assert sub.subscription_crypto_link == panel_user.happ_crypto_link
    api.create_user.assert_not_called()


def _create_service() -> RemnaWaveService:
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._panel_timezone = ZoneInfo('UTC')
    service._utc_timezone = ZoneInfo('UTC')
    return service


def _make_panel_user(telegram_id: int, expire_at: str, status: str = 'ACTIVE') -> dict:
    return {
        'telegramId': telegram_id,
        'expireAt': expire_at,
        'status': status,
    }


def _async_nested_ctx() -> MagicMock:
    """Async context manager for ``db.begin_nested()`` (SAVEPOINT)."""
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    return nested


def test_deduplicate_prefers_latest_expire_date():
    service = _create_service()

    telegram_id = 100
    older = _make_panel_user(telegram_id, datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat())
    newer = _make_panel_user(telegram_id, datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([older, newer])

    assert deduplicated[telegram_id] is newer


def test_deduplicate_prefers_active_status_on_same_expire():
    service = _create_service()

    telegram_id = 200
    expire = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat()
    disabled = _make_panel_user(telegram_id, expire, status='DISABLED')
    active = _make_panel_user(telegram_id, expire, status='ACTIVE')

    deduplicated = service._deduplicate_panel_users_by_telegram_id([disabled, active])

    assert deduplicated[telegram_id] is active


def test_deduplicate_ignores_records_without_expire_date():
    service = _create_service()

    telegram_id = 300
    missing_expire = _make_panel_user(telegram_id, '')
    valid = _make_panel_user(telegram_id, datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([missing_expire, valid])

    assert deduplicated[telegram_id] is valid


async def test_get_or_create_user_handles_unique_violation(monkeypatch):
    service = _create_service()
    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=_async_nested_ctx())

    panel_user = {'telegramId': 555, 'username': 'existing'}
    existing_user = object()

    create_user_mock = AsyncMock(side_effect=IntegrityError('stmt', 'params', Exception('unique')))
    get_user_mock = AsyncMock(return_value=existing_user)

    monkeypatch.setattr('app.services.remnawave_service.create_user_no_commit', create_user_mock)
    monkeypatch.setattr(
        'app.services.remnawave_service.get_user_by_telegram_id',
        get_user_mock,
    )

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is existing_user
    assert created is False
    create_user_mock.assert_awaited_once()
    get_user_mock.assert_awaited_once_with(db, 555)


async def test_get_or_create_user_creates_new(monkeypatch):
    service = _create_service()
    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=_async_nested_ctx())

    panel_user = {'telegramId': 777, 'username': 'new_user'}
    new_user = object()

    create_user_mock = AsyncMock(return_value=new_user)

    monkeypatch.setattr('app.services.remnawave_service.create_user_no_commit', create_user_mock)

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is new_user
    assert created is True
    create_user_mock.assert_awaited_once_with(
        db=db,
        telegram_id=777,
        username='new_user',
        first_name='User 777',
        last_name=None,
        language='ru',
    )
