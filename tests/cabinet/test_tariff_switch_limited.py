"""Exercise cabinet preview and confirmation with exhausted paid subscriptions.

Real route, pricing and traffic policy; synthetic persistence and panel adapters.
The same unittest suite also runs inside the isolated release image.
"""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import tariff_switch as route
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.database.models import Subscription, Tariff


class LimitedTariffSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.end = datetime.now(UTC) + timedelta(days=10, hours=12)
        self.sub = Subscription(
            id=101,
            user_id=42,
            status='limited',
            is_trial=False,
            tariff_id=1,
            end_date=self.end,
            traffic_limit_gb=300,
            traffic_used_gb=300.04,
            device_limit=2,
            purchased_traffic_gb=0,
            remnawave_id=None,
        )
        self.user = SimpleNamespace(
            id=42, balance_kopeks=10000, remnawave_id=None, promo_group=None, get_primary_promo_group=lambda: None
        )
        self.old = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            is_daily=False,
            period_prices={'30': 19900},
            traffic_limit_gb=300,
            device_limit=2,
        )
        self.new = Tariff(
            id=3,
            name='Family',
            is_active=True,
            is_daily=False,
            period_prices={'30': 39900},
            traffic_limit_gb=750,
            device_limit=5,
            allowed_squads=[],
            allowed_promo_groups=[],
        )
        self.request = TariffPurchaseRequest(tariff_id=3, period_days=30)
        self.db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: self.sub)),
            refresh=AsyncMock(),
            commit=AsyncMock(),
        )
        self.patch(route, 'resolve_subscription', AsyncMock(return_value=self.sub))
        self.patch(
            route,
            'get_tariff_by_id',
            AsyncMock(side_effect=lambda db, tariff_id: self.old if tariff_id == 1 else self.new),
        )
        for key, value in {
            'SALES_MODE': 'tariffs',
            'MULTI_TARIFF_ENABLED': False,
            'TARIFF_SWITCH_UPGRADE_ENABLED': True,
            'TARIFF_SWITCH_DOWNGRADE_ENABLED': True,
            'TARIFF_SWITCH_RESET_FREE_DAYS': True,
            'RESET_TRAFFIC_ON_TARIFF_SWITCH': True,
            'ADMIN_NOTIFICATIONS_ENABLED': False,
        }.items():
            self.patch(route.settings, key, value)
        self.mock_path('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=self.user))
        self.undo = self.mock_path('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock())
        for name in ('platega', 'lava'):
            self.mock_path(f'app.services.payment.{name}.cancel_{name}_recurring_for_subscription_safe', AsyncMock())
        self.mock_path(
            'app.services.cashera_recurring_cancel.cancel_cashera_recurring_for_subscription_safe', AsyncMock()
        )
        self.mock_path('app.services.yandex_offline_conv_service.store_cid_only', AsyncMock())
        self.mock_path('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
        self.transactions = self.patch(route, 'create_transaction', AsyncMock(return_value=SimpleNamespace(id=77)))

        async def charge(db, user, amount, *args, **kwargs):
            user.balance_kopeks -= amount
            return True

        self.charge = self.patch(route, 'subtract_user_balance', AsyncMock(side_effect=charge))
        self.panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
        self.patch(route, 'SubscriptionService', Mock(return_value=self.panel))
        self.patch(route, '_subscription_to_response', lambda sub, **kw: {'status': sub.status})

    def patch(self, target, name, value):
        p = patch.object(target, name, value)
        self.addCleanup(p.stop)
        return p.start()

    def mock_path(self, path, value):
        p = patch(path, value)
        self.addCleanup(p.stop)
        return p.start()

    async def preview(self):
        return await route.preview_tariff_switch(self.request, self.user, self.db, self.sub.id)

    async def confirm(self):
        return await route.switch_tariff(self.request, self.user, self.db, self.sub.id)

    async def test_paid_limited_upgrade_quotes_charges_and_reactivates(self):
        preview = await self.preview()
        self.assertTrue(preview['can_switch'])
        self.assertEqual(preview['upgrade_cost_kopeks'], 6666)
        result = await self.confirm()
        self.assertEqual(result['charged_kopeks'], preview['upgrade_cost_kopeks'])
        self.assertEqual(self.user.balance_kopeks, 3334)
        self.assertEqual((self.sub.status, self.sub.tariff_id, self.sub.traffic_limit_gb), ('active', 3, 750))
        self.assertEqual(self.sub.end_date, self.end)
        self.assertEqual(self.sub.traffic_used_gb, 0)
        self.assertEqual(self.charge.await_count, 1)
        self.assertEqual(self.transactions.await_count, 1)
        self.assertFalse(self.charge.await_args.kwargs['commit'])
        self.db.commit.assert_awaited_once()
        self.assertTrue(self.panel.create_remnawave_user.await_args.kwargs['reset_traffic'])

    async def test_insufficient_balance_is_quoted_and_blocks_confirmation(self):
        self.user.balance_kopeks = 3350
        preview = await self.preview()
        self.assertFalse(preview['can_switch'])
        self.assertEqual(preview['missing_amount_kopeks'], 3316)
        with self.assertRaises(HTTPException) as raised:
            await self.confirm()
        self.assertEqual(raised.exception.status_code, 402)
        self.assertEqual(self.sub.tariff_id, 1)
        self.assertEqual(self.sub.status, 'limited')
        self.charge.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    async def test_disallowed_states_rejected_by_both_endpoints(self):
        for status, trial, expired, code in [
            ('limited', False, True, 'subscription_expired'),
            ('active', False, True, 'subscription_expired'),
            ('expired', False, False, 'subscription_expired'),
            ('disabled', False, False, 'subscription_not_active'),
            ('pending', False, False, 'subscription_not_active'),
            ('limited', True, False, 'trial_cannot_switch'),
        ]:
            with self.subTest(status=status, trial=trial, expired=expired):
                self.sub.status, self.sub.is_trial = status, trial
                self.sub.end_date = datetime.now(UTC) - timedelta(seconds=1) if expired else self.end
                for call in (self.preview, self.confirm):
                    with self.assertRaises(HTTPException) as raised:
                        await call()
                    self.assertEqual(raised.exception.detail['code'], code)
        self.charge.assert_not_awaited()
        self.transactions.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    async def test_expiration_rechecked_on_locked_subscription(self):
        await self.preview()
        self.sub.end_date = datetime.now(UTC) - timedelta(seconds=1)
        with self.assertRaises(HTTPException) as raised:
            await self.confirm()
        self.assertEqual(raised.exception.detail['code'], 'subscription_expired')
        self.charge.assert_not_awaited()

    async def test_grace_restored_expiry_cannot_produce_free_switch(self):
        async def restore(*args):
            self.sub.end_date = datetime.now(UTC) - timedelta(seconds=1)

        self.undo.side_effect = restore
        with self.assertRaises(HTTPException) as raised:
            await self.confirm()
        self.assertEqual(raised.exception.detail['code'], 'subscription_expired')
        self.charge.assert_not_awaited()
        self.transactions.assert_not_awaited()

    async def test_upgrade_with_reset_disabled_preserves_used_traffic(self):
        route.settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False
        await self.confirm()
        self.assertEqual(self.sub.status, 'active')
        self.assertEqual(self.sub.traffic_used_gb, 300.04)
        self.assertFalse(self.panel.create_remnawave_user.await_args.kwargs['reset_traffic'])

    async def test_free_switch_does_not_grant_traffic_or_reactivate_exhausted_quota(self):
        self.new.period_prices = {'30': 9900}
        self.new.traffic_limit_gb = 100
        result = await self.confirm()
        self.assertEqual(result['charged_kopeks'], 0)
        self.assertEqual(self.sub.status, 'limited')
        self.assertEqual(self.sub.traffic_used_gb, 300.04)
        self.assertEqual(self.sub.end_date, self.end)
        self.charge.assert_not_awaited()
        self.assertFalse(self.panel.create_remnawave_user.await_args.kwargs['reset_traffic'])

    async def test_unlimited_tariff_restores_access_without_reset(self):
        route.settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False
        self.new.traffic_limit_gb = 0
        await self.confirm()
        self.assertEqual(self.sub.status, 'active')
        self.assertEqual(self.sub.traffic_used_gb, 300.04)

    async def test_ordinary_active_upgrade_keeps_existing_behavior(self):
        self.sub.status, self.sub.traffic_used_gb = 'active', 5
        await self.test_paid_limited_upgrade_quotes_charges_and_reactivates()

    async def test_free_round_trip_and_full_discount_preserve_consumed_traffic(self):
        self.user.promo_offer_discount_percent = 100
        self.sub.traffic_used_gb = 750.0
        original_old, original_new = self.old, self.new
        for target in (original_new, original_old, original_new, original_old):
            current = original_old if self.sub.tariff_id == original_old.id else original_new
            self.old, self.new = current, target
            self.patch(
                route,
                'get_tariff_by_id',
                AsyncMock(side_effect=lambda db, tariff_id: current if tariff_id == current.id else target),
            )
            self.request = TariffPurchaseRequest(tariff_id=target.id, period_days=30)
            result = await self.confirm()
            self.assertEqual(result['charged_kopeks'], 0)
            self.assertEqual(self.sub.traffic_used_gb, 750.0)
            self.assertEqual(self.sub.status, 'limited')
            self.assertEqual(self.sub.end_date, self.end)
            self.assertFalse(self.panel.create_remnawave_user.await_args.kwargs['reset_traffic'])
        self.assertEqual(self.user.balance_kopeks, 10000)
        self.charge.assert_not_awaited()

    async def test_active_downgrade_to_exhausted_quota_becomes_limited(self):
        self.sub.status, self.sub.traffic_used_gb = 'active', 200.0
        self.new.period_prices, self.new.traffic_limit_gb = {'30': 9900}, 100
        await self.confirm()
        self.assertEqual(self.sub.status, 'limited')
        self.assertEqual(self.sub.traffic_used_gb, 200.0)
        self.charge.assert_not_awaited()

    async def test_duplicate_confirmation_cannot_charge_or_reset_again(self):
        await self.confirm()
        self.sub.traffic_used_gb = 12.5
        with self.assertRaises(HTTPException) as raised:
            await self.confirm()
        self.assertEqual(raised.exception.detail, 'Already on this tariff')
        self.assertEqual(self.charge.await_count, 1)
        self.assertEqual(self.sub.traffic_used_gb, 12.5)

    async def test_quota_boundary_does_not_restore_access(self):
        route.settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False
        self.sub.traffic_used_gb = 750.0
        await self.confirm()
        self.assertEqual(self.sub.status, 'limited')
        self.assertEqual(self.sub.traffic_used_gb, 750.0)

    async def test_direction_restriction_applies_to_limited(self):
        route.settings.TARIFF_SWITCH_UPGRADE_ENABLED = False
        for call in (self.preview, self.confirm):
            with self.assertRaises(HTTPException) as raised:
                await call()
            self.assertEqual(raised.exception.status_code, 403)
        self.charge.assert_not_awaited()

    async def test_free_source_tariff_still_requires_purchase(self):
        self.old.period_prices = {'30': 0}
        for call in (self.preview, self.confirm):
            with self.assertRaises(HTTPException) as raised:
                await call()
            self.assertEqual(raised.exception.detail['code'], 'free_tariff_cannot_switch')
        self.charge.assert_not_awaited()
