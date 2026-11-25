"""Credential copying cannot upgrade guests; rotation requires a fresh, bound proof."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet import dependencies
from app.cabinet.auth import jwt_handler
from app.cabinet.auth.session_security import auth_proof_identity
from app.cabinet.routes import auth
from app.cabinet.schemas.auth import RefreshTokenRequest


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(jwt_handler.settings, 'CABINET_JWT_SECRET', 'proof-regression-key-with-more-than-32-characters')


def account():
    return SimpleNamespace(id=42, email='owner@example.com', email_verified=True, telegram_id=101, status='active')


def request(payload, *, verified_telegram=False):
    return SimpleNamespace(
        state=SimpleNamespace(
            cabinet_auth_method=payload.get('auth_method'),
            cabinet_auth_payload=payload,
            cabinet_verified_telegram_id=101 if verified_telegram else None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'verified_telegram', 'allowed'),
    [
        ('primary', False, True),
        ('personal_link', False, True),
        ('guest', False, False),
        ('guest', True, False),
        (None, False, False),
        (None, True, True),
    ],
)
async def test_copy_preserves_proven_owner_access_without_upgrading_guest(method, verified_telegram, allowed):
    user = account()
    req = request({'auth_method': method}, verified_telegram=verified_telegram)
    if allowed:
        assert await dependencies.get_personal_link_copy_user(req, user) is user
    else:
        with pytest.raises(HTTPException) as denied:
            await dependencies.get_personal_link_copy_user(req, user)
        assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('proof', ['email', 'telegram'])
@pytest.mark.parametrize('age', [0, 599, 601, -10])
async def test_rotation_requires_recent_original_proof(proof, age):
    user = account()
    payload = {
        'auth_method': 'primary',
        'auth_proof': proof,
        'auth_time': int(datetime.now(UTC).timestamp()) - age,
        'auth_identity': auth_proof_identity(user, proof),
    }
    if 0 <= age <= 600:
        assert await dependencies.get_personal_link_rotation_user(request(payload), user) is user
    else:
        with pytest.raises(HTTPException) as denied:
            await dependencies.get_personal_link_rotation_user(request(payload), user)
        assert denied.value.detail['code'] == 'personal_link_reauthentication_required'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['primary', 'guest', 'personal_link', None])
async def test_ordinary_signin_without_credential_proof_cannot_rotate(method):
    with pytest.raises(HTTPException):
        await dependencies.get_personal_link_rotation_user(request({'auth_method': method}), account())


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['email', 'email_verified', 'telegram_id'])
async def test_identity_changes_invalidate_rotation_proof(change):
    user = account()
    proof = 'telegram' if change == 'telegram_id' else 'email'
    payload = {
        'auth_method': 'primary',
        'auth_proof': proof,
        'auth_time': int(datetime.now(UTC).timestamp()),
        'auth_identity': auth_proof_identity(user, proof),
    }
    setattr(user, change, {'email': 'other@example.com', 'email_verified': False, 'telegram_id': 202}[change])
    with pytest.raises(HTTPException):
        await dependencies.get_personal_link_rotation_user(request(payload), user)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['guest', 'personal_link'])
async def test_nonprimary_session_cannot_borrow_even_a_current_proof(method):
    user = account()
    payload = {
        'auth_method': method,
        'auth_proof': 'email',
        'auth_time': int(datetime.now(UTC).timestamp()),
        'auth_identity': auth_proof_identity(user, 'email'),
    }
    with pytest.raises(HTTPException):
        await dependencies.get_personal_link_rotation_user(request(payload), user)


@pytest.mark.asyncio
@pytest.mark.parametrize('proof', [None, 'email', 'telegram'])
@pytest.mark.parametrize('rotation', [False, True])
async def test_refresh_never_promotes_or_renews_original_proof(monkeypatch, proof, rotation):
    user = account()
    claims = (
        {}
        if proof is None
        else {
            'auth_proof': proof,
            'auth_time': int(datetime.now(UTC).timestamp()) - 3600,
            'auth_identity': auth_proof_identity(user, proof),
        }
    )
    token = jwt_handler.create_refresh_token(user.id, auth_method='primary', **claims)
    record = SimpleNamespace(user_id=user.id, is_valid=True, token_hash='a' * 64, device_info=None, revoked_at=None)
    query = MagicMock()
    query.scalar_one_or_none.return_value = record
    db = MagicMock(execute=AsyncMock(return_value=query), commit=AsyncMock())
    monkeypatch.setattr(auth, 'lock_auth_user', AsyncMock(return_value=user))
    monkeypatch.setattr(auth.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=([], [], 0)))
    result = await auth.refresh_token(
        RefreshTokenRequest(refresh_token=token),
        SimpleNamespace(headers={'X-Refresh-Token-Rotation': '1'} if rotation else {}),
        db,
    )
    for kind in ('access', 'refresh'):
        payload = jwt_handler.get_token_payload(getattr(result, kind + '_token'), expected_type=kind)
        assert {key: payload[key] for key in claims} == claims
        if proof is None:
            assert not {'auth_proof', 'auth_time', 'auth_identity'} & payload.keys()
    with pytest.raises(HTTPException):
        await dependencies.get_personal_link_rotation_user(
            request(jwt_handler.get_token_payload(result.access_token)),
            user,
        )


@pytest.mark.parametrize('kind', ['access', 'refresh'])
def test_jwt_minting_rejects_proof_on_personal_or_guest_sessions(kind):
    user = account()
    mint = getattr(jwt_handler, 'create_' + kind + '_token')
    for method in ('guest', 'personal_link'):
        with pytest.raises(ValueError):
            mint(
                user.id,
                auth_method=method,
                auth_proof='email',
                auth_time=1,
                auth_identity=auth_proof_identity(user, 'email'),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('proof', [None, 'email', 'telegram'])
async def test_issuer_attaches_only_explicit_original_proof_to_both_tokens(monkeypatch, proof):
    user = account()
    monkeypatch.setattr(auth, 'ensure_superadmin_role_on_login', AsyncMock())
    monkeypatch.setattr(auth.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=([], [], 0)))
    monkeypatch.setattr(auth, '_user_to_response', lambda _: {'id': user.id, 'created_at': datetime.now(UTC)})
    result = await auth._create_auth_response(
        user, MagicMock(), auth_proof=proof, auth_time=1700000000 if proof else None
    )
    for kind in ('access', 'refresh'):
        payload = jwt_handler.get_token_payload(getattr(result, kind + '_token'), expected_type=kind)
        assert payload['auth_method'] == 'primary'
        if proof:
            assert payload['auth_proof'] == proof and payload['auth_time'] == 1700000000
            assert payload['auth_identity'] == auth_proof_identity(user, proof)
        else:
            assert 'auth_proof' not in payload and 'auth_time' not in payload


@pytest.mark.asyncio
async def test_legacy_telegram_copy_cannot_survive_telegram_reassignment():
    user = account()
    req = request({}, verified_telegram=True)
    user.telegram_id = 202
    with pytest.raises(HTTPException):
        await dependencies.get_personal_link_copy_user(req, user)
