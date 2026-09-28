from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.database.models import GuestPurchaseStatus
from app.services import guest_purchase_service as service
from app.services.registration_access_service import (
    RegistrationAccessDecision,
    RegistrationAccessReason,
    RegistrationChannel,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return [] if self._value is None else [self._value]


@pytest.mark.asyncio
async def test_find_guest_purchase_user_is_non_mutating_for_existing_email_user() -> None:
    finder = getattr(service, 'find_guest_purchase_user', None)
    assert finder is not None, 'landing admission needs a non-mutating lookup helper'

    user = SimpleNamespace(
        id=7,
        email='user@example.com',
        password_hash=None,
        email_verified=False,
        promo_group_id=None,
        referral_code=None,
    )
    before = dict(vars(user))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(user))

    found = await finder(db, 'email', 'user@example.com')

    assert found is user
    assert vars(user) == before
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_landing_access_uses_existing_user_and_requested_channel() -> None:
    evaluator = getattr(service, 'evaluate_guest_purchase_registration', None)
    assert evaluator is not None, 'landing admission needs a shared domain evaluator'

    user = SimpleNamespace(id=11, status='active', email='user@example.com')
    decision = RegistrationAccessDecision(True, RegistrationAccessReason.EXISTING_ACTIVE)
    access = AsyncMock(return_value=decision)

    with (
        patch.object(service, 'find_guest_purchase_user', AsyncMock(return_value=user)),
        patch.object(service, '_guest_registration_access_service', SimpleNamespace(evaluate=access)),
    ):
        found, actual = await evaluator(
            object(),
            channel=RegistrationChannel.LANDING_PURCHASE,
            contact_type='email',
            contact_value='user@example.com',
        )

    assert found is user
    assert actual is decision
    context = access.await_args.args[1]
    assert context.channel is RegistrationChannel.LANDING_PURCHASE
    assert context.existing_user is user
    assert context.identity.email == 'user@example.com'
    assert context.identity.email_verified is False
    assert context.identity.verified_admin is False


@pytest.mark.asyncio
@pytest.mark.parametrize('claim_identifier', ['Ab3_dE9-fG7x', 'T' * 64, 'T' * 59])
async def test_web_gift_claim_denies_missing_user_before_account_mutation(claim_identifier: str) -> None:
    from app.cabinet.routes import landing

    purchase = SimpleNamespace(
        id=1,
        token='T' * 64,
        claim_code='Ab3_dE9-fG7x',
        is_gift=True,
        status=GuestPurchaseStatus.PAID.value,
        buyer_user_id=None,
        user_id=None,
        tariff_id=1,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(purchase))
    create_or_mutate = AsyncMock(side_effect=AssertionError('account mutation must not run'))
    denied = RegistrationAccessDecision(False, RegistrationAccessReason.CHANNEL_NOT_ALLOWED)

    with (
        patch.object(landing, 'get_client_ip', return_value='127.0.0.1'),
        patch.object(landing.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing, 'get_user_by_email', AsyncMock(return_value=None)),
        patch.object(landing, '_find_or_create_user', create_or_mutate),
        patch.object(landing, 'evaluate_public_registration', AsyncMock(return_value=denied), create=True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await landing.claim_gift(
                claim_identifier,
                landing.GiftClaimRequest(email='new@example.com'),
                MagicMock(),
                db,
            )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {'code': 'registration_invite_required'}
    create_or_mutate.assert_not_awaited()
    db.flush.assert_not_called()
    assert purchase.user_id is None
    assert purchase.status == GuestPurchaseStatus.PAID.value


@pytest.mark.parametrize('claim_code', ['Ab3_dE9-fG7x', None])
def test_bot_gift_claim_link_preserves_short_code_and_legacy_fallback(claim_code: str | None) -> None:
    from app.cabinet.routes.landing import _build_purchase_status_response

    purchase = SimpleNamespace(
        token='A' * 64,
        claim_code=claim_code,
        is_gift=True,
        status=GuestPurchaseStatus.PAID.value,
        tariff=None,
        delivered_at=None,
        subscription_url=None,
        subscription_crypto_link=None,
        contact_value='buyer@example.com',
        gift_recipient_value='friend@example.com',
        gift_message=None,
        gift_recipient_type='email',
        contact_type='email',
        period_days=30,
        cabinet_password=None,
        auto_login_token=None,
        paid_at=None,
        user=None,
    )

    with patch(
        'app.cabinet.routes.landing.settings',
        SimpleNamespace(CABINET_URL='https://cab.example', get_bot_username=lambda: 'ExampleBot'),
    ):
        response = _build_purchase_status_response(purchase)

    # D02: new gifts expose an independent short code; pre-migration gifts keep
    # their original 12-character alias. Never expose the full payment token.
    public_code = claim_code or purchase.token[:12]
    assert response.gift_code == public_code
    assert response.bot_claim_link == f'https://t.me/ExampleBot?start=GIFT_{public_code}'
    assert response.bot_claim_url == response.bot_claim_link
    assert response.claim_url == f'https://cab.example/gift?tab=activate&code={public_code}'
    assert response.cabinet_claim_url == response.claim_url
    assert len(public_code) == 12
    assert len(f'GIFT_{public_code}') <= 64
    assert purchase.token not in response.bot_claim_link
    assert purchase.token not in response.claim_url


@pytest.mark.asyncio
async def test_paid_fulfillment_rechecks_access_before_find_or_create() -> None:
    purchase = SimpleNamespace(
        id=9,
        token='P' * 64,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=False,
        contact_type='email',
        contact_value='new@example.com',
        gift_recipient_type=None,
        gift_recipient_value=None,
        tariff_id=1,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(purchase))
    denied = RegistrationAccessDecision(False, RegistrationAccessReason.CHANNEL_NOT_ALLOWED)
    mutate = AsyncMock(side_effect=AssertionError('User mutation must not run'))

    with (
        patch.object(service, 'evaluate_guest_purchase_registration', AsyncMock(return_value=(None, denied))),
        patch.object(service, '_find_or_create_user', mutate),
    ):
        with pytest.raises(service.GuestPurchaseError) as exc_info:
            await service.fulfill_purchase(db, purchase.token)

    assert exc_info.value.status_code == 403
    mutate.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_paid_gift_fulfillment_never_creates_recipient_user() -> None:
    purchase = SimpleNamespace(
        id=10,
        token='G' * 64,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=True,
        user_id=None,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(purchase))
    mutate = AsyncMock(side_effect=AssertionError('gift must stay unbound until claim'))

    with patch.object(service, '_find_or_create_user', mutate):
        result = await service.fulfill_purchase(db, purchase.token)

    assert result is purchase
    assert purchase.status == GuestPurchaseStatus.PAID.value
    assert purchase.user_id is None
    mutate.assert_not_awaited()
