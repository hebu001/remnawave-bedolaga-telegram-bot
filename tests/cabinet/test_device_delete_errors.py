"""C14: authenticated device deletion must reflect the actual panel outcome."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices
from app.config import settings
from app.database.crud import subscription as subscription_crud
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError, RemnaWaveTransientError
from app.services.remnawave_service import RemnaWaveService


@pytest.fixture
def panel(monkeypatch):
    api = RemnaWaveAPI('https://panel.example.test', 'test-token')
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    @asynccontextmanager
    async def client(_service):
        yield api

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', client)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: True)
    subscription = SimpleNamespace(id=42, user_id=7, remnawave_id=42)
    resolver = AsyncMock(return_value=subscription)
    monkeypatch.setattr(subscription_crud, 'get_subscription_by_id_for_user', resolver)
    return SimpleNamespace(api=api, user=SimpleNamespace(id=7, remnawave_id=7), resolver=resolver)


async def delete(panel):
    return await devices.delete_device('TARGET', subscription_id=42, user=panel.user, db=AsyncMock())


async def test_delete_uses_selected_subscription_id_and_owner(panel):
    assert (await delete(panel))['success'] is True
    assert panel.resolver.await_args.args[1:] == (42, 7)
    panel.api._make_request.assert_awaited_once_with(
        'POST', '/api/hwid/devices/delete', data={'userId': 42, 'hwid': 'TARGET'}
    )


async def test_foreign_subscription_never_reaches_panel(panel):
    panel.resolver.return_value = None
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == 404
    panel.api._make_request.assert_not_awaited()


async def test_missing_multi_tariff_id_does_not_fall_back_to_another_tariff(panel):
    panel.resolver.return_value.remnawave_id = None
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == 400
    panel.api._make_request.assert_not_awaited()


async def test_repeated_delete_404_is_idempotent_success(panel):
    panel.api._make_request.side_effect = RemnaWaveAPIError('already absent', 404)
    assert (await delete(panel))['success'] is True
    assert (await delete(panel))['success'] is True


@pytest.mark.parametrize(
    'upstream_status, expected',
    [(401, 502), (403, 502), (400, 502), (500, 502), (429, 503), (502, 503), (503, 503), (504, 504)],
)
async def test_panel_errors_do_not_request_client_reauthentication(panel, upstream_status, expected):
    panel.api._make_request.side_effect = RemnaWaveAPIError('private provider payload', upstream_status)
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == expected
    assert 'private provider payload' not in error.value.detail


@pytest.mark.parametrize('timed_out', [False, True])
async def test_transient_failure_returns_service_or_gateway_timeout(panel, timed_out):
    failure = RemnaWaveTransientError('private connection details')
    if timed_out:
        failure.__cause__ = TimeoutError('timed out')
    panel.api._make_request.side_effect = failure
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == (504 if timed_out else 503)


@pytest.mark.parametrize(
    'payload',
    [
        None,
        {},
        {'response': {'total': 1, 'devices': [{'hwid': 'TARGET'}]}},
        {'response': {'total': 2, 'devices': [{'hwid': 'OTHER'}]}},
        {'response': {'total': 1, 'devices': [{}]}},
        {'response': {'total': 0, 'devices': None}},
    ],
)
async def test_noop_or_malformed_panel_response_is_not_success(panel, payload):
    panel.api._make_request.return_value = payload
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == 502


async def test_legacy_ack_requires_a_successful_read_confirmation(panel):
    panel.api._make_request.side_effect = [
        {'response': {}},
        {'response': {'total': 1, 'devices': [{'hwid': 'OTHER'}]}},
    ]
    assert (await delete(panel))['success'] is True
    assert panel.api._make_request.await_args.args == ('GET', '/api/hwid/devices/42')


async def test_legacy_ack_verification_failure_does_not_return_success(panel):
    panel.api._make_request.side_effect = [{'response': {}}, RemnaWaveAPIError('invalid upstream key', 401)]
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == 502


@pytest.mark.parametrize('target_in_list', [False, True])
async def test_complete_large_confirmation_is_not_paginated(panel, target_in_list):
    panel.api._make_request.side_effect = [
        {'response': {}},
        {
            'response': {
                'total': 1001,
                'devices': [
                    *[{'hwid': f'OTHER-{i}'} for i in range(1000)],
                    {'hwid': 'TARGET' if target_in_list else 'LAST'},
                ],
            }
        },
    ]
    if target_in_list:
        with pytest.raises(HTTPException) as error:
            await delete(panel)
        assert error.value.status_code == 502
    else:
        assert (await delete(panel))['success'] is True
    panel.api._make_request.assert_awaited_with('GET', '/api/hwid/devices/42')
    assert panel.api._make_request.await_count == 2


async def test_incomplete_per_user_list_is_not_guessed_complete(panel):
    panel.api._make_request.side_effect = [
        {'response': {}},
        {'response': {'total': 1001, 'devices': [{'hwid': f'OTHER-{i}'} for i in range(1000)]}},
    ]
    with pytest.raises(HTTPException) as error:
        await delete(panel)
    assert error.value.status_code == 502
    assert panel.api._make_request.await_count == 2


async def test_ack_followed_by_404_confirms_absence(panel):
    panel.api._make_request.side_effect = [{'response': {}}, RemnaWaveAPIError('absent', 404)]
    assert (await delete(panel))['success'] is True
