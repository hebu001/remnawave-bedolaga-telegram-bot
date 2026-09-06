"""Guest account credentials use the bounded async password service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.auth import password_utils
from app.services import guest_purchase_service as guest
from app.utils.password_executor import PasswordWorkloadBusy


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True])
async def test_guest_password_is_awaited_and_matches_delivered_credentials(monkeypatch, existing):
    monkeypatch.setattr(password_utils, 'BCRYPT_ROUNDS', 4)
    hashing = AsyncMock(wraps=password_utils.hash_password_async)
    monkeypatch.setattr(guest, 'hash_password_async', hashing)
    user = SimpleNamespace(password_hash=None, email_verified=True, promo_group_id=1, referral_code='existing')
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: user if existing else None))
    db = AsyncMock()
    db.execute.return_value = result
    db.add = MagicMock()
    db.begin_nested = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    monkeypatch.setattr(guest, '_get_or_create_default_promo_group', AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(guest, 'create_unique_referral_code', AsyncMock(return_value='new'))
    purchase = SimpleNamespace(cabinet_password=None)
    returned, is_new = await guest._find_or_create_user(db, 'email', 'guest@example.com', purchase=purchase)
    assert is_new
    hashing.assert_awaited_once_with(purchase.cabinet_password)
    assert password_utils.verify_password(purchase.cabinet_password, returned.password_hash)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_password_overload_does_not_assign_or_consume_credentials(monkeypatch):
    monkeypatch.setattr(guest, 'hash_password_async', AsyncMock(side_effect=PasswordWorkloadBusy))
    user = SimpleNamespace(password_hash=None)
    purchase = SimpleNamespace(cabinet_password=None, status='paid')
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: user))
    db = AsyncMock()
    db.execute.return_value = result
    with pytest.raises(guest.GuestPurchaseError) as error:
        await guest._find_or_create_user(db, 'email', 'guest@example.com', purchase=purchase)
    assert error.value.status_code == 503
    assert user.password_hash is None
    assert purchase.cabinet_password is None
    assert purchase.status == 'paid'
    db.commit.assert_not_awaited()
