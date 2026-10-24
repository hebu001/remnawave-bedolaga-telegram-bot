"""Local Telegram denylist supplements upstream without stale cache or network dependency."""

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services import blacklist_service as blacklist_module


@pytest.fixture
def local_blacklist(monkeypatch):
    config = SimpleNamespace(
        BLACKLIST_CHECK_ENABLED=True,
        BLACKLIST_GITHUB_URL='https://example.invalid/blacklist.txt',
        BLACKLIST_ADDITIONAL_IDS='',
        BLACKLIST_UPDATE_INTERVAL_HOURS=24,
        BLACKLIST_IGNORE_ADMINS=True,
        is_admin=lambda telegram_id: telegram_id == 9003,
    )
    monkeypatch.setattr(blacklist_module, 'settings', config)
    service = blacklist_module.BlacklistService()
    service.update_blacklist = AsyncMock(side_effect=AssertionError('Unexpected upstream request'))
    return service, config


def test_additional_ids_default_and_standard_setting_definition():
    from app.services.system_settings_service import bot_configuration_service

    assert Settings.model_fields['BLACKLIST_ADDITIONAL_IDS'].default == ''
    definition = bot_configuration_service.get_definition('BLACKLIST_ADDITIONAL_IDS')
    assert definition.python_type is str
    assert definition.category_key == 'BLACKLIST'


@pytest.mark.parametrize(
    ('raw_ids', 'expected'),
    [
        ('', set()),
        (' , \n\t ', set()),
        ('9001,9002 9001\n9004\t9005', {9001, 9002, 9004, 9005}),
        ('broken, 9001 0 -4 12.5 9002 @someone', {9001, 9002}),
        ('9999999999', {9999999999}),
    ],
)
def test_parse_additional_ids_ignores_invalid_entries(local_blacklist, raw_ids, expected):
    service, config = local_blacklist
    config.BLACKLIST_ADDITIONAL_IDS = raw_ids

    assert service.get_additional_blacklist_ids() == expected


@pytest.mark.asyncio
async def test_local_id_is_blocked_without_upstream_data_or_request(local_blacklist):
    service, config = local_blacklist
    config.BLACKLIST_ADDITIONAL_IDS = '9001'

    assert await service.is_user_blacklisted(9001) == (True, 'Заблокирован администратором')
    service.update_blacklist.assert_not_awaited()
    assert service.blacklist_data == []
    assert service.last_update is None
    assert service._check_cache == {}


@pytest.mark.asyncio
async def test_local_change_overrides_cached_miss_and_removal_applies_immediately(local_blacklist):
    service, config = local_blacklist
    cached_miss = (False, None, time.monotonic())
    service._check_cache[9001] = cached_miss

    config.BLACKLIST_ADDITIONAL_IDS = '9001'
    assert await service.is_user_blacklisted(9001) == (True, 'Заблокирован администратором')
    assert service._check_cache[9001] == cached_miss

    config.BLACKLIST_ADDITIONAL_IDS = ''
    assert await service.is_user_blacklisted(9001) == (False, None)
    service.update_blacklist.assert_not_awaited()


@pytest.mark.asyncio
async def test_removing_local_id_keeps_existing_upstream_ban(local_blacklist):
    service, config = local_blacklist
    service.blacklist_data = [(9001, '', 'upstream reason')]
    service.last_update = datetime.now(UTC)
    assert await service.is_user_blacklisted(9001) == (True, 'upstream reason')

    config.BLACKLIST_ADDITIONAL_IDS = '9001'
    assert await service.is_user_blacklisted(9001) == (True, 'Заблокирован администратором')
    config.BLACKLIST_ADDITIONAL_IDS = ''

    assert await service.is_user_blacklisted(9001) == (True, 'upstream reason')
    service.update_blacklist.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_refresh_keeps_local_ids_and_applies_new_remote_entries(local_blacklist, monkeypatch):
    _, config = local_blacklist
    config.BLACKLIST_ADDITIONAL_IDS = '9001'
    service = blacklist_module.BlacklistService()
    service._check_cache[9002] = (False, None, time.monotonic())
    requested_urls = []

    @asynccontextmanager
    async def response(url):
        requested_urls.append(url)
        yield SimpleNamespace(status=200, text=AsyncMock(return_value='9002 # upstream reason\n'))

    @asynccontextmanager
    async def client_session():
        yield SimpleNamespace(get=response)

    monkeypatch.setattr(blacklist_module.aiohttp, 'ClientSession', client_session)

    assert await service.update_blacklist() is True
    assert await service.is_user_blacklisted(9001) == (True, 'Заблокирован администратором')
    assert await service.is_user_blacklisted(9002) == (True, 'upstream reason')
    assert service.blacklist_data == [(9002, '', 'upstream reason')]
    assert requested_urls == [config.BLACKLIST_GITHUB_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize('additional_ids', ['', '9001', 'broken,0,-4'])
async def test_upstream_entries_and_other_users_still_use_existing_rules(local_blacklist, additional_ids):
    service, config = local_blacklist
    config.BLACKLIST_ADDITIONAL_IDS = additional_ids
    upstream_entries = [(9002, '@upstream_user', 'upstream reason')]
    service.blacklist_data = upstream_entries.copy()
    service.last_update = datetime.now(UTC)

    assert await service.is_user_blacklisted(9002) == (True, 'upstream reason')
    assert await service.is_user_blacklisted(9004, 'UPSTREAM_USER') == (True, 'upstream reason')
    assert await service.is_user_blacklisted(9005, 'ordinary_user') == (False, None)
    assert service.blacklist_data == upstream_entries
    service.update_blacklist.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_switch_still_disables_local_and_upstream_checks(local_blacklist):
    service, config = local_blacklist
    config.BLACKLIST_CHECK_ENABLED = False
    config.BLACKLIST_ADDITIONAL_IDS = '9001'
    service.blacklist_data = [(9002, '', 'upstream reason')]

    assert await service.is_user_blacklisted(9001) == (False, None)
    assert await service.is_user_blacklisted(9002) == (False, None)
    service.update_blacklist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('ignore_admins', [True, False])
async def test_local_list_honors_existing_admin_exemption(local_blacklist, ignore_admins):
    service, config = local_blacklist
    config.BLACKLIST_ADDITIONAL_IDS = '9003'
    config.BLACKLIST_IGNORE_ADMINS = ignore_admins

    result = await service.is_user_blacklisted(9003)

    expected = (False, None) if ignore_admins else (True, 'Заблокирован администратором')
    assert result == expected
    service.update_blacklist.assert_not_awaited()
