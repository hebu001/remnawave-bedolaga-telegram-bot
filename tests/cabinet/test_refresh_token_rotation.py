"""Regression tests for sliding cabinet refresh-token rotation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from app.cabinet.auth import jwt_handler
from app.cabinet.routes import auth
from app.cabinet.schemas.auth import RefreshTokenRequest


def test_refresh_tokens_are_unique_when_issued_in_the_same_second() -> None:
    secret = 'test-refresh-secret-that-is-long-enough'
    fake_settings = SimpleNamespace(
        get_cabinet_refresh_token_expire_days=lambda: 7,
        get_cabinet_jwt_secret=lambda: secret,
    )
    with patch.object(jwt_handler, 'settings', fake_settings):
        first = jwt_handler.create_refresh_token(42)
        second = jwt_handler.create_refresh_token(42)

    assert first != second
    first_payload = jwt.decode(first, secret, algorithms=[jwt_handler.JWT_ALGORITHM])
    second_payload = jwt.decode(second, secret, algorithms=[jwt_handler.JWT_ALGORITHM])
    assert first_payload['jti'] != second_payload['jti']


@pytest.mark.asyncio
async def test_rotate_refresh_token_revokes_old_and_stores_replacement() -> None:
    old_record = SimpleNamespace(
        user_id=42,
        token_hash='a' * 64,
        device_info='telegram-ios',
        revoked_at=None,
    )
    db = MagicMock()
    db.commit = AsyncMock()
    expires_at = datetime.now(UTC) + timedelta(days=7)

    with (
        patch.object(auth, 'create_refresh_token', return_value='new-refresh-token'),
        patch.object(auth, 'get_refresh_token_expires_at', return_value=expires_at),
    ):
        result = await auth._rotate_refresh_token(db, old_record, 42)

    assert result == 'new-refresh-token'
    assert old_record.revoked_at is not None
    replacement = db.add.call_args.args[0]
    assert replacement.user_id == 42
    assert replacement.device_info == 'telegram-ios'
    assert replacement.expires_at == expires_at
    assert replacement.token_hash != old_record.token_hash
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_endpoint_returns_rotated_refresh_token() -> None:
    old_record = SimpleNamespace(
        user_id=42,
        token_hash='b' * 64,
        device_info='telegram-android',
        revoked_at=None,
        is_valid=True,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = old_record
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)
    user = SimpleNamespace(id=42, telegram_id=6925404065, status='active')

    rotate = AsyncMock(return_value='rotated-refresh-token')
    with (
        patch.object(auth, 'get_token_payload', return_value={'sub': '42', 'type': 'refresh'}),
        patch.object(auth, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.object(auth.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=([], [], 0))),
        patch.object(auth, 'create_access_token', return_value='new-access-token'),
        patch.object(auth, '_rotate_refresh_token', rotate),
        patch.object(
            auth,
            'settings',
            SimpleNamespace(get_cabinet_access_token_expire_minutes=lambda: 15),
        ),
    ):
        response = await auth.refresh_token(
            request=RefreshTokenRequest(refresh_token='old-refresh-token'),
            raw_request=SimpleNamespace(headers={'X-Refresh-Token-Rotation': '1'}),
            db=db,
        )

    assert response.access_token == 'new-access-token'
    assert response.refresh_token == 'rotated-refresh-token'
    assert response.expires_in == 900
    rotate.assert_awaited_once_with(db, old_record, 42)


@pytest.mark.asyncio
async def test_legacy_refresh_client_keeps_old_token_during_rollout() -> None:
    old_record = SimpleNamespace(
        user_id=42,
        token_hash='c' * 64,
        device_info='legacy-webview',
        revoked_at=None,
        is_valid=True,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = old_record
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)
    user = SimpleNamespace(id=42, telegram_id=6925404065, status='active')

    rotate = AsyncMock(return_value='must-not-be-used')
    with (
        patch.object(auth, 'get_token_payload', return_value={'sub': '42', 'type': 'refresh'}),
        patch.object(auth, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.object(auth.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=([], [], 0))),
        patch.object(auth, 'create_access_token', return_value='new-access-token'),
        patch.object(auth, '_rotate_refresh_token', rotate),
        patch.object(
            auth,
            'settings',
            SimpleNamespace(get_cabinet_access_token_expire_minutes=lambda: 15),
        ),
    ):
        response = await auth.refresh_token(
            request=RefreshTokenRequest(refresh_token='old-refresh-token'),
            raw_request=SimpleNamespace(headers={}),
            db=db,
        )

    assert response.refresh_token == 'old-refresh-token'
    rotate.assert_not_awaited()
