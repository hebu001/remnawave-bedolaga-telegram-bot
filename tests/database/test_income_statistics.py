"""Execute the revenue queries against a small ledger, including month boundaries."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine

from app.database.crud import transaction as revenue
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


@pytest.fixture
def ledger(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    engine = create_engine('sqlite://')
    metadata = MetaData()
    transactions = Table(
        'transactions',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('type', String),
        Column('amount_kopeks', Integer),
        Column('payment_method', String),
        Column('is_completed', Boolean),
        Column('created_at', DateTime),
        Column('completed_at', DateTime),
    )
    wata = Table(
        'wata_payments',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('amount_kopeks', Integer),
        Column('is_paid', Boolean),
        Column('currency', String),
        Column('paid_at', DateTime),
    )
    yookassa = Table(
        'yookassa_payments',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('amount_kopeks', Integer),
        Column('is_paid', Boolean),
        Column('currency', String),
        Column('captured_at', DateTime),
        Column('status', String),
        Column('test_mode', Boolean),
        Column('transaction_id', Integer),
    )
    metadata.create_all(engine)
    with engine.connect() as connection:
        # Retained fixture helper for the historical receipt regression.
        connection.connection.create_function(
            'timezone',
            2,
            lambda zone, value: (
                datetime.fromisoformat(value)
                .replace(tzinfo=UTC)
                .astimezone(ZoneInfo('Europe/Moscow'))
                .isoformat(sep=' ')[:19]
                if value
                else None
            ),
        )

        class Ledger:
            def get_bind(self):
                return connection

            async def execute(self, statement):
                return connection.execute(statement)

            def add(self, table, **values):
                connection.execute(table.insert().values(**values))

        yield Ledger(), transactions, wata, yookassa
    engine.dispose()


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_subpage_deposit_and_debit_count_as_one_payment(ledger):
    db, transactions, wata, _ = ledger
    paid = utc('2026-09-10 10:00:00')
    for kind, amount in [('deposit', 29800), ('subscription_payment', -29800)]:
        db.add(
            transactions,
            type=kind,
            amount_kopeks=amount,
            payment_method='wata',
            is_completed=True,
            created_at=paid,
            completed_at=paid,
        )
    db.add(wata, amount_kopeks=29800, is_paid=True, currency='RUB', paid_at=paid)
    stats = await revenue.get_transactions_statistics(db, utc('2026-09-01'), utc('2026-09-16'))
    assert stats['totals']['income_kopeks'] == 29800
    assert stats['by_payment_method'] == {'wata': {'count': 1, 'amount': 29800}}


@pytest.mark.asyncio
async def test_unlinked_gift_included_and_unpaid_test_bonus_balance_excluded(ledger):
    db, transactions, wata, yookassa = ledger
    paid = utc('2026-09-10 10:00:00')
    db.add(wata, amount_kopeks=19900, is_paid=True, currency='RUB', paid_at=paid)
    db.add(wata, amount_kopeks=99900, is_paid=False, currency='RUB', paid_at=paid)
    for status, is_paid, test_mode in [
        ('succeeded', True, False),
        ('succeeded', True, True),
        ('canceled', False, False),
        ('waiting_for_capture', True, False),
    ]:
        db.add(
            yookassa,
            amount_kopeks=30000,
            is_paid=is_paid,
            currency='RUB',
            captured_at=paid,
            status=status,
            test_mode=test_mode,
        )
    for kind, method in [('referral_reward', None), ('deposit', 'manual'), ('subscription_payment', 'balance')]:
        db.add(
            transactions,
            type=kind,
            payment_method=method,
            amount_kopeks=10000,
            is_completed=True,
            created_at=paid,
            completed_at=paid,
        )
    assert await revenue.get_income_total(db, utc('2026-09-01'), utc('2026-09-16')) == 49900


@pytest.mark.asyncio
async def test_historical_yookassa_uses_completed_time_and_other_gateways_remain(ledger):
    db, transactions, _, yookassa = ledger
    paid = utc('2026-09-10 10:00:00')
    db.add(
        transactions,
        id=1,
        type='deposit',
        amount_kopeks=40000,
        payment_method='yookassa',
        is_completed=True,
        created_at=utc('2026-08-31'),
        completed_at=paid,
    )
    db.add(
        yookassa,
        amount_kopeks=40000,
        is_paid=True,
        currency='RUB',
        captured_at=None,
        transaction_id=1,
        status='succeeded',
        test_mode=False,
    )
    db.add(
        transactions,
        type='deposit',
        amount_kopeks=7000,
        payment_method='telegram_stars',
        is_completed=True,
        created_at=paid,
        completed_at=paid,
    )
    assert await revenue.get_income_total(db, utc('2026-09-01'), utc('2026-09-16')) == 47000


@pytest.mark.asyncio
async def test_moscow_month_today_and_chart_agree(ledger, monkeypatch):
    db, _, wata, _ = ledger
    fixed_now = utc('2026-09-15 22:00:00')  # September 16, 01:00 MSK

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(revenue, 'datetime', FixedDatetime)
    for paid, amount in [
        ('2026-08-31 20:59:59', 100),
        ('2026-08-31 21:00:00', 200),
        ('2026-09-15 20:59:59', 300),
        ('2026-09-15 21:00:00', 400),
        ('2026-09-15 22:00:01', 500),
    ]:
        db.add(wata, amount_kopeks=amount, is_paid=True, currency='RUB', paid_at=utc(paid))
    stats = await revenue.get_transactions_statistics(db)
    assert stats['period']['start_date'] == utc('2026-08-31 21:00:00')
    assert stats['totals']['income_kopeks'] == 900
    assert stats['today']['income_kopeks'] == 400
    chart = await revenue.get_revenue_by_period(db, days=16)
    assert [(str(row['date']), row['amount_kopeks']) for row in chart] == [
        ('2026-09-01', 200),
        ('2026-09-15', 300),
        ('2026-09-16', 400),
    ]
    assert sum(row['amount_kopeks'] for row in chart) == stats['totals']['income_kopeks']
    assert await revenue.get_revenue_by_period(db, days=1) == [chart[-1]]


@pytest.mark.asyncio
async def test_empty_period_returns_zero(ledger):
    db, *_ = ledger
    stats = await revenue.get_transactions_statistics(db, utc('2026-09-01'), utc('2026-09-16'))
    assert stats['totals']['income_kopeks'] == 0
    assert stats['by_payment_method'] == {}
