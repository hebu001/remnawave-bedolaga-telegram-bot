"""Completing Telegram re-registration cannot undo the fork's saved balance.

0b4a4e78 explicitly retained deposits and balance in /start. Both completion
entrypoints must keep the current value rather than reconstruct it from deposits
(which could restore money already refunded or erase debt).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Subscription, Transaction, User, UserStatus
from app.handlers import start
from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize('balance', [12500, 0, -4500])
@pytest.mark.parametrize('via_callback', [False, True])
async def test_registration_completion_preserves_current_balance_and_ledger(monkeypatch, balance, via_callback):
    tables = (User.__table__, Subscription.__table__, Transaction.__table__)
    async with memory_session(monkeypatch, tables) as db:
        user = User(
            telegram_id=123,
            status=UserStatus.DELETED.value,
            language='ru',
            balance_kopeks=balance,
            has_had_paid_subscription=True,
        )
        db.add(user)
        await db.flush()
        db.add_all(
            [
                Transaction(user_id=user.id, type='deposit', amount_kopeks=20000, description='Existing deposit'),
                Transaction(user_id=user.id, type='refund', amount_kopeks=-7500, description='Already refunded'),
            ]
        )
        await db.commit()

        async def current_ledger():
            result = await db.execute(select(Transaction.__table__).order_by(Transaction.id))
            return [dict(row) for row in result.mappings()]

        before = await current_ledger()
        monkeypatch.setattr(start, 'get_user_by_telegram_id', AsyncMock(return_value=user))
        monkeypatch.setattr(
            start,
            '_prepare_telegram_completion_access',
            AsyncMock(return_value=(RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_GRANTED), None)),
        )
        monkeypatch.setattr(start, '_bind_registration_invite', AsyncMock(return_value=True))
        for name in (
            '_apply_campaign_bonus_if_needed',
            'delete_pending_payload_from_redis',
            '_activate_pending_gift_after_registration',
            '_redeem_pending_coupon',
            '_persist_pending_subid_after_registration',
        ):
            monkeypatch.setattr(start, name, AsyncMock(return_value=None))
        # Stop at the requested partner screen after completing the real DB writes.
        monkeypatch.setattr(start, '_open_pending_partner_menu', AsyncMock(return_value=True))
        state = SimpleNamespace(get_data=AsyncMock(return_value={'language': 'ru'}), clear=AsyncMock())
        telegram_user = SimpleNamespace(id=123, username='existing', first_name='Existing', last_name=None)
        message = SimpleNamespace(from_user=telegram_user, answer=AsyncMock(), bot=AsyncMock())
        event = SimpleNamespace(from_user=telegram_user, message=message, bot=message.bot) if via_callback else message
        complete = start.complete_registration_from_callback if via_callback else start.complete_registration

        for _ in range(2):
            await complete(event, state, db)
            await db.refresh(user)
            assert user.status == UserStatus.ACTIVE.value
            assert user.balance_kopeks == balance
            assert user.has_had_paid_subscription is True
            assert await current_ledger() == before
