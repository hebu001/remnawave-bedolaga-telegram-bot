"""Verified Telegram identities must pass blacklist checks before auth side effects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import auth
from app.cabinet.schemas.auth import (
    DeepLinkPollRequest,
    TelegramAuthRequest,
    TelegramOIDCAuthRequest,
    TelegramWidgetAuthRequest,
)
from app.services import referral_service


TELEGRAM_ID = 910000001
USERNAME = 'synthetic_auth_user'
FLOWS = ('init_data', 'widget', 'oidc')


@pytest.fixture
def auth_flow(monkeypatch):
    events = []
    user = SimpleNamespace(
        id=41,
        telegram_id=TELEGRAM_ID,
        username=USERNAME,
        first_name='Synthetic',
        last_name=None,
        status='active',
        cabinet_last_login=None,
    )
    state = SimpleNamespace(
        events=events,
        user=user,
        existing_user=user,
        valid=True,
        blocked=False,
        reason='Synthetic blacklist reason',
        response=SimpleNamespace(refresh_token='synthetic-refresh', campaign_bonus=None),
        db=AsyncMock(),
    )

    def validate_init_data(*_args, **_kwargs):
        events.append('validated')
        return {'id': TELEGRAM_ID, 'username': USERNAME, 'first_name': 'Synthetic'} if state.valid else None

    def validate_widget(*_args, **_kwargs):
        events.append('validated')
        return state.valid

    async def validate_oidc(*_args, **_kwargs):
        events.append('validated')
        return {'id': str(TELEGRAM_ID), 'preferred_username': USERNAME, 'name': 'Synthetic'} if state.valid else None

    async def check_blacklist(*_args):
        events.append('blacklist')
        return state.blocked, state.reason

    async def lookup(*_args):
        events.append('lookup')
        return state.existing_user

    async def setting(_db, key):
        return {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '42'}.get(key)

    monkeypatch.setattr(auth, 'get_client_ip', MagicMock(return_value='192.0.2.1'))
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(auth, 'validate_telegram_init_data', MagicMock(side_effect=validate_init_data))
    monkeypatch.setattr(auth, 'validate_telegram_login_widget', MagicMock(side_effect=validate_widget))
    monkeypatch.setattr(auth, 'validate_telegram_oidc_token', AsyncMock(side_effect=validate_oidc))
    monkeypatch.setattr(auth, 'get_setting_value', AsyncMock(side_effect=setting))
    state.blacklist = AsyncMock(side_effect=check_blacklist)
    monkeypatch.setattr(auth.blacklist_service, 'is_user_blacklisted', state.blacklist)
    state.lookup = AsyncMock(side_effect=lookup)
    monkeypatch.setattr(auth, 'get_user_by_telegram_id', state.lookup)
    state.lookup_by_id = AsyncMock(return_value=user)
    monkeypatch.setattr(auth, 'get_user_by_id', state.lookup_by_id)

    for name, result in (
        ('_gate_cabinet_identity', None),
        ('_create_or_claim_telegram_user', user),
        ('_require_legal_consent', []),
        ('_recover_cabinet_user_after_gate', None),
        ('_consume_widget_payload', None),
        ('_consume_oidc_token', None),
        ('_create_auth_response', state.response),
        ('_store_refresh_token', None),
        ('_process_referral_code', None),
        ('_process_campaign_bonus', None),
    ):
        mock = AsyncMock(return_value=result)
        setattr(state, name, mock)
        monkeypatch.setattr(auth, name, mock)
    state.record_consent = AsyncMock()
    monkeypatch.setattr(auth.legal_consent_service, 'record_consent', state.record_consent)
    state.pending_referral = AsyncMock(return_value=None)
    monkeypatch.setattr(referral_service, 'get_pending_referral', state.pending_referral)
    state.attach_referrer = AsyncMock()
    monkeypatch.setattr(referral_service, 'attach_referrer_if_missing', state.attach_referrer)
    state.poll = AsyncMock(return_value={'status': 'linked', 'user_id': 99})
    state.consume = AsyncMock(return_value={'user_id': user.id})
    monkeypatch.setattr(auth, 'poll_web_auth_token', state.poll)
    monkeypatch.setattr(auth, 'consume_web_auth_token', state.consume)
    return state


async def _login(flow, state):
    if flow == 'init_data':
        handler = auth.auth_telegram
        request = TelegramAuthRequest(init_data='synthetic-init-data')
    elif flow == 'widget':
        handler = auth.auth_telegram_widget
        request = TelegramWidgetAuthRequest(
            id=TELEGRAM_ID,
            username=USERNAME,
            first_name='Synthetic',
            auth_date=1700000000,
            hash='a' * 64,
        )
    else:
        handler = auth.auth_telegram_oidc
        request = TelegramOIDCAuthRequest(id_token='synthetic-id-token')
    return await handler(request=request, raw_request=MagicMock(), db=state.db)


def _assert_no_auth_side_effects(state):
    assert state.db.mock_calls == []
    for name in (
        '_gate_cabinet_identity',
        '_create_or_claim_telegram_user',
        '_require_legal_consent',
        '_recover_cabinet_user_after_gate',
        '_consume_widget_payload',
        '_consume_oidc_token',
        '_create_auth_response',
        '_store_refresh_token',
        '_process_referral_code',
        '_process_campaign_bonus',
        'record_consent',
        'pending_referral',
        'attach_referrer',
    ):
        getattr(state, name).assert_not_awaited()
    assert state.user.cabinet_last_login is None


@pytest.mark.asyncio
@pytest.mark.parametrize('flow', FLOWS)
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('reason', ['Synthetic blacklist reason', None])
async def test_blacklisted_identity_cannot_register_claim_update_or_receive_tokens(auth_flow, flow, existing, reason):
    state = auth_flow
    state.blocked = True
    state.reason = reason
    state.existing_user = state.user if existing else None
    original_profile = vars(state.user).copy()

    with pytest.raises(HTTPException) as exc:
        await _login(flow, state)

    assert exc.value.status_code == 403
    assert exc.value.detail == {'code': 'blacklisted', 'message': reason or 'Доступ запрещен'}
    assert state.events == ['validated', 'blacklist']
    state.blacklist.assert_awaited_once_with(TELEGRAM_ID, USERNAME)
    state.lookup.assert_not_awaited()
    assert vars(state.user) == original_profile
    _assert_no_auth_side_effects(state)


@pytest.mark.asyncio
@pytest.mark.parametrize('flow', FLOWS)
async def test_invalid_telegram_credentials_cannot_query_blacklist_or_mutate_auth(auth_flow, flow):
    state = auth_flow
    state.valid = False
    state.blocked = True

    with pytest.raises(HTTPException) as exc:
        await _login(flow, state)

    assert exc.value.status_code == 401
    assert state.events == ['validated']
    state.blacklist.assert_not_awaited()
    state.lookup.assert_not_awaited()
    _assert_no_auth_side_effects(state)


@pytest.mark.asyncio
@pytest.mark.parametrize('flow', FLOWS)
@pytest.mark.parametrize('existing', [False, True])
async def test_allowed_telegram_identity_keeps_existing_and_new_user_login(auth_flow, flow, existing):
    state = auth_flow
    state.existing_user = state.user if existing else None

    response = await _login(flow, state)

    assert response is state.response
    assert state.events == ['validated', 'blacklist', 'lookup']
    state.blacklist.assert_awaited_once_with(TELEGRAM_ID, USERNAME)
    state.lookup.assert_awaited_once_with(state.db, TELEGRAM_ID)
    state._gate_cabinet_identity.assert_awaited_once()
    state._recover_cabinet_user_after_gate.assert_awaited_once()
    if existing:
        state._create_or_claim_telegram_user.assert_not_awaited()
        state.record_consent.assert_not_awaited()
        state.attach_referrer.assert_awaited_once()
    else:
        state._require_legal_consent.assert_awaited_once()
        state._create_or_claim_telegram_user.assert_awaited_once()
        assert state._create_or_claim_telegram_user.call_args.kwargs['telegram_id'] == TELEGRAM_ID
        state.record_consent.assert_awaited_once()
    if flow == 'widget':
        state._consume_widget_payload.assert_awaited_once()
    elif flow == 'oidc':
        state._consume_oidc_token.assert_awaited_once()
    assert state.user.cabinet_last_login is not None
    state.db.commit.assert_awaited_once()
    state._create_auth_response.assert_awaited_once_with(state.user, state.db)
    state._store_refresh_token.assert_awaited_once_with(state.db, state.user.id, state.response.refresh_token)


async def _poll(state):
    return await auth.poll_deep_link_token(
        request=DeepLinkPollRequest(token='synthetic-deeplink-token'), raw_request=MagicMock(), db=state.db
    )


@pytest.mark.asyncio
async def test_deeplink_blacklist_uses_consumed_identity_before_login_writes_and_tokens(auth_flow):
    state = auth_flow
    state.blocked = True

    with pytest.raises(HTTPException) as exc:
        await _poll(state)

    assert exc.value.status_code == 403
    assert exc.value.detail == {'code': 'blacklisted', 'message': state.reason}
    state.consume.assert_awaited_once_with('synthetic-deeplink-token')
    state.lookup_by_id.assert_awaited_once_with(state.db, state.user.id)
    state.blacklist.assert_awaited_once_with(TELEGRAM_ID, USERNAME)
    _assert_no_auth_side_effects(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('polled', 'consumed', 'expected_status'),
    [
        (None, {'user_id': 41}, 410),
        ({'status': 'pending'}, {'user_id': 41}, 202),
        ({'status': 'invalid'}, {'user_id': 41}, 410),
        ({'status': 'linked'}, None, 410),
        ({'status': 'linked'}, {'user_id': None}, 500),
    ],
)
async def test_unverified_deeplink_cannot_query_blacklist_or_issue_tokens(auth_flow, polled, consumed, expected_status):
    state = auth_flow
    state.poll.return_value = polled
    state.consume.return_value = consumed

    with pytest.raises(HTTPException) as exc:
        await _poll(state)

    assert exc.value.status_code == expected_status
    state.lookup_by_id.assert_not_awaited()
    state.blacklist.assert_not_awaited()
    _assert_no_auth_side_effects(state)


@pytest.mark.asyncio
async def test_allowed_deeplink_keeps_existing_user_login(auth_flow):
    state = auth_flow

    response = await _poll(state)

    assert response is state.response
    state.consume.assert_awaited_once()
    state.lookup_by_id.assert_awaited_once_with(state.db, state.user.id)
    state.blacklist.assert_awaited_once_with(TELEGRAM_ID, USERNAME)
    assert state.user.cabinet_last_login is not None
    state.db.commit.assert_awaited_once()
    state._create_auth_response.assert_awaited_once_with(state.user, state.db)
    state._store_refresh_token.assert_awaited_once_with(
        state.db, state.user.id, state.response.refresh_token, device_info='deep_link'
    )
