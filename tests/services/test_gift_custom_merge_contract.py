"""Real-query contracts joining short public codes to upstream claim ownership.

SQLite checks the predicates and lifecycle guards; PostgreSQL migration tests
separately prove the alias backfill and row-lock behavior.
"""

from unittest.mock import AsyncMock

import pytest

from app.database.crud.landing import create_guest_purchase
from app.database.models import Base, GuestPurchase, User
from app.services import guest_purchase_service
from app.services.gift_claim_service import (
    GiftClaimAlreadyOwnedError,
    GiftClaimNotActivatableError,
    GiftClaimSelfActivationError,
    claim_gift_for_user,
    get_gift_by_claim_identifier,
)
from app.services.registration_access_service import RegistrationInviteKind, VerifiedRegistrationIdentity
from app.services.registration_invite_service import RegistrationInviteService
from app.utils.gift_links import build_gift_claim_artifacts, parse_gift_claim_input
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    Base.metadata.tables[name]
    for name in (
        'users',
        'subscriptions',
        'tariffs',
        'promo_groups',
        'user_promo_groups',
        'tariff_promo_groups',
        'guest_purchases',
    )
]


def gift(token: str = 'T' * 64, **changes) -> GuestPurchase:
    return GuestPurchase(
        **{
            'token': token,
            'claim_code': 'C' * 12,
            'is_gift': True,
            'status': 'paid',
            'contact_type': 'email',
            'contact_value': 'buyer@example.invalid',
            'period_days': 30,
            'amount_kopeks': 10000,
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_new_gift_resolves_only_public_code_or_long_token(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        purchase = await create_guest_purchase(
            db,
            token='N' * 64,
            is_gift=True,
            status='paid',
            contact_type='email',
            contact_value='buyer@example.invalid',
            period_days=30,
            amount_kopeks=10000,
        )
        assert len(purchase.claim_code) == 12
        assert purchase.legacy_claim_prefix is None
        assert await get_gift_by_claim_identifier(db, purchase.claim_code) is purchase
        assert await get_gift_by_claim_identifier(db, purchase.token[:12]) is None
        assert await get_gift_by_claim_identifier(db, purchase.token[:48]) is purchase
        assert await get_gift_by_claim_identifier(db, purchase.token) is purchase


@pytest.mark.asyncio
async def test_recorded_legacy_alias_survives_and_ambiguity_fails_closed(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        old = gift(legacy_claim_prefix='L' * 12)
        db.add(old)
        await db.commit()
        assert await get_gift_by_claim_identifier(db, 'L' * 12) is old
        duplicate = gift('Q' * 64, claim_code='D' * 12, legacy_claim_prefix='L' * 12)
        db.add(duplicate)
        await db.commit()
        assert await get_gift_by_claim_identifier(db, 'L' * 12) is None
        assert await get_gift_by_claim_identifier(db, 'C' * 12) is old


@pytest.mark.asyncio
async def test_exact_public_code_has_priority_over_another_gifts_alias(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        exact = gift()
        legacy = gift('L' * 64, claim_code='D' * 12, legacy_claim_prefix=exact.claim_code)
        db.add_all([exact, legacy])
        await db.commit()
        assert await get_gift_by_claim_identifier(db, exact.claim_code) is exact


@pytest.mark.asyncio
async def test_non_gift_is_never_resolved_by_code_alias_or_token(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        purchase = gift(is_gift=False, legacy_claim_prefix='L' * 12)
        db.add(purchase)
        await db.commit()
        for identifier in (purchase.claim_code, purchase.legacy_claim_prefix, purchase.token):
            assert await get_gift_by_claim_identifier(db, identifier) is None


@pytest.mark.parametrize(
    'claim_input',
    [
        'C' * 12,
        'GIFT_' + 'C' * 12,
        'giftclaim_' + 'C' * 12,
        'https://t.me/example_bot?start=GIFT_' + 'C' * 12,
        'https://cabinet.example/gift?tab=activate&code=' + 'C' * 12,
    ],
)
@pytest.mark.asyncio
async def test_short_code_uses_upstream_first_claim_and_idempotency(monkeypatch, claim_input):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=1, telegram_id=10001)
        recipient = User(id=2, telegram_id=10002)
        purchase = gift(buyer_user_id=1)
        db.add_all([buyer, recipient, purchase])
        await db.commit()

        async def activate(_db, token, *, skip_notification):
            assert token == purchase.token
            assert skip_notification is True
            assert purchase.user_id == recipient.id
            assert purchase.status == 'pending_activation'
            purchase.status = 'delivered'
            await db.flush()
            return purchase

        provision = AsyncMock(side_effect=activate)
        monkeypatch.setattr(guest_purchase_service, 'activate_purchase', provision)
        assert await claim_gift_for_user(db, recipient.id, claim_input) is purchase
        assert await claim_gift_for_user(db, recipient.id, claim_input) is purchase
        provision.assert_awaited_once()


@pytest.mark.parametrize(
    ('changes', 'claimant_id', 'expected'),
    [
        ({'buyer_user_id': 1}, 1, GiftClaimSelfActivationError),
        ({'user_id': 1}, 2, GiftClaimAlreadyOwnedError),
        ({'user_id': 1, 'status': 'delivered'}, 2, GiftClaimAlreadyOwnedError),
        ({'status': 'pending'}, 2, GiftClaimNotActivatableError),
        ({'status': 'failed'}, 2, GiftClaimNotActivatableError),
        ({'status': 'refunded'}, 2, GiftClaimNotActivatableError),
    ],
)
@pytest.mark.asyncio
async def test_short_codes_preserve_upstream_ownership_and_status_guards(monkeypatch, changes, claimant_id, expected):
    async with memory_session(monkeypatch, _TABLES) as db:
        purchase = gift(**changes)
        db.add_all([User(id=1, telegram_id=10001), User(id=2, telegram_id=10002), purchase])
        await db.commit()
        provision = AsyncMock()
        monkeypatch.setattr(guest_purchase_service, 'activate_purchase', provision)
        before = purchase.user_id, purchase.status
        with pytest.raises(expected):
            await claim_gift_for_user(db, claimant_id, purchase.claim_code)
        provision.assert_not_awaited()
        assert (purchase.user_id, purchase.status) == before


def test_all_new_share_artifacts_use_the_independent_code():
    artifacts = build_gift_claim_artifacts(
        'SECRET' + 'T' * 58,
        'example_bot',
        'https://cabinet.example',
        'A subscription for you',
        claim_code='C' * 12,
    )
    assert artifacts.public_code == 'C' * 12
    assert artifacts.bot_claim_url == 'https://t.me/example_bot?start=GIFT_' + 'C' * 12
    assert artifacts.cabinet_claim_url == 'https://cabinet.example/gift?tab=activate&code=' + 'C' * 12
    assert 'SECRET' not in repr(artifacts)
    assert parse_gift_claim_input(artifacts.cabinet_claim_url, allow_claim_code=True) == 'C' * 12


@pytest.mark.asyncio
async def test_long_legacy_prefix_treats_urlsafe_underscore_literally(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        literal = gift('a' * 47 + '_' + 'x' * 16)
        lookalike = gift('a' * 47 + 'B' + 'y' * 16, claim_code='D' * 12)
        db.add_all([literal, lookalike])
        await db.commit()
        assert await get_gift_by_claim_identifier(db, literal.token[:48]) is literal


@pytest.mark.parametrize('claim_code', ['GIFT_1234567', 'gift-1234567', 'GiFt_1234567', 'giftclaim_12', 'giftclaim-12'])
@pytest.mark.asyncio
async def test_exact_short_code_keeps_namespace_looking_characters_in_every_channel(monkeypatch, claim_code):
    async with memory_session(monkeypatch, _TABLES) as db:
        purchase = gift(claim_code=claim_code)
        db.add(purchase)
        await db.commit()
        artifacts = build_gift_claim_artifacts(
            purchase.token, 'example_bot', 'https://cabinet.example', claim_code=claim_code
        )
        for claim_input in (
            claim_code,
            artifacts.cabinet_claim_url,
            artifacts.bot_claim_url,
            'GIFT_' + claim_code,
            'giftclaim_' + claim_code,
        ):
            assert parse_gift_claim_input(claim_input, allow_claim_code=True) == claim_code
            assert await get_gift_by_claim_identifier(db, claim_input) is purchase
        # Telegram start handling already strips its wrapper before this resolver.
        assert (
            await get_gift_by_claim_identifier(
                db, parse_gift_claim_input(artifacts.bot_claim_url, allow_claim_code=True)
            )
            is purchase
        )


@pytest.mark.parametrize('wrapper', ['GIFT_', 'giftclaim_'])
@pytest.mark.asyncio
async def test_registration_gift_invite_strips_only_the_outer_namespace(monkeypatch, wrapper):
    async with memory_session(monkeypatch, _TABLES) as db:
        purchase = gift(claim_code='giftclaim_12')
        db.add(purchase)
        await db.commit()
        evidence = await RegistrationInviteService().validate(
            db,
            start_parameter=wrapper + purchase.claim_code,
            identity=VerifiedRegistrationIdentity(telegram_id=10002),
            allow_gift=True,
            lock_limited=True,
        )
        assert evidence is not None
        assert evidence.kind is RegistrationInviteKind.GIFT
        assert evidence.locked_gift is purchase
