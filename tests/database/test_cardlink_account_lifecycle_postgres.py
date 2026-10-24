"""Actual account deletion retains paid Cardlink receipts and revenue on PostgreSQL."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from app.database.crud.transaction import get_income_total, income_payments_query
from app.database.models import CardlinkPayment, Transaction, User
from app.services.blocked_users_service import BlockedUsersService
from app.services.remnawave_service import RemnaWaveService
from app.services.user_service import UserService
from tests.database.test_cardlink_settlement_postgres import (
    AMOUNT,
    TABLES,
    deliver,
    seed,
    service,  # noqa: F401
)
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('delete_path', ['blocked_users', 'user_service'])
@pytest.mark.parametrize('provider_status', ['SUCCESS', 'OVERPAID'])
async def test_account_deletion_preserves_cardlink_income_and_replay_safety(
    postgres_database, service, monkeypatch, delete_path, provider_status
):
    """Keep the verified receipt after deleting its account and ledger, without a second credit."""
    panel_call = Mock(side_effect=AssertionError('No panel call is expected for this account'))
    monkeypatch.setattr(RemnaWaveService, 'get_api_client', panel_call)
    bot = Mock()
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)

    async with postgres_sessions(postgres_database, TABLES) as (db, observer):
        user_id, payment_id, callback = await seed(db, service)
        service.cardlink_service.get_bill.return_value['status'] = provider_status
        assert await deliver(service, db, callback)
        other_user = User(telegram_id=905102, first_name='Unrelated fixture', balance_kopeks=777)
        db.add(other_user)
        await db.commit()
        other_user_id = other_user.id
        assert await get_income_total(observer, start, end) == AMOUNT
        await observer.rollback()
        receipt = await db.get(CardlinkPayment, payment_id)
        assert receipt.is_paid and receipt.transaction_id is not None
        paid_at = receipt.paid_at
        transaction_id = receipt.transaction_id
        await db.rollback()
        db.expunge_all()

        if delete_path == 'blocked_users':
            assert await BlockedUsersService(bot).delete_user_from_db(db, user_id)
        else:
            result = await UserService().delete_user_account(db, user_id, admin_id=0)
            assert result.bot_deleted and not result.grace_blocked

        # A fresh identity map prevents ORM-cached objects from masking an actual DELETE.
        observer.expunge_all()
        assert await observer.get(User, user_id) is None
        assert await observer.get(Transaction, transaction_id) is None
        receipt = await observer.get(CardlinkPayment, payment_id)
        assert receipt is not None and receipt.is_paid
        assert receipt.user_id is None and receipt.transaction_id is None
        assert receipt.amount_kopeks == AMOUNT and receipt.status == provider_status
        assert receipt.paid_at == paid_at
        assert await get_income_total(observer, start, end) == AMOUNT
        assert await observer.scalar(select(func.count()).select_from(income_payments_query())) == 1
        await observer.rollback()

        # A late provider retry and explicit status refresh must not recreate the
        # deleted account, resurrect the ledger or credit an unrelated account.
        db.expunge_all()
        assert await deliver(service, db, callback)
        assert await deliver(service, db, callback, poll=True, payment_id=payment_id)
        observer.expunge_all()
        assert await observer.get(User, user_id) is None
        assert await observer.scalar(select(func.count()).select_from(Transaction)) == 0
        receipt = await observer.get(CardlinkPayment, payment_id)
        assert receipt.user_id is None and receipt.transaction_id is None and receipt.is_paid
        assert (await observer.get(User, other_user_id)).balance_kopeks == 777
        assert await get_income_total(observer, start, end) == AMOUNT
        assert await observer.scalar(select(func.count()).select_from(income_payments_query())) == 1
        panel_call.assert_not_called()
        assert not bot.mock_calls
