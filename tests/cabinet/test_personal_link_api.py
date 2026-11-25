"""API and database regressions for permanent cabinet credentials."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.cabinet import dependencies
from app.cabinet.auth import jwt_handler, personal_link
from app.cabinet.routes import auth, personal_link as routes
from app.config import settings
from app.database.models import User
from app.services.channel_subscription_service import channel_subscription_service
from tests.integration.test_purchase_atomicity import sessions


__all__ = ['sessions']
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs isolated PostgreSQL'),
]


def bearer(token):
    return {'Authorization': f'Bearer {token}'}


@pytest_asyncio.fixture
async def client(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_SECRET', 'a9' * 32)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://personal.example.test')
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'personal-link-tests-distinct-long-jwt-key')
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', '')
    monkeypatch.setattr(settings, 'SUPPORT_MODERATOR_IDS', '')
    monkeypatch.setattr(personal_link.maintenance_service, 'is_maintenance_active', lambda: False)
    monkeypatch.setattr(personal_link.blacklist_service, 'is_user_blacklisted', AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))

    async def db_override():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(routes.router, prefix='/cabinet')
    app.include_router(auth.router, prefix='/cabinet')
    app.dependency_overrides[dependencies.get_cabinet_db] = db_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://personal.example.test') as client:
        yield client


@pytest_asyncio.fixture
async def owner(sessions, client):
    async with sessions() as db:
        user = User(
            email=f'{uuid4().hex}@example.test',
            auth_type='email',
            status='active',
            email_verified=True,
            first_name='Link owner',
            balance_kopeks=12345,
        )
        db.add(user)
        await db.commit()
        pair = await auth._create_auth_response(user, db, auth_proof='email')
        await auth._store_refresh_token(db, user.id, pair.refresh_token)
        return SimpleNamespace(id=user.id, pair=pair)


async def link(client, owner):
    result = await client.get('/cabinet/auth/personal-link', headers=bearer(owner.pair.access_token))
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'no-store'
    return result.json()['url']


async def exchange(client, url):
    token = url.split('#', 1)[1] if '#' in url else url.rsplit('/', 1)[1]
    return await client.post('/cabinet/auth/personal-link/exchange', json={'token': token})


async def test_repeatable_link_full_account_and_rotating_session(client, owner):
    url = await link(client, owner)
    assert url == await link(client, owner)
    assert url.startswith('https://personal.example.test/s/')
    assert len(url.rsplit('/', 1)[1]) == 22
    first, second = await exchange(client, url), await exchange(client, url)
    assert first.status_code == second.status_code == 200
    assert first.headers['cache-control'] == 'no-store'
    pair = first.json()
    assert pair['user']['id'] == owner.id and pair['user']['balance_kopeks'] == 12345
    assert pair['refresh_token'] != second.json()['refresh_token']
    payload = jwt_handler.get_token_payload(pair['access_token'])
    assert payload['auth_method'] == 'personal_link'
    assert payload['permissions'] == [] and payload.get('role_level', 0) == 0
    me = await client.get('/cabinet/auth/me', headers=bearer(pair['access_token']))
    assert me.status_code == 200 and me.json()['id'] == owner.id
    refreshed = await client.post(
        '/cabinet/auth/refresh',
        json={'refresh_token': pair['refresh_token']},
        headers={'X-Refresh-Token-Rotation': '1'},
    )
    assert refreshed.status_code == 200
    new_payload = jwt_handler.get_token_payload(refreshed.json()['refresh_token'], expected_type='refresh')
    assert new_payload['auth_method'] == 'personal_link'
    assert new_payload['personal_link_id'] == payload['personal_link_id']


async def test_rotation_revokes_link_sessions_preserves_primary(client, owner):
    old_url = await link(client, owner)
    pair = (await exchange(client, old_url)).json()
    rotated = await client.post('/cabinet/auth/personal-link/rotate', headers=bearer(owner.pair.access_token))
    assert rotated.status_code == 200
    assert rotated.json()['url'] != old_url
    assert (await exchange(client, old_url)).status_code == 401
    assert (await exchange(client, rotated.json()['url'])).status_code == 200
    assert (await client.get('/cabinet/auth/me', headers=bearer(pair['access_token']))).status_code == 401
    assert (
        await client.post('/cabinet/auth/refresh', json={'refresh_token': pair['refresh_token']})
    ).status_code == 401
    assert (await client.get('/cabinet/auth/me', headers=bearer(owner.pair.access_token))).status_code == 200


async def test_both_formats_keep_same_session_generation_and_rotate_together(client, owner, sessions):
    short_url = await link(client, owner)
    async with sessions() as db:
        user = await db.get(User, owner.id)
        origin, key = personal_link._configuration()
        old_v1 = personal_link._sign(user, origin, key)
    legacy_pair = (await exchange(client, origin + '/enter#' + old_v1)).json()
    short_pair = (await exchange(client, short_url)).json()
    legacy_claims = jwt_handler.get_token_payload(legacy_pair['access_token'])
    short_claims = jwt_handler.get_token_payload(short_pair['access_token'])
    assert legacy_claims['personal_link_id'] == short_claims['personal_link_id']
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    token = short_url.rsplit('/', 1)[1]
    alias = token[:-1] + alphabet[alphabet.index(token[-1]) + 1]
    assert (await client.post('/cabinet/auth/personal-link/exchange', json={'token': alias})).status_code == 401
    rotated = await client.post('/cabinet/auth/personal-link/rotate', headers=bearer(owner.pair.access_token))
    assert rotated.status_code == 200
    for url in (short_url, origin + '/enter#' + old_v1):
        assert (await exchange(client, url)).status_code == 401
    for pair in (legacy_pair, short_pair):
        assert (await client.get('/cabinet/auth/me', headers=bearer(pair['access_token']))).status_code == 401
        assert (
            await client.post('/cabinet/auth/refresh', json={'refresh_token': pair['refresh_token']})
        ).status_code == 401
    assert (await exchange(client, rotated.json()['url'])).status_code == 200


async def test_copy_allowed_for_link_sessions_replacement_requires_primary_proof(client, owner):
    url = await link(client, owner)
    access = (await exchange(client, url)).json()['access_token']
    response = await client.get('/cabinet/auth/personal-link', headers=bearer(access))
    assert response.status_code == 200 and response.json()['url'] == url
    assert (await client.post('/cabinet/auth/personal-link/rotate', headers=bearer(access))).status_code == 403
    guest = jwt_handler.create_access_token(owner.id, None, auth_method='guest')
    legacy = jwt_handler.create_access_token(owner.id, None)
    for token in (guest, legacy):
        for method, suffix in (('GET', ''), ('POST', '/rotate')):
            response = await client.request(method, '/cabinet/auth/personal-link' + suffix, headers=bearer(token))
            assert response.status_code == 403
            assert response.headers['cache-control'] == 'no-store'
    assert await link(client, owner) == url
    anonymous = await client.get('/cabinet/auth/personal-link')
    assert anonymous.status_code == 401 and anonymous.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize(
    'body',
    [
        {'token': 'private-input-too-short'},
        {'token': 'private-value' * 500},
        {'token': ['secret-input']},
        {'secret': 'unknown-input'},
        None,
    ],
)
async def test_invalid_input_is_not_reflected_or_cached(client, body):
    response = await client.post('/cabinet/auth/personal-link/exchange', json=body)
    assert response.status_code == 400
    assert response.json() == {'detail': 'Invalid request'}
    assert response.headers['cache-control'] == 'no-store'


async def test_bad_signature_unknown_selector_rate_limit_fail_closed(client, owner, monkeypatch):
    url = await link(client, owner)
    token = url.rsplit('/', 1)[1]
    invalid = token[:-1] + ('A' if token[-1] != 'A' else 'B')
    for value in (invalid, 'v1.' + uuid4().hex + '.' + 'a' * 43):
        response = await client.post('/cabinet/auth/personal-link/exchange', json={'token': value})
        assert response.status_code == 401 and value not in response.text
    limiter = AsyncMock(return_value=True)
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', limiter)
    response = await exchange(client, url)
    assert response.status_code == 429 and response.headers['cache-control'] == 'no-store'
    assert response.headers['retry-after'] == '60'
    assert limiter.call_args.kwargs['fail_closed'] is True


@pytest.mark.parametrize(
    'change, value, expected',
    [
        ('CABINET_PERSONAL_LINK_ENABLED', False, 404),
        ('CABINET_PERSONAL_LINK_SECRET', 'bad-key', 503),
        ('CABINET_PERSONAL_LINK_SECRET', 'f5' * 32, 401),
        ('CABINET_URL', 'https://other.example.test', 401),
    ],
)
async def test_configuration_change_disables_old_credentials(client, owner, monkeypatch, change, value, expected):
    url = await link(client, owner)
    access = (await exchange(client, url)).json()['access_token']
    monkeypatch.setattr(settings, change, value)
    response = await exchange(client, url)
    assert response.status_code == expected
    assert response.headers['cache-control'] == 'no-store'
    assert (await client.get('/cabinet/auth/me', headers=bearer(access))).status_code == 401
    assert (await client.get('/cabinet/auth/me', headers=bearer(owner.pair.access_token))).status_code == 200


@pytest.mark.parametrize('kind', ['blocked', 'deleted'])
async def test_inactive_accounts_rejected(client, owner, sessions, kind):
    url = await link(client, owner)
    async with sessions() as db:
        user = await db.get(User, owner.id)
        user.status = kind
        await db.commit()
    assert (await exchange(client, url)).status_code == 403


async def test_database_default_covers_orm_and_raw_inserts(client, sessions):
    async with sessions() as db:
        users = [
            User(auth_type=kind, status='active', email=f'{uuid4().hex}@example.test')
            for kind in ('telegram', 'email', 'google', 'yandex', 'discord', 'vk')
        ]
        db.add_all(users)
        await db.commit()
        # Literal SQL supplies existing mandatory columns and leaves both link
        # fields to PostgreSQL. SQLAlchemy's Python defaults cannot fill them.
        raw_id, raw_selector = (
            await db.execute(
                text(
                    'INSERT INTO users(auth_type, status, has_had_paid_subscription, email_verified, '
                    'auto_promo_group_assigned, auto_promo_group_threshold_kopeks, promo_offer_discount_percent, '
                    'has_made_first_topup, restriction_topup, restriction_subscription, partner_status) '
                    "VALUES ('email', 'active', false, false, false, 0, 0, false, false, false, 'none') "
                    'RETURNING id, cabinet_personal_link_id'
                )
            )
        ).one()
        await db.commit()
        assert raw_id and raw_selector
        selectors = {user.cabinet_personal_link_id for user in users}
        assert len(selectors) == len(users) and None not in selectors
        assert raw_selector not in selectors
        for user in users:
            payload = {'sub': str(user.id), 'auth_method': 'primary', 'auth_version': user.cabinet_auth_version}
            assert (await personal_link.get_personal_link(db, user.id, auth_payload=payload)).startswith(
                'https://personal.example.test/s/'
            )


async def test_concurrent_exchange_and_rotation_remain_consistent(client, owner, sessions):
    url = await link(client, owner)
    async with sessions() as db:
        user = await personal_link.resolve_personal_link(db, url.rsplit('/', 1)[1])
        rotation = asyncio.create_task(
            client.post('/cabinet/auth/personal-link/rotate', headers=bearer(owner.pair.access_token))
        )
        pair = await auth._create_auth_response(
            user, db, auth_method='personal_link', personal_link_id=str(personal_link.personal_link_session_id(user))
        )
        await auth._store_refresh_token(db, user.id, pair.refresh_token, device_info='personal_link')
    result = await rotation
    assert result.status_code == 200
    assert (await exchange(client, url)).status_code == 401
    assert (await exchange(client, result.json()['url'])).status_code == 200
    assert (await client.get('/cabinet/auth/me', headers=bearer(pair.access_token))).status_code == 401


async def test_copy_rechecks_link_generation_after_dependency_before_revealing(client, owner, sessions, monkeypatch):
    url = await link(client, owner)
    access = (await exchange(client, url)).json()['access_token']
    original = personal_link.get_personal_link
    replacement_selector = uuid4()

    async def rotate_between_dependency_and_lock(db, user_id, **kwargs):
        async with sessions() as competing:
            user = await competing.get(User, user_id)
            user.cabinet_personal_link_id = replacement_selector
            await competing.commit()
        return await original(db, user_id, **kwargs)

    monkeypatch.setattr(routes, 'get_personal_link', rotate_between_dependency_and_lock)
    response = await client.get('/cabinet/auth/personal-link', headers=bearer(access))
    assert response.status_code == 401 and 'url' not in response.json()
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('change,expected', [('credential_reset', 401), ('email_identity', 403)])
async def test_rotation_rechecks_proof_after_dependency_under_lock(
    client, owner, sessions, monkeypatch, change, expected
):
    original = personal_link.get_personal_link
    async with sessions() as db:
        original_selector = (await db.get(User, owner.id)).cabinet_personal_link_id

    async def change_between_dependency_and_lock(db, user_id, **kwargs):
        async with sessions() as competing:
            user = await competing.get(User, user_id)
            if change == 'credential_reset':
                user.cabinet_auth_version += 1
            else:
                user.email = f'{uuid4().hex}@example.test'
            await competing.commit()
        return await original(db, user_id, **kwargs)

    monkeypatch.setattr(routes, 'get_personal_link', change_between_dependency_and_lock)
    response = await client.post('/cabinet/auth/personal-link/rotate', headers=bearer(owner.pair.access_token))
    assert response.status_code == expected and 'url' not in response.json()
    async with sessions() as db:
        assert (await db.get(User, owner.id)).cabinet_personal_link_id == original_selector


async def test_telegram_copy_checks_current_owner_under_lock(client, owner, sessions):
    async with sessions() as db:
        user = await db.get(User, owner.id)
        user.telegram_id = 900000001
        user.cabinet_auth_version = 5
        await db.commit()
        assert await personal_link.get_personal_link(db, owner.id, verified_telegram_id=900000001)
        user.telegram_id = 900000002
        await db.commit()
        with pytest.raises(HTTPException) as denied:
            await personal_link.get_personal_link(db, owner.id, verified_telegram_id=900000001)
        assert denied.value.status_code == 403
        await db.rollback()
        with pytest.raises(HTTPException) as unproven:
            await personal_link.get_personal_link(db, owner.id)
        assert unproven.value.status_code == 403


@pytest.mark.parametrize('staff', ['admin', 'moderator'])
async def test_channel_exempt_staff_link_sessions_stay_customer_only(client, owner, sessions, monkeypatch, staff):
    telegram_id = 9_200_000_000 + owner.id
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(settings, 'ADMIN_IDS' if staff == 'admin' else 'SUPPORT_MODERATOR_IDS', str(telegram_id))
    channels = AsyncMock(return_value=[{'title': 'Required channel', 'is_subscribed': False}])
    monkeypatch.setattr(channel_subscription_service, 'get_channels_with_status', channels)
    async with sessions() as db:
        user = await db.get(User, owner.id)
        user.telegram_id = telegram_id
        await db.commit()
        bot_url = await personal_link.get_personal_link(db, user.id, verified_telegram_id=telegram_id)
    url = await link(client, owner)
    assert url == bot_url
    exchanged = await exchange(client, url)
    assert exchanged.status_code == 200
    pair = exchanged.json()
    headers = bearer(pair['access_token'])
    assert (await client.get('/cabinet/auth/me', headers=headers)).status_code == 200
    copied = await client.get('/cabinet/auth/personal-link', headers=headers)
    assert copied.status_code == 200 and copied.json()['url'] == url
    assert (await client.get('/cabinet/auth/me/is-admin', headers=headers)).json() == {'is_admin': False}
    assert (await client.get('/cabinet/auth/me/permissions', headers=headers)).json() == {
        'permissions': [],
        'roles': [],
        'role_level': 0,
    }
    assert (await client.post('/cabinet/auth/personal-link/rotate', headers=headers)).status_code == 403
    refreshed = await client.post(
        '/cabinet/auth/refresh',
        json={'refresh_token': pair['refresh_token']},
        headers={'X-Refresh-Token-Rotation': '1'},
    )
    assert refreshed.status_code == 200
    payload = jwt_handler.get_token_payload(refreshed.json()['access_token'])
    assert payload['auth_method'] == 'personal_link'
    assert payload['permissions'] == [] and payload['roles'] == [] and not payload.get('role_level')
    assert (await client.get('/cabinet/auth/me', headers=bearer(refreshed.json()['access_token']))).status_code == 200
    channels.assert_not_awaited()
    if staff == 'moderator':
        # The existing ordinary-login policy outside link management is unchanged.
        assert (await client.get('/cabinet/auth/me', headers=bearer(owner.pair.access_token))).status_code == 403


async def test_unsubscribed_ordinary_user_cannot_issue_exchange_or_refresh_link(client, owner, sessions, monkeypatch):
    telegram_id = 9_300_000_000 + owner.id
    url = await link(client, owner)
    pair = (await exchange(client, url)).json()
    async with sessions() as db:
        user = await db.get(User, owner.id)
        user.telegram_id = telegram_id
        await db.commit()
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    channels = AsyncMock(return_value=[{'title': 'Required channel', 'is_subscribed': False}])
    monkeypatch.setattr(channel_subscription_service, 'get_channels_with_status', channels)
    assert (await client.get('/cabinet/auth/personal-link', headers=bearer(owner.pair.access_token))).status_code == 403
    rejected = await exchange(client, url)
    assert rejected.status_code == 403
    assert rejected.json()['detail'] == {
        'code': 'required_channel_subscription_missing',
        'channel_titles': ['Required channel'],
    }
    assert (await client.get('/cabinet/auth/me', headers=bearer(pair['access_token']))).status_code == 401
    assert (
        await client.post('/cabinet/auth/refresh', json={'refresh_token': pair['refresh_token']})
    ).status_code == 401
    async with sessions() as db:
        with pytest.raises(HTTPException) as denied:
            await personal_link.get_personal_link(db, owner.id, verified_telegram_id=telegram_id)
        assert denied.value.status_code == 403
    assert channels.await_count >= 4
