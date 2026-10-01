"""Promo pricing must use explicit async loads and preserve priority discounts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import NO_VALUE

from app.database.models import (
    Base,
    PromoGroup,
    ServerSquad,
    SystemSetting,
    Tariff,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.services.gift_purchase_service import list_gift_offers, quote_gift_purchase
from app.services.pricing_engine import PricingEngine
from app.utils.pricing_utils import ensure_user_promo_groups_loaded
from tests.fixtures.postgres_db import postgres_session
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    SystemSetting.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    server_squad_promo_groups,
)


def test_loaded_priority_and_id_tiebreak_are_preserved():
    legacy = PromoGroup(id=1, name='legacy', priority=50)
    low = PromoGroup(id=2, name='low', priority=1)
    high = PromoGroup(id=3, name='high', priority=5)
    winner = PromoGroup(id=4, name='winner', priority=5)
    user = User(promo_group=legacy)
    user.user_promo_groups = [
        UserPromoGroup(promo_group_id=group.id, promo_group=group) for group in (winner, low, high)
    ]
    assert user.get_primary_promo_group() is winner
    assert PricingEngine.resolve_promo_group(user) is winner
    user.user_promo_groups = []
    assert user.get_primary_promo_group() is legacy


def test_unloaded_transient_promo_relationships_are_safe():
    user = User(telegram_id=123)
    assert inspect(user).attrs.user_promo_groups.loaded_value is NO_VALUE
    assert user.get_primary_promo_group() is None
    assert PricingEngine.resolve_promo_group(user) is None


@pytest.mark.asyncio
async def test_preload_does_not_query_for_transient_users_or_dtos():
    db = AsyncMock()
    await ensure_user_promo_groups_loaded(db, User())
    await ensure_user_promo_groups_loaded(db, SimpleNamespace(promo_group=None))
    await ensure_user_promo_groups_loaded(db, MagicMock(spec=User))
    await ensure_user_promo_groups_loaded(db, None)
    db.execute.assert_not_called()


async def _assert_quote_loads_best_group(db, loading):
    legacy = PromoGroup(name='legacy', priority=100, is_default=False, period_discounts={'30': 5})
    low = PromoGroup(name='low', priority=1, is_default=False, period_discounts={'30': 10})
    high = PromoGroup(name='high', priority=5, is_default=False, period_discounts={'30': 20})
    winner = PromoGroup(name='winner', priority=5, is_default=False, period_discounts={'30': 30})
    db.add_all([legacy, low, high, winner])
    await db.flush()
    buyer = User(telegram_id=99123, balance_kopeks=100000, promo_group_id=legacy.id)
    tariff = Tariff(name='Gift', is_active=True, show_in_gift=True, device_limit=2, period_prices={'30': 10000})
    db.add_all([buyer, tariff, SystemSetting(key='CABINET_GIFT_ENABLED', value='true')])
    await db.flush()
    db.add_all([UserPromoGroup(user_id=buyer.id, promo_group_id=group.id) for group in (low, high, winner)])
    await db.commit()
    buyer_id, tariff_id, winner_id = buyer.id, tariff.id, winner.id
    db.expunge_all()

    stmt = select(User).where(User.id == buyer_id)
    if loading == 'partial':
        stmt = stmt.options(selectinload(User.user_promo_groups))
    elif loading == 'legacy_only':
        stmt = stmt.options(selectinload(User.promo_group))
    elif loading != 'raw':
        stmt = stmt.options(
            selectinload(User.promo_group),
            selectinload(User.user_promo_groups)
            .selectinload(UserPromoGroup.promo_group)
            .defer(PromoGroup.period_discounts)
            if loading == 'deferred_discount'
            else selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
        )
    buyer = (await db.execute(stmt)).scalar_one()
    scalar_case = loading in {
        'expired_priority',
        'expired_discount',
        'expired_link_key',
        'deferred_discount',
        'expired_offer_deadline',
    }
    if scalar_case:
        links = buyer.user_promo_groups
        winner_link = next(link for link in links if link.promo_group_id == winner_id)
        winning_group = winner_link.promo_group
        low_group = next(link.promo_group for link in links if link.promo_group.name == 'low')
        if loading == 'expired_priority':
            db.expire(winning_group, ['priority'])
        elif loading == 'expired_discount':
            db.expire(winning_group, ['period_discounts'])
        elif loading == 'expired_link_key':
            db.expire(winner_link, ['promo_group_id'])
        elif loading == 'expired_offer_deadline':
            db.expire(buyer, ['promo_offer_discount_expires_at'])
        # Read repair must retain dirty pricing fields and pending collection additions.
        winning_group.device_discount_percent = 41
        winning_group.name = 'pending winner'
        low_group.period_discounts = {'30': 99}
        pending_link = UserPromoGroup(promo_group=PromoGroup(name='pending group', priority=0))
        links.append(pending_link)
        pending_links = tuple(links)
    # These pending mutations must survive the explicit relationship load.
    buyer.balance_kopeks = 77777
    buyer.promo_offer_discount_percent = 50
    # Exercise callers with autoflush enabled as well as the default prod setup.
    db.autoflush = True
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    event.listen(db.bind.sync_engine, 'before_cursor_execute', record_sql)
    try:
        # Pure getters may only read loaded state, even inside an async session.
        if loading in {'partial', 'legacy_only'} or (scalar_case and loading != 'expired_offer_deadline'):
            with pytest.raises(RuntimeError, match='explicitly loaded'):
                PricingEngine.resolve_promo_group(buyer)
            initial_group = None
        else:
            initial_group = PricingEngine.resolve_promo_group(buyer)
        assert not sql
        if loading in {'eager', 'expired_offer_deadline'}:
            assert initial_group.id == winner_id
        else:
            assert initial_group is None

        await ensure_user_promo_groups_loaded(db, buyer)
        assert buyer.get_primary_promo_group().id == winner_id
        assert buyer.balance_kopeks == 77777
        assert buyer.promo_offer_discount_percent == 50
        if scalar_case:
            assert winning_group.device_discount_percent == 41
            assert winning_group.name == 'pending winner'
            assert low_group.period_discounts == {'30': 99}
            assert tuple(buyer.user_promo_groups) == pending_links
        if loading == 'eager':
            assert not sql
        else:
            assert sql
        assert all(statement.lstrip().startswith('SELECT') for statement in sql)
        # Read through the active connection, bypassing ORM/autoflush: pending
        # changes must not have reached the DB even within our own transaction.
        connection = await db.connection()
        stored = (
            await connection.execute(
                select(User.__table__.c.balance_kopeks, User.__table__.c.promo_offer_discount_percent).where(
                    User.__table__.c.id == buyer_id
                )
            )
        ).one()
        assert tuple(stored) == (100000, 0)
        if scalar_case:
            stored_group = (
                await connection.execute(
                    select(PromoGroup.__table__.c.device_discount_percent, PromoGroup.__table__.c.name).where(
                        PromoGroup.__table__.c.id == winner_id
                    )
                )
            ).one()
            assert tuple(stored_group) == (0, 'winner')
        if db.bind.dialect.name == 'postgresql':
            async with db.bind.connect() as reader:
                independently_stored = (
                    await reader.execute(
                        select(User.__table__.c.balance_kopeks, User.__table__.c.promo_offer_discount_percent).where(
                            User.__table__.c.id == buyer_id
                        )
                    )
                ).one()
                assert tuple(independently_stored) == (100000, 0)
        sql.clear()
        await ensure_user_promo_groups_loaded(db, buyer)
        assert not sql  # A second quote does not repeat relationship queries.
        quote = await quote_gift_purchase(db, buyer, tariff_id, 30)
        assert quote.promo_group_discount_kopeks == 3000
        assert quote.promo_offer_discount_kopeks == 3500
        assert quote.final_price_kopeks == 3500

        # Public pricing paths also accept a freshly selected ORM user.
        db.expire(buyer, ['promo_group', 'user_promo_groups'])
        offers = await list_gift_offers(db, buyer)
        assert offers[0].quotes[0].final_price_kopeks == 3500
        db.expire(buyer, ['promo_group', 'user_promo_groups'])
        tariff = (await db.execute(select(Tariff).where(Tariff.id == tariff_id))).scalar_one()
        price = await PricingEngine().calculate_tariff_purchase_price(tariff, 30, user=buyer)
        assert price.final_total == 3500
    finally:
        event.remove(db.bind.sync_engine, 'before_cursor_execute', record_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'loading',
    [
        'raw',
        'partial',
        'legacy_only',
        'eager',
        'expired_priority',
        'expired_discount',
        'expired_link_key',
        'deferred_discount',
        'expired_offer_deadline',
    ],
)
async def test_sqlite_quote_preserves_best_discount_without_implicit_io(monkeypatch, loading):
    async with memory_session(monkeypatch, TABLES) as db:
        await _assert_quote_loads_best_group(db, loading)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    'loading',
    [
        'raw',
        'partial',
        'legacy_only',
        'eager',
        'expired_priority',
        'expired_discount',
        'expired_link_key',
        'deferred_discount',
        'expired_offer_deadline',
    ],
)
async def test_postgres_quote_preserves_best_discount_without_implicit_io(postgres_database, loading):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _assert_quote_loads_best_group(db, loading)


async def _assert_tariff_availability_load(db, allowed):
    group = PromoGroup(name='sender', priority=5, is_default=False, period_discounts={'30': 30})
    other = PromoGroup(name='other', priority=1, is_default=False, period_discounts={'30': 10})
    db.add_all([group, other])
    await db.flush()
    user = User(telegram_id=99500, balance_kopeks=100000, promo_group=group, user_promo_groups=[])
    tariff = Tariff(
        name='original tariff',
        is_active=True,
        device_limit=1,
        period_prices={'30': 10000},
        allowed_promo_groups=[group if allowed else other],
    )
    db.add_all([user, tariff])
    await db.commit()
    tariff_id = tariff.id
    db.expire(tariff, ['allowed_promo_groups'])
    assert inspect(tariff).attrs.allowed_promo_groups.loaded_value is NO_VALUE
    tariff.name = 'pending tariff'
    tariff.period_prices = {'30': 20000}
    user.balance_kopeks = 77777
    user.promo_offer_discount_percent = 50
    db.autoflush = True
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    event.listen(db.bind.sync_engine, 'before_cursor_execute', record_sql)
    try:
        price = await PricingEngine().calculate_tariff_purchase_price(tariff, 30, user=user)
        assert price.final_total == (7000 if allowed else 10000)
        assert price.promo_group_discount == (6000 if allowed else 0)
        assert tariff.name == 'pending tariff'
        assert tariff.period_prices == {'30': 20000}
        assert user.balance_kopeks == 77777
        assert user.promo_offer_discount_percent == 50
        assert sql and all(statement.lstrip().startswith('SELECT') for statement in sql)
        connection = await db.connection()
        stored_tariff = (
            await connection.execute(
                select(Tariff.__table__.c.name, Tariff.__table__.c.period_prices).where(
                    Tariff.__table__.c.id == tariff_id
                )
            )
        ).one()
        assert tuple(stored_tariff) == ('original tariff', {'30': 10000})
    finally:
        event.remove(db.bind.sync_engine, 'before_cursor_execute', record_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [True, False])
async def test_sqlite_direct_tariff_pricing_loads_availability_without_autoflush(monkeypatch, allowed):
    async with memory_session(monkeypatch, TABLES) as db:
        await _assert_tariff_availability_load(db, allowed)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [True, False])
async def test_postgres_direct_tariff_pricing_loads_availability_without_autoflush(postgres_database, allowed):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _assert_tariff_availability_load(db, allowed)
