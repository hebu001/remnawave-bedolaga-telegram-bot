"""Renewal/reset notification integration without payments, panel or Telegram IO."""

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.services import remnawave_webhook_service as webhooks, renewal_reset_notice as notice
from app.services.subscription_service import SubscriptionService


class RenewalResetNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = {}
        self.cache = SimpleNamespace(
            set=AsyncMock(side_effect=self.set_value),
            get=AsyncMock(side_effect=lambda k: self.storage.get(k)),
            getdel=AsyncMock(side_effect=lambda k: self.storage.pop(k, None)),
            delete=AsyncMock(side_effect=lambda k: self.storage.pop(k, None) is not None),
        )
        self.patches = [
            patch.object(notice, 'cache', self.cache),
            patch.object(webhooks, 'update_subscription_usage', AsyncMock()),
            patch.object(webhooks, 'reactivate_subscription', AsyncMock()),
            patch.object(webhooks, 'get_open_grace_subscription_ids', AsyncMock(return_value=set())),
            patch.object(webhooks.settings, 'WEBHOOK_NOTIFY_USER_ENABLED', True),
            patch.object(webhooks.settings, 'WEBHOOK_NOTIFY_TRAFFIC_RESET', True),
            patch.object(type(webhooks.settings), 'is_multi_tariff_enabled', return_value=False),
            patch('app.utils.timezone.get_local_timezone', return_value=ZoneInfo('Europe/Moscow')),
            patch.object(webhooks.notification_delivery_service, 'send_notification', AsyncMock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.sub = SimpleNamespace(
            id=11,
            user_id=22,
            is_trial=False,
            tariff_id=3,
            status='active',
            end_date=datetime.now(UTC).replace(hour=12, minute=30, second=0, microsecond=0) + timedelta(days=30),
        )
        self.user = SimpleNamespace(id=22, telegram_id=1234, language='ru')
        self.api = SimpleNamespace(reset_user_traffic=AsyncMock(return_value=SimpleNamespace(id=33)))
        self.service = webhooks.RemnaWaveWebhookService(bot=SimpleNamespace())
        self.service._get_subscription_keyboard = lambda user: webhooks.InlineKeyboardMarkup(
            inline_keyboard=[[webhooks.InlineKeyboardButton(text='Моя подписка', callback_data='subscription')]]
        )

    async def set_value(self, key, value, expire):
        self.assertEqual(expire, notice.NOTICE_TTL)
        self.storage[key] = value
        return True

    def payload(self, **changes):
        result = {
            'id': 33,
            'expireAt': self.sub.end_date.isoformat(),
            'lastTrafficResetAt': datetime.now(UTC).isoformat(),
        }
        result.update(changes)
        return result

    async def reset(self, renewal=True):
        return await notice.reset_traffic_with_notice(self.api, 33, self.sub, renewal=renewal)

    async def webhook(self, **changes):
        await self.service._handle_user_traffic_reset(AsyncMock(), self.user, self.sub, self.payload(**changes))
        return webhooks.notification_delivery_service.send_notification.call_args.kwargs

    async def test_renewal_webhook_renders_reset_and_new_local_expiry(self):
        await self.reset()
        delivered = await self.webhook()
        text = delivered['telegram_message']
        self.assertIn('Подписка продлена', text)
        self.assertIn('Трафик сброшен', text)
        self.assertIn(self.sub.end_date.strftime('%d.%m.%Y') + ' 15:30 MSK', text)
        self.assertEqual(delivered['context']['text_key'], 'WEBHOOK_SUB_RENEWED_RESET')
        self.assertEqual(len(delivered['telegram_markup'].inline_keyboard), 2)
        self.assertEqual(delivered['telegram_markup'].inline_keyboard[-1][0].callback_data, 'webhook:close')

    async def test_manual_or_scheduled_reset_keeps_generic_text(self):
        await self.reset(renewal=False)
        result = await self.webhook()
        self.assertEqual(result['context']['text_key'], 'WEBHOOK_SUB_TRAFFIC_RESET')
        self.assertNotIn('Подписка продлена', result['telegram_message'])

    async def test_failed_or_cancelled_reset_does_not_leave_notice(self):
        for failure in (RuntimeError('panel unavailable'), asyncio.CancelledError()):
            self.api.reset_user_traffic.side_effect = failure
            with self.assertRaises(type(failure)):
                await self.reset()
            self.assertEqual(self.storage, {})

    async def test_false_reset_result_does_not_leave_notice(self):
        self.api.reset_user_traffic.return_value = None
        self.assertIsNone(await self.reset())
        self.assertEqual(self.storage, {})

    async def test_webhook_before_api_response_still_correlates(self):
        async def reset(_id):
            delivered = await self.webhook()
            self.assertEqual(delivered['context']['text_key'], 'WEBHOOK_SUB_RENEWED_RESET')
            return SimpleNamespace(id=33)

        self.api.reset_user_traffic.side_effect = reset
        await self.reset()
        self.assertEqual(self.storage, {})

    async def test_duplicate_webhook_cannot_repeat_renewal_confirmation(self):
        await self.reset()
        self.assertIsNotNone(await notice.consume_renewal_reset(self.sub, self.payload()))
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_sibling_and_owner_mismatch_do_not_consume_context(self):
        await self.reset()
        for field in ('id', 'user_id'):
            original = getattr(self.sub, field)
            setattr(self.sub, field, 999)
            self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))
            setattr(self.sub, field, original)
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload(id=44)))
        self.assertIsNotNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_stale_or_unverifiable_event_is_generic(self):
        await self.reset()
        cases = (
            {'lastTrafficResetAt': (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
            {'lastTrafficResetAt': None},
            {'lastTrafficResetAt': 'bad'},
            {'id': True},
            {'expireAt': (self.sub.end_date - timedelta(days=30)).isoformat()},
        )
        for case in cases:
            self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload(**case)))
        self.assertIsNotNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_rolled_back_or_replaced_term_is_not_announced(self):
        await self.reset()
        self.sub.end_date += timedelta(days=1)
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_expired_context_is_not_used_even_if_cache_returns_it(self):
        await self.reset()
        self.storage[notice._key(33)]['started_at'] = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_trial_and_expired_subscriptions_are_not_marked(self):
        self.sub.is_trial = True
        await self.reset()
        self.assertEqual(self.storage, {})
        self.sub.is_trial = False
        self.sub.end_date = datetime.now(UTC) - timedelta(seconds=1)
        await self.reset()
        self.assertEqual(self.storage, {})

    async def test_missing_cache_does_not_prevent_panel_reset(self):
        self.cache.set.side_effect = None
        self.cache.set.return_value = False
        await self.reset()
        self.api.reset_user_traffic.assert_awaited_once_with(33)
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_notification_settings_are_preserved(self):
        for setting in ('WEBHOOK_NOTIFY_USER_ENABLED', 'WEBHOOK_NOTIFY_TRAFFIC_RESET'):
            await self.reset()
            with patch.object(webhooks.settings, setting, False):
                await self.service._handle_user_traffic_reset(AsyncMock(), self.user, self.sub, self.payload())
            webhooks.notification_delivery_service.send_notification.assert_not_awaited()

    async def test_subscription_service_only_marks_explicit_renewal_reasons(self):
        service = SubscriptionService.__new__(SubscriptionService)
        for reason in ('смена тарифа', 'покупка тарифа', 'конвертация триала', None):
            await service._reset_user_traffic(self.api, 33, self.user, reason, subscription=self.sub)
            self.assertEqual(self.storage, {})
        for reason in notice.RENEWAL_REASONS:
            await service._reset_user_traffic(self.api, 33, self.user, reason, subscription=self.sub)
            self.assertIsNotNone(await notice.consume_renewal_reset(self.sub, self.payload()))

    async def test_purchase_classification_excludes_trial_new_and_switch(self):
        self.assertTrue(notice.is_tariff_renewal(self.sub, 3))
        self.assertFalse(notice.is_tariff_renewal(self.sub, 4))
        self.assertFalse(notice.is_tariff_renewal(None, 3))
        self.sub.is_trial = True
        self.assertFalse(notice.is_tariff_renewal(self.sub, 3))

    async def test_ordinary_reset_clears_old_renewal_context(self):
        await self.reset()
        await self.reset(renewal=False)
        self.assertIsNone(await notice.consume_renewal_reset(self.sub, self.payload()))
