"""Personal links follow Telegram staff's channel exemption, never their privileges."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.cabinet.auth import jwt_handler, personal_link, session_security
from app.cabinet.routes import support_ws
from app.config import settings
from app.services.channel_subscription_service import channel_subscription_service


TELEGRAM_ID = 987654321


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_SECRET', '53' * 32)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://personal.example.test')
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'personal-link-channel-policy-test-secret')
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', '')
    monkeypatch.setattr(settings, 'SUPPORT_MODERATOR_IDS', '')
    monkeypatch.setattr(personal_link.maintenance_service, 'is_maintenance_active', lambda: False)
    blacklist = AsyncMock(return_value=(False, None))
    channels = AsyncMock(
        return_value=[
            {'title': 'Required channel', 'is_subscribed': False, 'channel_id': '-100123', 'link': 'private-link'},
            {'title': 'Already subscribed', 'is_subscribed': True},
        ]
    )
    monkeypatch.setattr(personal_link.blacklist_service, 'is_user_blacklisted', blacklist)
    monkeypatch.setattr(channel_subscription_service, 'get_channels_with_status', channels)
    return SimpleNamespace(blacklist=blacklist, channels=channels)


def owner():
    return SimpleNamespace(
        id=42,
        telegram_id=TELEGRAM_ID,
        username=None,
        email='owner@example.com',
        email_verified=True,
        status='active',
        cabinet_auth_version=0,
        cabinet_personal_link_id=uuid4(),
        cabinet_personal_link_key_version=1,
    )


def configure_staff(monkeypatch, kind):
    monkeypatch.setattr(settings, 'ADMIN_IDS' if kind == 'admin' else 'SUPPORT_MODERATOR_IDS', str(TELEGRAM_ID))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['admin', 'moderator'])
async def test_staff_can_issue_exchange_and_validate_without_channel_membership(policy, monkeypatch, kind):
    configure_staff(monkeypatch, kind)
    user = owner()
    db = SimpleNamespace(commit=AsyncMock(), scalar=AsyncMock(return_value=user))
    monkeypatch.setattr(personal_link, 'lock_auth_user', AsyncMock(return_value=user))
    url = await personal_link.get_personal_link(db, user.id, verified_telegram_id=TELEGRAM_ID)
    assert await personal_link.resolve_personal_link(db, url.rsplit('/', 1)[1]) is user
    payload = {
        'sub': str(user.id),
        'auth_method': 'personal_link',
        'auth_version': 0,
        'personal_link_id': str(personal_link.personal_link_session_id(user)),
    }
    assert await session_security.session_is_current(db, payload, user)
    policy.channels.assert_not_awaited()
    assert policy.blacklist.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['ordinary', 'email_admin', 'rbac_role'])
async def test_other_identities_still_require_membership_and_receive_titles_only(policy, monkeypatch, kind):
    user = owner()
    if kind == 'email_admin':
        monkeypatch.setattr(settings, 'ADMIN_EMAILS', user.email)
    if kind == 'rbac_role':
        user.roles, user.permissions = ['superadmin'], ['*:*']
    with pytest.raises(HTTPException) as denied:
        await personal_link.require_personal_link_access(None, user)
    assert denied.value.status_code == 403
    assert denied.value.detail == {
        'code': 'required_channel_subscription_missing',
        'channel_titles': ['Required channel'],
    }
    policy.channels.assert_awaited_once_with(TELEGRAM_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['admin', 'moderator'])
@pytest.mark.parametrize(
    'gate,expected', [('blocked', 403), ('deleted', 403), ('maintenance', 503), ('blacklist', 403)]
)
async def test_staff_exemption_does_not_skip_other_access_gates(policy, monkeypatch, kind, gate, expected):
    configure_staff(monkeypatch, kind)
    user = owner()
    if gate in ('blocked', 'deleted'):
        user.status = gate
    elif gate == 'maintenance':
        monkeypatch.setattr(personal_link.maintenance_service, 'is_maintenance_active', lambda: True)
    else:
        policy.blacklist.return_value = (True, 'Blocked')
    with pytest.raises(HTTPException) as denied:
        await personal_link.require_personal_link_access(None, user)
    assert denied.value.status_code == expected
    policy.channels.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['admin', 'moderator'])
async def test_removing_staff_exemption_rechecks_existing_link_sessions(policy, monkeypatch, kind):
    configure_staff(monkeypatch, kind)
    user = owner()
    payload = {
        'auth_method': 'personal_link',
        'auth_version': 0,
        'personal_link_id': str(personal_link.personal_link_session_id(user)),
    }
    assert await session_security.session_is_current(None, payload, user)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    monkeypatch.setattr(settings, 'SUPPORT_MODERATOR_IDS', '')
    assert not await session_security.session_is_current(None, payload, user)
    policy.channels.assert_awaited_once_with(TELEGRAM_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['admin', 'moderator'])
async def test_staff_link_support_socket_remains_owner_only(policy, monkeypatch, kind):
    configure_staff(monkeypatch, kind)
    user = owner()
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), scalar=AsyncMock(return_value=user))
    monkeypatch.setattr(support_ws, 'get_user_by_id', AsyncMock(return_value=user))
    token = jwt_handler.create_access_token(
        user.id,
        user.telegram_id,
        auth_method='personal_link',
        personal_link_id=str(personal_link.personal_link_session_id(user)),
    )
    context, error = await support_ws._authenticate_ws(db, SimpleNamespace(headers={}), access_token=token)
    assert error is None and context is not None
    assert (
        context.role == 'owner' and context.permissions == [] and context.role_names == [] and context.role_level == 0
    )
    assert not await support_ws._has_permission(db, context, 'tickets:read')
    session = support_ws.SupportWsSession(SimpleNamespace(), context)
    assert await support_ws._refresh_session_context(db, session)
    assert session.context.role == 'owner' and not session.context.permissions
    policy.channels.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [None, 'primary', 'guest'])
async def test_other_moderator_socket_sessions_keep_existing_channel_policy(policy, monkeypatch, method):
    configure_staff(monkeypatch, 'moderator')
    error = await support_ws._apply_cabinet_account_guards(
        None, owner(), init_data_matches_user=False, auth_method=method
    )
    assert error is not None and error['code'] == 'FORBIDDEN'
    policy.channels.assert_awaited_once_with(TELEGRAM_ID)
