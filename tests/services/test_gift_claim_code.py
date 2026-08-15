"""Contract tests for compact gift claim codes and links."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.gift_claim_service import (
    GIFT_CLAIM_CODE_LENGTH,
    build_gift_bot_link,
    build_gift_web_link,
    generate_gift_claim_code,
    get_gift_by_claim_identifier,
    gift_public_code,
    is_supported_gift_claim_identifier,
)


def _gift(*, claim_code: str | None = 'C' * 12, token: str = 'T' * 64) -> SimpleNamespace:
    return SimpleNamespace(is_gift=True, claim_code=claim_code, token=token)


def test_claim_code_is_compact_urlsafe_and_high_entropy() -> None:
    codes = {generate_gift_claim_code() for _ in range(100)}

    assert len(codes) == 100
    assert all(len(code) == GIFT_CLAIM_CODE_LENGTH for code in codes)
    assert all(re.fullmatch(r'[A-Za-z0-9_-]{12}', code) for code in codes)


def test_public_code_prefers_claim_code_and_keeps_legacy_fallback() -> None:
    assert gift_public_code(_gift()) == 'C' * 12
    assert gift_public_code(_gift(claim_code=None, token='L' * 64)) == 'L' * 12
    assert gift_public_code(_gift(claim_code='O' * 22, token='L' * 64)) == 'L' * 12


def test_compact_links_use_claim_code_without_exposing_internal_token() -> None:
    purchase = _gift(claim_code='aB_9-' + 'x' * 7, token='SECRET' + 'T' * 58)

    with (
        patch('app.services.gift_claim_service.settings.BOT_USERNAME', 'test3evo_bot'),
        patch('app.services.gift_claim_service.settings.CABINET_URL', 'https://miniapp.evoevoevo.com'),
    ):
        bot_link = build_gift_bot_link(purchase)
        web_link = build_gift_web_link(purchase)

    assert bot_link == f'https://t.me/test3evo_bot?start=GIFT_{purchase.claim_code}'
    assert web_link == f'https://miniapp.evoevoevo.com/gift?tab=activate&code={purchase.claim_code}'
    assert purchase.token not in bot_link
    assert purchase.token not in web_link


def test_supported_identifier_formats_are_explicit() -> None:
    assert is_supported_gift_claim_identifier('C' * 12)
    assert is_supported_gift_claim_identifier('T' * 48)  # older safe long prefix
    assert is_supported_gift_claim_identifier('T' * 64)  # full internal token
    assert not is_supported_gift_claim_identifier('X' * 8)
    assert not is_supported_gift_claim_identifier('X' * 22)  # obsolete claim-code format
    assert not is_supported_gift_claim_identifier('X' * 21)
    assert not is_supported_gift_claim_identifier('X' * 23)


@pytest.mark.asyncio
async def test_twelve_character_code_prefers_exact_claim_code() -> None:
    purchase = _gift()
    result = MagicMock()
    result.scalar_one_or_none.return_value = purchase
    db = AsyncMock()
    db.execute.return_value = result

    resolved = await get_gift_by_claim_identifier(db, 'C' * 12)

    assert resolved is purchase
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert 'guest_purchases.claim_code =' in sql
    assert 'guest_purchases.token LIKE' not in sql


@pytest.mark.asyncio
async def test_legacy_twelve_character_token_prefix_remains_supported() -> None:
    purchase = _gift()
    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = None
    legacy_result = MagicMock()
    legacy_result.scalars.return_value.all.return_value = [purchase]
    db = AsyncMock()
    db.execute.side_effect = [exact_result, legacy_result]

    resolved = await get_gift_by_claim_identifier(db, 'L' * 12)

    assert resolved is purchase
    legacy_statement = db.execute.await_args_list[1].args[0]
    sql = str(legacy_statement)
    assert 'guest_purchases.token LIKE' in sql
    assert 'guest_purchases.claim_code LIKE' not in sql


@pytest.mark.asyncio
async def test_ambiguous_twelve_character_prefix_is_rejected() -> None:
    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = None
    legacy_result = MagicMock()
    legacy_result.scalars.return_value.all.return_value = [_gift(), _gift()]
    db = AsyncMock()
    db.execute.side_effect = [exact_result, legacy_result]

    assert await get_gift_by_claim_identifier(db, 'C' * 12) is None
