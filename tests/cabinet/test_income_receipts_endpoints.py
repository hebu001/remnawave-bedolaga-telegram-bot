"""Execute income consumers against gateway receipts, including unlinked gifts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_sales_stats, admin_stats
from app.database.crud.transaction import get_income_total, get_revenue_by_period, get_transactions_statistics
from app.database.models import CasheraPayment, GuestPurchase, Transaction, User, WataPayment, YooKassaPayment
from app.utils.timezone import local_month_start
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
ADMIN = SimpleNamespace(id=1, username='admin')
TABLES = [
    User.__table__,
    Transaction.__table__,
    WataPayment.__table__,
    YooKassaPayment.__table__,
    CasheraPayment.__table__,
    GuestPurchase.__table__,
]


@pytest.mark.asyncio
async def test_receipts_drive_cards_chart_recent_payments_and_sales(
    postgres_database, monkeypatch, reset_local_timezone_cache
):
    zone = use_timezone(monkeypatch, 'Europe/Moscow')
    now = datetime.now(UTC)
    start = local_month_start(now)
    async with postgres_session(postgres_database, TABLES) as db:
        user = User(telegram_id=1, first_name='Synthetic payer')
        db.add(user)
        await db.flush()
        for kind, amount in [('deposit', 29800), ('subscription_payment', -29800)]:
            db.add(
                Transaction(
                    user_id=user.id,
                    type=kind,
                    amount_kopeks=amount,
                    payment_method='wata',
                    is_completed=True,
                    created_at=now,
                    completed_at=now,
                )
            )
        old_deposit = Transaction(
            user_id=user.id,
            type='deposit',
            amount_kopeks=30000,
            payment_method='yookassa',
            is_completed=True,
            created_at=start - timedelta(days=1),
            completed_at=now,
        )
        db.add(old_deposit)
        await db.flush()
        db.add_all(
            [
                WataPayment(payment_link_id='ordinary', amount_kopeks=29800, is_paid=True, currency='RUB', paid_at=now),
                WataPayment(
                    payment_link_id='unlinked-gift', amount_kopeks=19900, is_paid=True, currency='RUB', paid_at=now
                ),
                YooKassaPayment(
                    yookassa_payment_id='legacy',
                    amount_kopeks=30000,
                    is_paid=True,
                    status='succeeded',
                    currency='RUB',
                    test_mode=False,
                    captured_at=None,
                    transaction_id=old_deposit.id,
                ),
                YooKassaPayment(
                    yookassa_payment_id='test-payment',
                    amount_kopeks=99900,
                    is_paid=True,
                    status='succeeded',
                    currency='RUB',
                    test_mode=True,
                    captured_at=now,
                ),
                GuestPurchase(
                    token='a' * 64,
                    contact_type='telegram',
                    contact_value='synthetic',
                    period_days=30,
                    amount_kopeks=19900,
                    is_gift=True,
                    status='paid',
                    paid_at=now,
                    payment_method='wata',
                ),
            ]
        )
        await db.commit()
        expected = 29800 + 19900 + 30000
        stats = await get_transactions_statistics(db, start, now)
        assert stats['totals']['income_kopeks'] == expected
        assert stats['today']['income_kopeks'] == expected
        assert sum(item['amount'] for item in stats['by_payment_method'].values()) == expected
        assert stats['by_payment_method']['wata']['count'] == 2
        chart = await get_revenue_by_period(db, days=now.astimezone(zone).day)
        assert sum(row['amount_kopeks'] for row in chart) == expected
        recent = await admin_stats.get_recent_payments(limit=50, admin=ADMIN, db=db)
        assert recent.total_today_kopeks == recent.total_week_kopeks == expected
        sales = await admin_sales_stats.get_sales_summary(
            days=None, start_date=start.isoformat(), end_date=now.isoformat(), admin=ADMIN, db=db
        )
        assert sales.total_revenue_kopeks == expected
        # All financial dashboard queries remain real; only unrelated panel/subscription calls are isolated.
        monkeypatch.setattr(
            admin_stats,
            '_get_nodes_overview',
            AsyncMock(
                return_value=admin_stats.NodesOverview(
                    total=0, online=0, offline=0, disabled=0, total_users_online=0, nodes=[]
                )
            ),
        )
        monkeypatch.setattr(admin_stats, 'get_subscriptions_statistics', AsyncMock(return_value={}))
        monkeypatch.setattr(admin_stats, 'get_server_statistics', AsyncMock(return_value={}))
        monkeypatch.setattr(admin_stats, '_get_tariff_stats', AsyncMock(return_value=None))
        dashboard = await admin_stats.get_dashboard_stats(admin=ADMIN, db=db)
        assert dashboard.financial.income_today_kopeks == dashboard.financial.income_month_kopeks == expected


@pytest.mark.asyncio
async def test_sales_preserves_other_gateway_gift_fallback(postgres_database, monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    now = datetime.now(UTC)
    start = local_month_start(now)
    async with postgres_session(postgres_database, TABLES) as db:
        db.add(
            GuestPurchase(
                token='b' * 64,
                contact_type='telegram',
                contact_value='synthetic',
                period_days=30,
                amount_kopeks=7000,
                is_gift=True,
                status='paid',
                paid_at=now,
                payment_method='telegram_stars',
            )
        )
        await db.commit()
        # Receipt restoration must not erase the upstream sales-only fallback for other gateways.
        assert await get_income_total(db, start, now) == 0
        sales = await admin_sales_stats.get_sales_summary(
            days=None, start_date=start.isoformat(), end_date=now.isoformat(), admin=ADMIN, db=db
        )
        assert sales.total_revenue_kopeks == 7000


@pytest.mark.asyncio
async def test_cashera_gift_receipt_and_recurring_share_income_source_without_sales_fallback_double_count(
    postgres_database,
    monkeypatch,
    reset_local_timezone_cache,
):
    use_timezone(monkeypatch, 'Europe/Moscow')
    now = datetime.now(UTC)
    start = local_month_start(now)
    async with postgres_session(postgres_database, TABLES) as db:
        user = User(telegram_id=90001, first_name='Synthetic Cashera payer')
        db.add(user)
        await db.flush()
        topup = Transaction(
            user_id=user.id,
            type='deposit',
            amount_kopeks=30000,
            payment_method='cashera',
            external_id='synthetic-one-time',
            is_completed=True,
            created_at=now,
            completed_at=now,
        )
        db.add(topup)
        await db.flush()
        db.add_all(
            [
                CasheraPayment(
                    user_id=user.id,
                    order_id='synthetic-one-time',
                    cashera_uuid='cashera-one-time',
                    amount_kopeks=30000,
                    status='success',
                    is_paid=True,
                    paid_at=now,
                    transaction_id=topup.id,
                ),
                Transaction(
                    user_id=user.id,
                    type='subscription_payment',
                    amount_kopeks=-30000,
                    payment_method='cashera',
                    external_id='cashera-one-time',
                    is_completed=True,
                    created_at=now,
                    completed_at=now,
                ),
                CasheraPayment(
                    order_id='synthetic-gift',
                    cashera_uuid='cashera-direct-gift',
                    amount_kopeks=22000,
                    status='success',
                    is_paid=True,
                    paid_at=now,
                ),
                GuestPurchase(
                    token='c' * 64,
                    contact_type='telegram',
                    contact_value='synthetic',
                    period_days=30,
                    amount_kopeks=22000,
                    is_gift=True,
                    status='paid',
                    paid_at=now,
                    payment_method='cashera',
                ),
                Transaction(
                    user_id=user.id,
                    type='subscription_payment',
                    amount_kopeks=-40000,
                    payment_method='cashera',
                    external_id='cashera-recurring-only',
                    is_completed=True,
                    created_at=now,
                    completed_at=now,
                ),
                CasheraPayment(
                    order_id='synthetic-unpaid',
                    amount_kopeks=99900,
                    status='pending',
                    is_paid=False,
                    paid_at=now,
                ),
            ]
        )
        await db.commit()
        expected = 30000 + 22000 + 40000
        stats = await get_transactions_statistics(db, start, now)
        assert stats['totals']['income_kopeks'] == expected
        assert stats['by_payment_method'] == {'cashera': {'count': 3, 'amount': expected}}
        recent = await admin_stats.get_recent_payments(limit=50, admin=ADMIN, db=db)
        assert recent.total_today_kopeks == expected
        sales = await admin_sales_stats.get_sales_summary(
            days=None,
            start_date=start.isoformat(),
            end_date=now.isoformat(),
            admin=ADMIN,
            db=db,
        )
        assert sales.total_revenue_kopeks == expected
