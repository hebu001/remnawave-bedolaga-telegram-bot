from unittest.mock import AsyncMock

import pytest

from app.handlers import start


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'code,exists,expected',
    [(None, False, False), ('start', False, False), ('unknown', False, False), ('valid-referral', True, True)],
)
async def test_referral_notice_requires_real_referrer(monkeypatch, code, exists, expected):
    lookup = AsyncMock(return_value=object() if exists else None)
    monkeypatch.setattr(start, 'get_user_by_referral_code', lookup)
    assert await start._is_existing_referral_code(AsyncMock(), code) is expected
    if code is None:
        lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_referral_notice_omitted_when_lookup_fails(monkeypatch):
    monkeypatch.setattr(start, 'get_user_by_referral_code', AsyncMock(side_effect=RuntimeError))
    assert not await start._is_existing_referral_code(AsyncMock(), 'unknown')


@pytest.mark.asyncio
async def test_repeat_gift_deeplink_reports_already_activated(monkeypatch):
    from types import SimpleNamespace

    from app.services.gift_claim_service import GiftClaimAlreadyActivatedError

    claim = AsyncMock(side_effect=GiftClaimAlreadyActivatedError)
    monkeypatch.setattr(start, 'claim_gift_for_user', claim)
    state = AsyncMock()
    state.get_data.return_value = {'pending_gift_token': 'gift-token'}
    answer = AsyncMock()
    await start._activate_pending_gift_after_registration(
        AsyncMock(), state, SimpleNamespace(id=1, language='ru'), answer
    )
    answer.assert_awaited_once()
    text = answer.call_args.args[0]
    assert 'уже был активирован' in text
    assert 'Ваша подписка обновлена' not in text
    assert claim.call_args.kwargs['report_already_activated'] is True
